"""Extract measured sweep results into docs/results_reproduced.json, keyed by offered load.

Parses the per-run logs written by sweep.sh.

Usage: python3 extract_results.py <log_dir> ../docs/results_reproduced.json [sweep_stdout ...]

Conditions (all at 100 GbE line rate unless noted):
  pinned_2100mhz     logs tagged "pinned"   -- every core pinned to 2.100 GHz, turbo off,
                                               cpuset-isolated (INVESTIGATION.md 3.4b/3.4c).
                                               TSC ticks == core cycles in this arm.
  turbo_instr        logs tagged "turbo"    -- same code and cpuset, turbo ON. The
                                               representative arm: production runs with turbo.
  linerate_2tx_instr logs tagged "instr"    -- 2 TX gen, post-7ebf038 (float stats). Turbo,
                                               NO frequency instrumentation -- see caveat below.
  linerate_93mpps    logs tagged "linerate" -- generator -l 0-16 (8 TX cores)
  linerate_2tx_gen   logs tagged "gen2tx"   -- generator -l 0-4  (2 TX cores)
  engine_trio        logs tagged "trio"   -- all three engines in one sitting:
                                               dramblast, maglev, and `-m none` (the
                                               forwarding loop's third branch,
                                               main.c:351-356 -- same MAC write, no
                                               lookup). The floor everything else must
                                               be read against.
  capped_72mpps      logs tagged "sweep"    -- generator -l 0-2  (1 TX core), 72 Mpps

Caveat on cycles_per_pkt across conditions
------------------------------------------
l2fwd measures with rte_rdtsc() and this SKU's TSC is invariant at 2.1 GHz, so
"Cycle per fwd packet" is TSC ticks = elapsed time, in EVERY condition. It is
therefore comparable across conditions as time, but it equals core cycles only
where the core clock is also 2.1 GHz, i.e. the pinned_2100mhz arm. For the turbo
arms multiply by the recorded freq_mhz/2100 to get core cycles; where freq_mhz is
absent (linerate_2tx_instr predates the instrumentation) that conversion is not
available and must not be guessed.

Delivered frequency and 1 GiB backing
-------------------------------------
sweep.sh samples both per run but prints them on its own stdout summary line, not
into the per-run log. Pass the tee'd sweep stdout as a trailing argument and they
are merged in by (mode, q); omit it and freq_mhz/hp1g are simply absent.
"""
import json, re, sys, pathlib, statistics

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from perf_csv import parse_perf   # the multiplexing guard

SP = pathlib.Path(sys.argv[1]); OUT = pathlib.Path(sys.argv[2])
pats = {
    "min": r"Minimum: ([0-9.]+)", "max": r"Maximum: ([0-9.]+)", "avg": r"Average: ([0-9.]+)",
    "cycles_per_pkt": r"Cycle per fwd packet: (\d+)",
    "rx_batch": r"Average rx batch sz: (\d+)",
    "rx_missed": r"RX-Missed \(Dropped\): (\d+)",
}

