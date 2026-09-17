#!/usr/bin/env python3
"""Turn the saturation sweep's raw counters into utilisations, and plot them.

Every row is a fraction of a ceiling, so the figure answers one question: as
cores are added, which resource reaches 100% first? A per-core resource holds
its utilisation flat as cores are added; a shared one climbs.

Ceilings come from counter_groups.sh (hardware configuration) or from
validate_counters.sh (measured). Where a ceiling is not known, the metric is
reported as a rate rather than a fraction and is drawn on the second panel, so
that nothing is plotted as a percentage of a number that was guessed.

    python3 l2fwd/analyse_saturation.py <outdir>  -> docs/saturation.svg
"""
import pathlib
import re
import sys

from perf_csv import parse_perf as _parse_perf_guarded   # the canonical guard

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
SURFACE, INK, INK_2, GRID = "#fbfaf7", "#1a1a1a", "#5a5a5a", "#e0ddd6"
PALETTE = ["#12707f", "#bb551c", "#4a7a3a", "#7a4a8a", "#a8324a", "#3a5a8a"]

# Nominal only: 8 channels x DDR5-4800 x 8 B. It describes the part number, not
# this machine, and is the last-resort denominator. dram_ceiling() below prefers
# anything measured.
DRAM_PEAK_GBS = 307.2
DRAM_CEIL = DRAM_PEAK_GBS
CEIL_SRC = "unset"


def dram_ceiling():
    """The denominator for DRAM utilisation, and the reason it is the one it is.

    There is no measured 24-thread MIXED ceiling for this machine and none is
    extrapolated -- see the header of .dram_ceiling. Order of preference:

      1. a measured mixed ceiling, if one ever becomes available
      2. the peer session's 24-thread READ ceiling, which is a lower bound and
         is named as such, so utilisations against it read as "at least"
      3. the datasheet figure, which describes a part number rather than this
         machine, and is the worst of the three

    Whichever is used, the caller prints its name beside every percentage.
    """
    f = pathlib.Path(__file__).resolve().parent / ".dram_ceiling"
    if f.exists():
        txt = f.read_text()
        m = re.search(r"^DRAM_ACHIEVED_GBS=([0-9.]+)", txt, re.M)
        if m and float(m.group(1)) > 0:
            return float(m.group(1)), "measured mixed ceiling"
        m = re.search(r"^PEER_READ_24=([0-9.]+)", txt, re.M)
        if m:
            return float(m.group(1)), "peer 24-thread READ ceiling, a LOWER BOUND"
    return DRAM_PEAK_GBS, "datasheet nominal (not this machine)"
PCIE_GBS = 31.5
LINE_RATE_MPPS = 96.15
FRAME_BYTES = 110


def sh_const(name, default):
    """Read a constant out of counter_groups.sh so the two cannot drift."""
    p = pathlib.Path(__file__).resolve().parent / "counter_groups.sh"
    m = re.search(r"^%s=([0-9.]+)" % name, p.read_text(), re.M)
    return float(m.group(1)) if m else default


def _base(ev):
    """perf prints raw events as `uncore_imc_0/event=0x05,...,name=cas_rd_0/`
    or as a bare name; reduce both to the name that was asked for."""
    m = re.search(r"name=([A-Za-z0-9_]+)", ev)
    if m:
        return m.group(1)
    return ev.strip().strip("/").split("/")[-1] or ev.strip()


def parse_perf(path):
    """-> ({event: value}, [problems]); delegates the guard to perf_csv.

    This deliberately does NOT do its own column arithmetic. An earlier version
    did, read field 5 as the enabled percentage, and so reported every
    metric-bearing event as multiplexed -- top-down fractions, insn-per-cycle --
    while passing any genuinely multiplexed event whose metric column happened
    to be empty. perf_csv.parse_perf already had that right, already separates
    MALFORMED from MULTIPLEXED, and is already fired against real hardware
    multiplexing by test_perf_guard.py --real. Reimplementing it here made this
    the fourth appearance of the same bug in one investigation.

    The one thing added on top is name normalisation: this study's raw specs are
    per-controller (`cas_rd_0` .. `cas_rd_7`), so the guarded parser's keys are
    remapped through _base() for the caller's convenience.
    """
    if not path.exists():
        return None, ["missing"]
    got, dropped, malformed = _parse_perf_guarded(path.read_text())
    problems = ["%s@%.2f%%" % (e, p) for e, p in dropped]
    problems += ["MALFORMED: %s" % why for _line, why in malformed]
    return {_base(k): v for k, v in got.items()}, problems


