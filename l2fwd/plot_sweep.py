"""Plot the queue-pair sweep: committed results vs. measured reproduction.

Reads  docs/results.json             (committed, produced by pre-91d2c14 code)
       docs/results_reproduced.json  (measured, corrected N+1 lcore invocation,
                                      keyed by offered load)
Writes docs/queue_sweep_reproduction.png
       docs/per_packet_cost.png
       docs/generator_sensitivity.png

Run:  nix develop .. -c python3 plot_sweep.py
"""

import json
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"

# --- design tokens -----------------------------------------------------------
# Categorical slots 1 and 2 of the reference palette, used unmodified.
# Validated light-mode: CVD dE 24.7 (protan), normal-vision dE 33.6, contrast >= 3:1.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
INK_MUTED = "#8a8983"
GRID = "#e4e3de"
BLUE = "#2a78d6"
ORANGE = "#eb6834"

COND = "linerate_93mpps"   # 8 TX cores, 16M flows, 100 GbE line rate
GEN_CEILING = 93.28        # Mpps offered: physical line rate for 110-byte frames

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "DejaVu Sans",
    "text.color": INK,
    "axes.labelcolor": INK_2,
    "xtick.color": INK_2,
    "ytick.color": INK_2,
    "axes.edgecolor": GRID,
    "axes.linewidth": 1.0,
    "xtick.major.size": 0,
    "ytick.major.size": 0,
})


def style(ax, title, xlabel, ylabel):
    ax.set_title(title, fontsize=12, color=INK, pad=12, loc="left", fontweight="medium")
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.grid(True, color=GRID, linewidth=0.8, alpha=0.9)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_xticks(range(1, 11))
    ax.tick_params(labelsize=9)


def line(ax, xs, ys, color, label):
    """2px line, >=8px markers with a 2px surface ring."""
    ax.plot(xs, ys, color=color, linewidth=2.0, marker="o", markersize=6,
            markerfacecolor=color, markeredgecolor=SURFACE, markeredgewidth=2.0,
            label=label, zorder=3, clip_on=False)


def band(ax, xs, lo, hi, color):
    ax.fill_between(xs, lo, hi, color=color, alpha=0.14, linewidth=0, zorder=1)


def series(d, mode, key):
    qs = sorted(int(q) for q in d[mode])
    return qs, [d[mode][str(q)][key] for q in qs]