# l2fwd's own Minimum/Maximum/Average are unreliable and must not be used as the
# headline number:
#   * main.c:166 declares `samples` as uint32_t* but main.c:208 stores a double
#     Mpps into it, so every sample is floor()ed. That is a fixed ~0.5 Mpps
#     downward bias -- only -0.3% at 93 Mpps but -4.6% at 17 Mpps, i.e. worst
#     exactly where the unsaturated per-queue slope is measured.
#   * Sample 0 is always cold (12-19% low) and therefore always sets `Minimum`,
#     so the reported min/max range is a startup artifact, not run-to-run spread.
#   * main.c:205 computes the interval as integer division, so it is exactly 1.0
#     even when the real interval drifts longer -- which lets `Maximum` exceed
#     line rate (a 95 Mpps reading on a 93.28 Mpps link).
# The per-second "%.2f Mpps" lines (main.c:207) are printed BEFORE truncation, so
# full precision is recoverable from the log. steady_mpps below is the median of
# those, excluding the cold first sample: immune to all three defects.
SAMPLE_RE = re.compile(r"^([0-9.]+) Mpps", re.M)
# condition -> log filename prefix
CONDS = {
    # the two clock arms: identical binary, cpuset and harness, differing only
    # in turbo and the frequency governor
    "pinned_2100mhz": "pinned", "turbo_instr": "turbo",
    # everything below was taken with the -B/-A/-Q build. `pinned2_asshipped`
    # is its control: same flags-free invocation as `pinned_2100mhz`, so any
    # difference between the two is the refactor and not the experiment.
    "pinned2_asshipped": "pinned2",
    # the same condition re-run hours later: the only run-to-run error bar
    "pinned3_repeat": "pinned3",
    # crossover: each mode on the other's page backing (and on 4 KiB, which
    # neither ships with, to turn a two-point swap into a three-point trend)
    "xover_dram_thp2m": "xdram2m", "xover_dram_4k": "xdram4k",
    "xover_mag_1g": "xmag1g", "xover_mag_4k": "xmag4k",
    # allocator amplification: C should be linear in the number of pairs
    "alloc_hoisted": "ahoist", "alloc_x2": "a2", "alloc_x4": "a4",
    "alloc_x8": "a8",
    # the floor: -m none, same MAC write, no lookup. Subtracting this arm is
    # what turns a per-packet cost into a per-LOOKUP cost.
    "engine_trio": "trio",
    # prefetch pipeline depth
    "depth_8": "d8", "depth_16": "d16", "depth_32": "d32",
    # depth 32 against the shipped 64, three interleaved repeats each, q=1..5
    # only. The single-sweep comparison was 1.8 cycles/packet against a 0.45
    # run-to-run floor, which separates the ramp model from no effect at
    # neither; these are the repeats that decide it.
    "depth_32_r1": "d32r1", "depth_32_r2": "d32r2", "depth_32_r3": "d32r3",
    "depth_64_r1": "d64r1", "depth_64_r2": "d64r2", "depth_64_r3": "d64r3",
    # historical arms, kept as the record; no delivered clock was recorded for
    # any of them, so their ticks cannot be put on a core-cycle axis
    "linerate_2tx_instr": "instr", "linerate_93mpps": "linerate",
    "linerate_2tx_gen": "gen2tx", "capped_72mpps": "sweep",
}

# sweep.sh's stdout summary line, e.g.
#   q=3  lcores=0,2,4,6  min=... hp1g=16->7 freq=2095MHz
# preceded by "=== mode=dramblast  tag=pinned ===" headers.
HDR_RE = re.compile(r"^=== mode=(\w+)\s+tag=(\w+) ===", re.M)
ROW_RE = re.compile(
    r"^q=(\d+)\s+.*?hp1g=(\d+)->(\d+)\s+freq=(\w+)MHz\s+insns=(\w+)", re.M)


def sidecar(paths):
    """(tag, mode, q) -> {hp1g_before, hp1g_during, freq_mhz} from sweep stdout."""
    got = {}
    for path in paths:
        txt = pathlib.Path(path).read_text(errors="replace").replace("\x1b", "")
        # Split on the mode headers so each row is attributed to the right mode.
        marks = [(m.start(), m.group(1), m.group(2)) for m in HDR_RE.finditer(txt)]
        for i, (pos, mode, tag) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(txt)
            for q, before, during, freq, insns in ROW_RE.findall(txt[pos:end]):
                rec = {"hp1g_before": int(before), "hp1g_during": int(during)}
                if freq.isdigit():
                    rec["freq_mhz"] = int(freq)
                if insns.isdigit():
                    rec["insns"] = int(insns)
                got[(tag, mode, int(q))] = rec
    return got


SIDE = sidecar(sys.argv[3:])
incomplete = []
multiplexed = []
malformed = []

