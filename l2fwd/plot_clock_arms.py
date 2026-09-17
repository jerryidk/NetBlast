"""Plot the two clock arms: cores pinned at 2.100 GHz vs. turbo.

Reads  docs/results_reproduced.json, conditions `pinned_2100mhz` and `turbo_instr`.
Writes docs/clock_arms.png

The two arms differ ONLY in the core clock (l2fwd/set_clock.sh); C-states,
irqbalance, nmi_watchdog, THP defrag and the cpuset partition are held fixed in
both, so this is a single-variable contrast. See docs/INVESTIGATION.md,
"Pre-registered: what the pinned re-baseline must show".

Run:  nix develop .. -c python3 plot_clock_arms.py
"""

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Design tokens, rcParams and the two drawing primitives are shared with
# plot_sweep.py; see l2fwd/plotlib.py.
from plotlib import (DOCS, INK, INK_2, INK_MUTED, BLUE, ORANGE, RC,  # noqa: E402
                     line, style)

TSC_MHZ = 2100.0      # invariant TSC rate; identical in BOTH arms
GEN_CEILING = 93.28   # Mpps offered, 100 GbE line rate at 110-byte frames

plt.rcParams.update(RC)


def rows(cond, mode):
    """[(q, record)] sorted by q, only queue counts present."""
    return [(q, cond[mode][str(q)]) for q in sorted(int(k) for k in cond.get(mode, {}))]


def core_cycles(rec):
    """l2fwd prints TSC ticks. Convert to core cycles using this run's OWN
    measured delivered frequency -- never a hardcoded ratio, and never sysfs,
    which reports the P-state request rather than delivery on this box."""
    freq = rec.get("freq_mhz")
    if freq is None or "cycles_per_pkt" not in rec:
        return None
    return rec["cycles_per_pkt"] * freq / TSC_MHZ


def main():
    allc = json.loads((DOCS / "results_reproduced.json").read_text())
    pinned, turbo = allc.get("pinned_2100mhz", {}), allc.get("turbo_instr", {})

    fig, axes = plt.subplots(1, 2, figsize=(13.4, 5.4))

    # ---- Panel A: the knee moves right when the clock is pinned -------------
    ax = axes[0]
    ax.axhline(GEN_CEILING, color=INK_MUTED, linewidth=1.2, linestyle=(0, (5, 4)), zorder=2)
    ax.text(0.8, GEN_CEILING + 2.2, "offered load: 93.3 Mpps (line rate)",
            fontsize=8.5, color=INK_MUTED, ha="left")
    for cond, dashed, tag in ((turbo, True, "turbo"), (pinned, False, "2.100 GHz")):
        for mode, color in (("dramblast", BLUE), ("maglev", ORANGE)):
            r = rows(cond, mode)
            if r:
                line(ax, [q for q, _ in r], [v["steady_mpps"] for _, v in r],
                     color, f"{mode}, {tag}", dashed)
    style(ax, "Pinning the clock moves the saturation knee right",
          "RX/TX queue pairs  (-q)", "Forwarded  (Mpps, median of warm samples)")
    ax.set_xticks(range(1, 11))
    ax.set_xlim(0.7, 10.6)
    ax.set_ylim(0, 110)
    ax.legend(loc="lower right", frameon=False, fontsize=8.5, labelcolor=INK_2)

    # ---- Panel B: the collapse test -----------------------------------------
    # Under `cycles/packet = P + C/B` this is a straight line: intercept P is
    # per-packet work, slope C is the per-burst cost. Core cycles are
    # clock-invariant for CPU-bound work, so if the cost is CPU work both arms
    # land on ONE line. Vertical separation between arms is the memory-latency
    # fraction, which is fixed in nanoseconds and does not scale with the clock.
    ax = axes[1]
    for cond, dashed, tag in ((turbo, True, "turbo"), (pinned, False, "2.100 GHz")):
        for mode, color in (("dramblast", BLUE), ("maglev", ORANGE)):
            pts = [(1.0 / v["rx_batch"], core_cycles(v))
                   for _, v in rows(cond, mode)
                   if v.get("rx_batch") and core_cycles(v) is not None]
            pts.sort()
            if pts:
                line(ax, [x for x, _ in pts], [y for _, y in pts],
                     color, f"{mode}, {tag}", dashed)
    style(ax, "Cost against burst size, both arms in core cycles",
          "1 / average RX burst size   (bursts per packet)",
          "Core cycles per forwarded packet")
    # Lower right: the upper left is where maglev's flat line sits, and the
    # upper right is the end of dramblast's rise. The wedge under dramblast is
    # the only empty region at every data set size.
    ax.legend(loc="lower right", frameon=False, fontsize=8.5, labelcolor=INK_2)

    fig.suptitle("Two clock arms: comparable versus representative",
                 fontsize=14.5, color=INK, x=0.05, ha="left", y=0.98,
                 fontweight="medium")
    fig.text(0.05, 0.915,
             "Same binary, same cpuset, same C-state / irqbalance / THP settings — only the core clock differs.\n"
             "Ticks are converted to core cycles per point using that run's own perf-measured frequency, never a fixed ratio.",
             fontsize=9.5, color=INK_2, ha="left", va="top", linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.875))
    out = DOCS / "clock_arms.png"
    fig.savefig(out, dpi=200)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