ROW = re.compile(r"(?:arm=(\S+)\s+)?q=(\d+)\s+grp=(\S+)\s+workers=(\d+)\s+"
                 r"avg=([\d.]+|NA)\s+cyc=(\d+|NA)\s+batch=(\d+|NA)\s+"
                 r"missed=(\d+|NA)")
ARM_FILTER = __import__("os").environ.get("ARM_FILTER")

# What each group MUST contain for its run to count.
#
# perf_csv.parse_perf reports what it can read; an event that the PMU refused
# outright is not a row it can read, so `<not supported>` is skipped silently --
# correctly, since the parser's contract is to parse what is there. The caller
# has to assert what it EXPECTED, and until this map existed nothing did.
#
# The failure this closes is partial and therefore the nastiest shape: the
# un-braced top-down group returns a real `slots` count of 21,979,161,492 with
# all four metrics reading `<not supported>` at 100.00 enabled. Every guard
# passes -- nothing multiplexed, nothing malformed, a healthy-looking group --
# and four numbers are simply absent. A presence check keyed on the group
# ("did this produce anything?") sees success. It has to be per event.
EXPECTED = {
    "topdown1": ["slots", "topdown-retiring", "topdown-bad-spec",
                 "topdown-fe-bound", "topdown-be-bound"],
    "topdown2": ["slots", "topdown-mem-bound", "topdown-fetch-lat",
                 "topdown-heavy-ops", "topdown-br-mispredict"],
    "mlp":      ["cycles", "l1d_pend_miss_pending",
                 "l1d_pend_miss_pending_cycles"],
    "fb":       ["cycles", "l1d_pend_miss_fb_full",
                 "offcore_reqs_outstanding_data_rd"],
    "tlbmem":   ["cycles", "instructions", "dtlb_walk_completed",
                 "dtlb_walk_active", "stalls_l3_miss", "LLC-load-misses"],
    # bw is checked structurally instead: eight controllers, read and write.
    "bw":       ["cas_rd_%d" % i for i in range(8)]
              + ["cas_wr_%d" % i for i in range(8)],
}