# Merge, do not clobber. Each invocation sees only the logs of the sweep that
# just ran, so rebuilding the file from scratch would silently delete every
# condition whose log directory is no longer on disk -- which is what happened
# once here, taking four historical conditions with it (they were recoverable
# from git; an uncommitted arm would not have been). A condition is now only
# rewritten when logs for it are actually found.
out = json.loads(OUT.read_text()) if OUT.exists() else {}
for cond, prefix in CONDS.items():
    fresh = {}
    # "none" is the floor arm (-m none). It is listed last and pruned below
    # when empty, so that adding it does not stamp an empty "none" key onto
    # every historical condition and make them look like they were swept for it.
    for mode in ("maglev", "dramblast", "none"):
        fresh[mode] = {}
        for q in range(1, 11):
            # run_matrix.sh puts each arm's logs in its own subdirectory, while
            # the earlier single-arm sweeps wrote them flat. Accept either, so
            # one extractor invocation covers the whole tree instead of needing
            # to be re-run once per arm with a different root (which is how the
            # depth arms were silently missing from the first analysis).
            name = f"{prefix}_{mode}_q{q}.log"
            log = SP / name
            if not log.exists():
                log = SP / prefix / name
            if not log.exists():
                continue
            txt = log.read_text(errors="replace").replace("\x1b", "")
            # Skip a run that has not finished. l2fwd prints its final summary
            # block (Minimum/Maximum/Average) once, after the last sample, so
            # its absence means the process is still running or died partway.
            # Without this check, extracting while a sweep is in flight silently
            # admits a truncated run: the per-second lines are already there, so
            # steady_mpps and cycles_per_pkt come out looking entirely
            # reasonable while describing half an experiment.
            if "Average:" not in txt:
                incomplete.append(log.name)
                continue
            rec = {}
            for key, pat in pats.items():
                m = re.findall(pat, txt)
                if m:
                    rec[key] = float(m[-1]) if key in ("avg", "min", "max") else int(m[-1])
            # full-precision, cold-sample-excluded steady state
            samples = [float(x) for x in SAMPLE_RE.findall(txt)]
            if len(samples) >= 3:
                rec["steady_mpps"] = round(statistics.median(samples[1:]), 2)
                rec["steady_min"] = round(min(samples[1:]), 2)
                rec["steady_max"] = round(max(samples[1:]), 2)
                rec["cold_sample0_mpps"] = round(samples[0], 2)
                rec["n_samples"] = len(samples)
            rec.update(SIDE.get((prefix, mode, q), {}))
            # per-run PMU sidecar written by sweep.sh, one `value,,event,...`
            # line per counter. Carried through verbatim so a question asked
            # later can be answered from data already on disk.
            perf = log.with_suffix(".perf")
            if perf.exists():
                got, dropped, bad_rows = parse_perf(
                    perf.read_text(errors="replace"))
                rec.update({"pmu_" + k: v for k, v in got.items()})
                multiplexed += [f"{log.name}:{e} at {pct:.2f}%"
                                for e, pct in dropped]
                malformed += [f"{log.name}: {why}\n        {ln}"
                              for ln, why in bad_rows]
            if rec:
                fresh[mode][str(q)] = rec
    if not fresh.get("none"):
        fresh.pop("none", None)
    if any(fresh[m] for m in fresh):
        out[cond] = fresh

OUT.write_text(json.dumps(out, indent=4) + "\n")
print(f"wrote {OUT}")
if malformed:
    # Louder than the multiplexing report, and deliberately: a multiplexed
    # reading means the machine was busy, a malformed one means this parser
    # was reading the wrong columns and nothing it produced can be trusted.
    print(f"  *** {len(malformed)} MALFORMED perf row(s) -- the CSV columns are "
          f"shifted, so the guard was not actually applied to them:")
    for m in malformed[:6]:
        print(f"      {m}")
    print("      Cause is almost always a raw event spec passed without "
          "`name=`: the spec")
    print("      contains commas, so it splits across several -x, columns. "
          "See perf_csv.py.")
if multiplexed:
    print(f"  *** DROPPED {len(multiplexed)} MULTIPLEXED counter reading(s) -- "
          f"these were time-shared and scaled, not measured:")
    for m in sorted(multiplexed)[:8]:
        print(f"      {m}")
    if len(multiplexed) > 8:
        print(f"      ... and {len(multiplexed) - 8} more")
    print("      Six events fit only because `cycles` and `instructions` land "
          "on fixed-function")
    print("      counters; with SMT enabled a logical CPU gets FOUR "
          "general-purpose counters,")
    print("      so four RAW events is the ceiling for a single pass.")
if incomplete:
    print(f"  SKIPPED {len(incomplete)} unfinished run(s): "
          + ", ".join(sorted(incomplete)[:6])
          + (" ..." if len(incomplete) > 6 else ""))
for cond in out:
    for mode in out[cond]:
        n = len(out[cond][mode])
        peak = max((r.get("avg", 0) for r in out[cond][mode].values()), default=0)
        print(f"  {cond:18s} {mode:10s} {n} points, peak {peak} Mpps")