def main():
    old = json.loads((DOCS / "results.json").read_text())
    new = json.loads((DOCS / "results_reproduced.json").read_text())[COND]

    # ---- Figure 1: does the collapse reproduce? -----------------------------
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2), sharey=True)

    for ax, mode in zip(axes, ("dramblast", "maglev")):
        qo, ao = series(old, mode, "avg")
        _, lo_o = series(old, mode, "min")
        _, hi_o = series(old, mode, "max")
        # Measured series uses steady_mpps, not l2fwd's own Average: the latter
        # is a mean of floor()ed samples including a cold sample 0 (see
        # extract_results.py). Band is the true spread over the warm samples.
        qn, an = series(new, mode, "steady_mpps")
        _, lo_n = series(new, mode, "steady_min")
        _, hi_n = series(new, mode, "steady_max")

        ax.axhline(GEN_CEILING, color=INK_MUTED, linewidth=1.2, linestyle=(0, (5, 4)),
                   zorder=2)
        ax.text(0.8, GEN_CEILING + 2.5, "offered load: 93.3 Mpps (line rate)", fontsize=8.5,
                color=INK_MUTED, ha="left")

        band(ax, qo, lo_o, hi_o, ORANGE)
        band(ax, qn, lo_n, hi_n, BLUE)
        line(ax, qo, ao, ORANGE, "committed results.json (avg, min/max band)")
        line(ax, qn, an, BLUE, "measured (steady state, warm samples)")

        # direct labels at the line ends, both placed above their point
        ax.annotate("committed", (qo[-1], ao[-1]), textcoords="offset points",
                    xytext=(-8, 12), fontsize=9, color=INK_2, ha="right")
        ax.annotate("measured", (qn[-1], an[-1]), textcoords="offset points",
                    xytext=(-8, 11), fontsize=9, color=INK_2, ha="right")

        style(ax, mode, "RX/TX queue pairs  (-q)",
              "Forwarded  (Mpps)" if mode == "dramblast" else "")
        ax.set_xlim(0.7, 10.6)
        ax.set_ylim(0, 125)

    axes[0].legend(loc="upper left", frameon=False, fontsize=9, ncol=1,
                   labelcolor=INK_2)
    fig.suptitle("The collapse does not reproduce at any queue-pair count",
                 fontsize=14.5, color=INK, x=0.055, ha="left", y=0.98,
                 fontweight="medium")
    fig.text(0.055, 0.925,
             "Committed data came from pre-91d2c14 code, a different threading model.\n"
             "Measured: N+1 lcores per N queues, generator at 100 GbE line rate, median of warm samples "
             "(cold first sample excluded).",
             fontsize=9.5, color=INK_2, ha="left", va="top", linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.875))
    out1 = DOCS / "queue_sweep_reproduction.png"
    fig.savefig(out1, dpi=200)
    print(f"wrote {out1}")

    # ---- Figure 2: the real queue-count-dependent cost ----------------------
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2))

    ax = axes[0]
    for mode, color in (("dramblast", BLUE), ("maglev", ORANGE)):
        qs, ys = series(new, mode, "cycles_per_pkt")
        line(ax, qs, ys, color, mode)
        ax.annotate(mode, (qs[-1], ys[-1]), textcoords="offset points",
                    xytext=(-6, 10), fontsize=9, color=INK_2, ha="right")
    # l2fwd prints this as "Cycle per fwd packet" but it is rte_rdtsc() deltas,
    # and this CPU has constant_tsc/nonstop_tsc with the TSC pinned at 2.1 GHz
    # while cores boost to 3.7 GHz. So these are TSC ticks (i.e. time), NOT core
    # cycles -- they understate true cycles by the boost ratio. Relative
    # comparisons between modes at the same queue count remain valid.
    style(ax, "Per-packet cost rises with queue count",
          "RX/TX queue pairs  (-q)", "TSC ticks per forwarded packet  (2.1 GHz)")
    ax.set_xlim(0.7, 10.6)
    ax.set_ylim(0, 200)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK_2)

    ax = axes[1]
    # The curves share the 64-packet plateau at low q and converge near zero at
    # high q, so anchor each label at its own uncrowded point rather than at a
    # common x or at the line ends.
    for mode, color, anchor_q in (("dramblast", BLUE, 5), ("maglev", ORANGE, 3)):
        qs, ys = series(new, mode, "rx_batch")
        line(ax, qs, ys, color, mode)
        i = qs.index(anchor_q)
        ax.annotate(mode, (qs[i], ys[i]), textcoords="offset points",
                    xytext=(9, 9), fontsize=9, color=INK_2, ha="left")
    style(ax, "...because each poll returns fewer packets",
          "RX/TX queue pairs  (-q)", "Average RX burst size  (packets)")
    ax.set_xlim(0.7, 10.6)
    ax.set_ylim(0, 70)
    ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK_2)

    fig.suptitle("A fixed per-burst cost, amortised over shrinking bursts",
                 fontsize=14.5, color=INK, x=0.055, ha="left", y=0.98,
                 fontweight="medium")
    fig.text(0.055, 0.915,
             "Line-rate load spread over more queues. dramblast rises 3.2x, maglev 1.3x — isolating a per-burst, "
             "not per-packet, cost. TSC is invariant at 2.1 GHz here, so ticks are time, not core cycles.",
             fontsize=9.5, color=INK_2, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    out2 = DOCS / "per_packet_cost.png"
    fig.savefig(out2, dpi=200)
    print(f"wrote {out2}")

    # ---- Figure 3: does the generator configuration change the answer? -------
    allc = json.loads((DOCS / "results_reproduced.json").read_text())
    a, b = allc["linerate_93mpps"], allc["linerate_2tx_gen"]

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2), sharey=True)
    worst = 0.0
    for ax, mode in zip(axes, ("dramblast", "maglev")):
        qa, ya = series(a, mode, "steady_mpps")
        qb, yb = series(b, mode, "steady_mpps")
        dev = max(abs(p - q) / q * 100 for p, q in zip(ya, yb))
        worst = max(worst, dev)

        ax.axhline(GEN_CEILING, color=INK_MUTED, linewidth=1.2,
                   linestyle=(0, (5, 4)), zorder=2)
        # thick pale line underneath, thin line with markers on top: agreement
        # reads as the thin curve tracking the centre of the thick one
        ax.plot(qa, ya, color=BLUE, linewidth=7.0, alpha=0.25, zorder=2,
                solid_capstyle="round", label="8 TX-core generator")
        line(ax, qb, yb, ORANGE, "2 TX-core generator")
        ax.annotate(f"max deviation {dev:.1f}%", (qb[-1], yb[-1]),
                    textcoords="offset points", xytext=(-8, -20), fontsize=9,
                    color=INK_2, ha="right")

        style(ax, mode, "RX/TX queue pairs  (-q)",
              "Forwarded  (Mpps)" if mode == "dramblast" else "")
        ax.set_xlim(0.7, 10.6)
        ax.set_ylim(0, 105)

    axes[0].legend(loc="lower right", frameon=False, fontsize=9, labelcolor=INK_2)
    fig.suptitle("Generator core count does not change the result",
                 fontsize=14.5, color=INK, x=0.055, ha="left", y=0.98,
                 fontweight="medium")
    fig.text(0.055, 0.925,
             "Both generators offer 93.28 Mpps line rate and 16.8M flows; only TX core count differs\n"
             "(-r compensated, since it is per TX core). Curves are steady-state medians of warm samples.",
             fontsize=9.5, color=INK_2, ha="left", va="top", linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.875))
    out3 = DOCS / "generator_sensitivity.png"
    fig.savefig(out3, dpi=200)
    print(f"wrote {out3}  (worst deviation across all points: {worst:.2f}%)")


if __name__ == "__main__":
    main()