def main(outdir):
    d = pathlib.Path(outdir)
    summary = d / "summary.txt"
    if not summary.exists():
        sys.exit("no summary.txt in %s" % outdir)

    conds = {}          # q -> {group: perf dict}
    meta = {}           # q -> {avg, cyc, batch, missed, workers}
    for line in summary.read_text().splitlines():
        m = ROW.search(line)
        if not m:
            continue
        arm = m.group(1) or "default"
        if ARM_FILTER and arm != ARM_FILTER:
            continue
        q, grp = int(m.group(2)), m.group(3)
        meta.setdefault(q, dict(workers=int(m.group(4)),
                                avg=None if m.group(5) == "NA" else float(m.group(5)),
                                cyc=None if m.group(6) == "NA" else int(m.group(6)),
                                batch=None if m.group(7) == "NA" else int(m.group(7)),
                                missed=None if m.group(8) == "NA" else int(m.group(8))))
        # sidecar name mirrors saturation.sh
        for p in d.glob("*%s*_q%d_%s.perf" % (arm if arm != "default" else "", q, grp)):
            vals, problems = parse_perf(p)
            if vals is not None:
                missing = [e for e in EXPECTED.get(grp, []) if e not in vals]
                if missing:
                    problems = list(problems) + [
                        "ABSENT (PMU refused, or the group was mis-specified): "
                        + ", ".join(missing)]
            conds.setdefault(q, {})[grp] = (vals, problems)

    if not conds:
        sys.exit("no per-group counter files found")

    # Report every problem before any number is printed. A run with an absent
    # event is not a run with a small gap in it -- the fraction that event
    # feeds is simply missing, and a figure drawn from the rest would look
    # complete.
    bad = [(q, g, p) for q in sorted(conds) for g, (_v, p) in conds[q].items() if p]
    if bad:
        print("COUNTER PROBLEMS -- these runs are not usable as measured:")
        for q, g, probs in bad:
            for pr in probs:
                print("  q=%-3d %-9s %s" % (q, g, pr))
        print()

    window = float(__import__("os").environ.get("PERF_WINDOW", "8"))
    global DRAM_CEIL, CEIL_SRC
    DRAM_CEIL, CEIL_SRC = dram_ceiling()
    print("DRAM denominator: %.1f GB/s -- %s" % (DRAM_CEIL, CEIL_SRC))
    print("(absolute GB/s is the primary figure; the percentage is secondary)\n")
    qs = sorted(conds)

    def get(q, grp, name, prefix=False):
        """Exact event-name lookup, or a sum over a prefix.

        Exact by default, deliberately. An earlier version matched substrings,
        which silently summed `cycles` with `pending_cycles`, and
        `l1d_pend_miss_pending` with `l1d_pend_miss_pending_cycles` -- i.e. it
        broke precisely the MLP ratio it was there to compute, and would have
        produced a plausible wrong number rather than an error. `prefix=True`
        is used only where a sum really is wanted, across the eight memory
        controllers.
        """
        entry = conds.get(q, {}).get(grp)
        if not entry or not entry[0]:
            return None
        vals = entry[0]
        if prefix:
            tot = sum(v for ev, v in vals.items() if _base(ev).startswith(name))
            return tot if any(_base(ev).startswith(name) for ev in vals) else None
        for ev, v in vals.items():
            if _base(ev) == name:
                return v
        return None

    def frac(a, b):
        return None if (a is None or not b) else a / b

    series = {}     # label -> {q: fraction}
    for q in qs:
        m = meta[q]
        slots = get(q, "topdown1", "slots")
        cyc_td = get(q, "mlp", "cycles")
        cyc_fb = get(q, "fb", "cycles")
        cyc_tm = get(q, "tlbmem", "cycles")

        def put(label, v):
            if v is not None:
                series.setdefault(label, {})[q] = v

        put("core: retiring / slots", frac(get(q, "topdown1", "topdown-retiring"), slots))
        put("core: backend-bound", frac(get(q, "topdown1", "topdown-be-bound"), slots))
        put("core: memory-bound", frac(get(q, "topdown2", "topdown-mem-bound"), slots))
        put("L1D fill buffers full", frac(get(q, "fb", "l1d_pend_miss_fb_full"), cyc_fb))
        put("stalled on L3 miss", frac(get(q, "tlbmem", "stalls_l3_miss"), cyc_tm))
        put("page walker active", frac(get(q, "tlbmem", "dtlb_walk_active"), cyc_tm))

        # Raw CAS counters return a count of 64 B cache lines; there is no
        # .scale file on this part, so the conversion is explicit here.
        rd = get(q, "bw", "cas_rd_", prefix=True)
        wr = get(q, "bw", "cas_wr_", prefix=True)
        if rd is not None and wr is not None:
            gbs = (rd + wr) * 64.0 / window / 1e9
            put("DRAM bandwidth (of %s)" % CEIL_SRC.split(",")[0], gbs / DRAM_CEIL)
            series.setdefault("_dram_gbs", {})[q] = gbs
        if m["avg"]:
            put("link rate (of line rate)", m["avg"] / LINE_RATE_MPPS)
            put("PCIe (of Gen4 x16)",
                m["avg"] * 1e6 * FRAME_BYTES / 1e9 / PCIE_GBS)
        # MLP is a count, not a fraction: reported separately.
        p_ = get(q, "mlp", "l1d_pend_miss_pending")
        pc = get(q, "mlp", "l1d_pend_miss_pending_cycles")
        if p_ and pc:
            series.setdefault("_mlp", {})[q] = p_ / pc

    # ---- text table ----
    print("%-28s %s" % ("resource (utilisation)",
                        " ".join("%8s" % ("q=%d" % q) for q in qs)))
    for lab in sorted(k for k in series if not k.startswith("_")):
        print("%-28s %s" % (lab, " ".join(
            "%7.1f%%" % (series[lab][q] * 100) if q in series[lab] else "      --"
            for q in qs)))
    for lab, unit in (("_dram_gbs", "GB/s"), ("_mlp", "misses")):
        if lab in series:
            print("%-28s %s" % (lab[1:] + " (" + unit + ")", " ".join(
                "%8.1f" % series[lab][q] if q in series[lab] else "      --"
                for q in qs)))
    print()
    for q in qs:
        m = meta[q]
        if m["missed"] is not None and m["avg"]:
            print("q=%-3d workers=%-3d %.2f Mpps  batch=%s  RX-Missed=%d"
                  % (q, m["workers"], m["avg"], m["batch"], m["missed"]))

    # ---- figure ----
    keys = [k for k in sorted(series) if not k.startswith("_")]
    PW, PH, PAD_L, PAD_T = 470, 300, 72, 62
    LEG_H = 22 * ((len(keys) + 1) // 2) + 18
    W, H = PAD_L + PW + 210, PAD_T + PH + LEG_H + 56
    o = []
    a = o.append
    a('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
      'height="%d" font-family="system-ui,-apple-system,Segoe UI,sans-serif">'
      % (W, H, W, H))
    a('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    a('<text x="%d" y="26" font-size="15" font-weight="600" fill="%s">'
      'Which resource saturates first?</text>' % (PAD_L, INK))
    a('<text x="%d" y="44" font-size="12" fill="%s">utilisation against each '
      'resource&#8217;s own ceiling, as worker cores are added</text>'
      % (PAD_L, INK_2))

    hi = max(1.0, max(v for k in keys for v in series[k].values()))

    def sx(q):
        i = qs.index(q)
        return PAD_L + (i + 0.5) / len(qs) * PW

    def sy(v):
        return PAD_T + PH - (v / hi) * PH

    for frac_ in (0, 0.25, 0.5, 0.75, 1.0):
        v = frac_ * hi
        a('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
          % (PAD_L, sy(v), PAD_L + PW, sy(v), GRID))
        a('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">'
          '%d%%</text>' % (PAD_L - 7, sy(v) + 3, INK_2, round(v * 100)))
    a('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" '
      'stroke-width="1.5" stroke-dasharray="5,3"/>'
      % (PAD_L, sy(1.0), PAD_L + PW, sy(1.0), "#a8324a"))
    a('<text x="%d" y="%.1f" font-size="10" fill="%s">ceiling</text>'
      % (PAD_L + PW + 5, sy(1.0) + 3, "#a8324a"))

    for q in qs:
        a('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">'
          '%d</text>' % (sx(q), PAD_T + PH + 16, INK_2, meta[q]["workers"]))
    a('<text x="%.1f" y="%d" font-size="11" fill="%s" text-anchor="middle">'
      'worker cores</text>' % (PAD_L + PW / 2, PAD_T + PH + 34, INK_2))

    for i, k in enumerate(keys):
        col = PALETTE[i % len(PALETTE)]
        pts = [(sx(q), sy(series[k][q])) for q in qs if q in series[k]]
        if len(pts) > 1:
            a('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>'
              % (col, " ".join("%.1f,%.1f" % p for p in pts)))
        for p in pts:
            a('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>' % (p[0], p[1], col))

    ly = PAD_T + PH + 48
    for i, k in enumerate(keys):
        col = PALETTE[i % len(PALETTE)]
        cx = PAD_L + (i % 2) * 330
        cy = ly + (i // 2) * 22
        a('<rect x="%d" y="%d" width="10" height="10" fill="%s"/>' % (cx, cy, col))
        a('<text x="%d" y="%d" font-size="11" fill="%s">%s</text>'
          % (cx + 15, cy + 9, INK_2, k.replace("&", "&amp;")))
    a("</svg>")
    out = DOCS / "saturation.svg"
    out.write_text("\n".join(o))
    print("\nwrote %s" % out)


if __name__ == "__main__":
    main(sys.argv[1])
