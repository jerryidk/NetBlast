"""Extract measured sweep results into docs/results_reproduced.json, keyed by offered load.

Parses the per-run logs written by sweep.sh.

Usage: python3 extract_results.py <log_dir> ../docs/results_reproduced.json

Conditions:
  linerate_2tx_instr logs tagged "instr"  -- 2 TX gen, line rate, post-7ebf038 (float stats),
                                            plus per-run 1 GiB hugepage and delivered-frequency samples
  linerate_93mpps  logs tagged "linerate" -- generator -l 0-16 (8 TX cores), 100 GbE line rate
  linerate_2tx_gen logs tagged "gen2tx"   -- generator -l 0-4  (2 TX cores), 100 GbE line rate
  capped_72mpps    logs tagged "sweep"    -- generator -l 0-2  (1 TX core),  72 Mpps
"""
import json, re, sys, pathlib, statistics

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
CONDS = {"linerate_2tx_instr": "instr", "linerate_93mpps": "linerate",
         "linerate_2tx_gen": "gen2tx", "capped_72mpps": "sweep"}

out = {}
for cond, prefix in CONDS.items():
    out[cond] = {}
    for mode in ("maglev", "dramblast"):
        out[cond][mode] = {}
        for q in range(1, 11):
            log = SP / f"{prefix}_{mode}_q{q}.log"
            if not log.exists():
                continue
            txt = log.read_text(errors="replace").replace("\x1b", "")
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
            if rec:
                out[cond][mode][str(q)] = rec

OUT.write_text(json.dumps(out, indent=4) + "\n")
print(f"wrote {OUT}")
for cond in out:
    for mode in out[cond]:
        n = len(out[cond][mode])
        peak = max((r["avg"] for r in out[cond][mode].values()), default=0)
        print(f"  {cond:18s} {mode:10s} {n} points, peak {peak} Mpps")
