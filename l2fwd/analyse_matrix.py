"""Read the run_matrix.sh conditions and answer the three questions they pose.

  1. CROSSOVER   does the page backing explain the per-packet gap between modes?
  2. ALLOCATOR   is the per-burst cost C the aligned_alloc/free round trip?
  3. DEPTH       is C instead the prefetch pipeline's ramp?

Each block prints the fitted P and C for its conditions and then states what the
numbers rule in or out, against a prediction written down before the runs.

PREDICTIONS, recorded here so they are not adjusted afterwards:

  Crossover. dramblast ships on 1 GiB pages and maglev on 2 MiB THP, and 8 GiB
  on 2 MiB pages is 4096 pages against a ~2048-entry STLB. If address
  translation is what separates the two modes, moving maglev to 1 GiB must drop
  its P substantially toward dramblast's, and moving dramblast to 4 KiB must
  raise its P a long way. Page size acts per access, so it should move P and
  leave C alone. If instead P barely moves, the gap is the prefetch pipeline and
  not the TLB, and §5.2's confound is real but small.

  Allocator. C must be LINEAR in the number of alloc/free pairs. The slope is
  what one pair costs on this machine. The intercept at -1 pair is whatever the
  per-burst cost is that has nothing to do with the allocator. If the shipped
  C (~645 cycles) is mostly allocator, the hoisted arm collapses toward zero; if
  the slope is ~60 cycles/pair as the literature figure implies, the allocator
  is ~9% of C and the intercept stays near 590.

  Depth. If C is the pipeline ramp, C falls as depth falls and P rises, because
  a shallower queue hides less latency in steady state. If C is the allocator,
  depth moves P but cannot move C. These two cannot mimic each other.

Usage: nix develop .. -c python3 analyse_matrix.py [--plot]
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fit_burst_model import DOCS, lsq, points          # noqa: E402

BLUE, ORANGE, INK, INK_2 = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e"
SURFACE, GRID = "#fcfcfb", "#e4e3de"


def fit_of(allc, cond, mode):
    d = allc.get(cond, {}).get(mode, {})
    if not d:
        return None
    pts = points(d)
    f = lsq(pts)
    if not f:
        return None
    P, C, r2, worst, n, se = f
    return {"P": P, "C": C, "r2": r2, "n": n, "se": se, "pts": pts}


def row(label, f):
    if f is None:
        return f"  {label:34s}  (no data)"
    se = f"+/-{f['se']:.0f}" if f["se"] else ""
    return (f"  {label:34s}  P = {f['P']:7.1f}   C = {f['C']:8.1f} {se:>8s}"
            f"   R2 {f['r2']:.3f}  n={f['n']}")


def main():
    allc = json.loads((DOCS / "results_reproduced.json").read_text())
    g = lambda c, m: fit_of(allc, c, m)

    base_d = g("pinned2_asshipped", "dramblast") or g("pinned_2100mhz", "dramblast")
    base_m = g("pinned2_asshipped", "maglev") or g("pinned_2100mhz", "maglev")

    print("=" * 84)
    print("CONTROL   the -B/-A/-Q build with no flags must reproduce the original")
    print("=" * 84)
    print(row("dramblast, as shipped", base_d))
    print(row("maglev, as shipped", base_m))
    # Only meaningful once the refactored build has actually been swept; before
    # that base_d IS the original arm and comparing it to itself would print a
    # reassuring 0.0% that means nothing.
    orig_d = g("pinned_2100mhz", "dramblast")
    if base_d and orig_d and "pinned2_asshipped" in allc:
        dp = abs(base_d["P"] - orig_d["P"]) / orig_d["P"] * 100
        dc = abs(base_d["C"] - orig_d["C"]) / abs(orig_d["C"]) * 100
        print(f"    dramblast vs the pre-refactor arm: P differs {dp:.1f}%, C differs {dc:.1f}%")
        print("    -> the refactor is innocent" if dp < 5 and dc < 15 else
              "    -> REFACTOR CHANGED BEHAVIOUR; nothing below can be read until this is resolved")

    print()
    print("=" * 84)
    print("1. CROSSOVER   each mode on the other's page backing")
    print("=" * 84)
    print(row("dramblast  1 GiB      (as shipped)", base_d))
    print(row("dramblast  2 MiB THP  (maglev's)", g("xover_dram_thp2m", "dramblast")))
    print(row("dramblast  4 KiB", g("xover_dram_4k", "dramblast")))
    print(row("maglev     2 MiB THP  (as shipped)", base_m))
    print(row("maglev     1 GiB      (dramblast's)", g("xover_mag_1g", "maglev")))
    print(row("maglev     4 KiB", g("xover_mag_4k", "maglev")))
    m1g, m2m = g("xover_mag_1g", "maglev"), base_m
    if m1g and m2m:
        closed = (m2m["P"] - m1g["P"]) / (m2m["P"] - base_d["P"]) * 100 if base_d else None
        print(f"\n    maglev P: {m2m['P']:.1f} -> {m1g['P']:.1f} on 1 GiB pages "
              f"({(m1g['P']-m2m['P'])/m2m['P']*100:+.1f}%)")
        if closed is not None:
            print(f"    that closes {closed:.0f}% of the gap to dramblast. "
                  f"The remainder is not address translation.")

    print()
    print("=" * 84)
    print("2. ALLOCATOR   C against the number of aligned_alloc/free pairs per burst")
    print("=" * 84)
    series = [(-1, g("alloc_hoisted", "dramblast")), (0, base_d),
              (2, g("alloc_x2", "dramblast")), (4, g("alloc_x4", "dramblast")),
              (8, g("alloc_x8", "dramblast"))]
    have = [(n, f) for n, f in series if f]
    for n, f in have:
        print(row(f"pairs = {n:+d}" + ("  (hoisted)" if n < 0 else ""), f))
    if len(have) >= 3:
        # pairs actually executed is n+1 for n>=0, and 0 for the hoisted arm
        pts = [(float(n + 1 if n >= 0 else 0), f["C"]) for n, f in have]
        fitres = lsq([(x, y, 0, 0) for x, y in pts])
        if fitres:
            C0, per_pair, r2, worst, _n, _se = fitres
            print(f"\n    C = {C0:.0f} + {per_pair:.0f} * pairs      (R2 {r2:.3f})")
            print(f"    -> one alloc/free pair costs {per_pair:.0f} cycles "
                  f"= {per_pair/2.1:.0f} ns on this machine")
            if base_d:
                sh = per_pair / base_d["C"] * 100
                print(f"    -> the shipped single pair is {sh:.1f}% of the shipped "
                      f"C of {base_d['C']:.0f} cycles")
                print(f"    -> {C0:.0f} cycles per burst are NOT the allocator")

    print()
    print("=" * 84)
    print("3. DEPTH   P and C against the prefetch pipeline depth")
    print("=" * 84)
    for d, cond in ((8, "depth_8"), (16, "depth_16"), (32, "depth_32")):
        print(row(f"depth = {d}", g(cond, "dramblast")))
    print(row("depth = 64  (as shipped)", base_d))
    ds = [(d, g(c, "dramblast")) for d, c in ((8, "depth_8"), (16, "depth_16"),
                                              (32, "depth_32"))] + [(64, base_d)]
    ds = [(d, f) for d, f in ds if f]
    if len(ds) >= 3:
        lo, hi = ds[0], ds[-1]
        print(f"\n    depth {lo[0]} -> {hi[0]}:  P {lo[1]['P']:.1f} -> {hi[1]['P']:.1f}"
              f"   C {lo[1]['C']:.1f} -> {hi[1]['C']:.1f}")
        print("    A pipeline-ramp C must fall with depth while P rises.")
        print("    An allocator C must be flat in depth.")

    if "--plot" not in sys.argv:
        return

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
                         "savefig.facecolor": SURFACE, "font.family": "DejaVu Sans",
                         "text.color": INK, "axes.labelcolor": INK_2,
                         "xtick.color": INK_2, "ytick.color": INK_2,
                         "axes.edgecolor": GRID, "xtick.major.size": 0,
                         "ytick.major.size": 0})
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))

    ax = axes[0]
    labels, Ps, Cs, cols = [], [], [], []
    for lab, f, col in (("dram\n1 GiB", base_d, BLUE),
                        ("dram\n2 MiB", g("xover_dram_thp2m", "dramblast"), BLUE),
                        ("dram\n4 KiB", g("xover_dram_4k", "dramblast"), BLUE),
                        ("mag\n1 GiB", g("xover_mag_1g", "maglev"), ORANGE),
                        ("mag\n2 MiB", base_m, ORANGE),
                        ("mag\n4 KiB", g("xover_mag_4k", "maglev"), ORANGE)):
        if f:
            labels.append(lab); Ps.append(f["P"]); Cs.append(f["C"]); cols.append(col)
    ax.bar(range(len(Ps)), Ps, color=cols, width=0.62)
    ax.set_xticks(range(len(labels))); ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylabel("P  (core cycles per packet)")
    ax.set_title("1. Page backing moves the per-packet cost", fontsize=11,
                 loc="left", pad=10)

    ax = axes[1]
    if len(have) >= 2:
        xs = [n + 1 if n >= 0 else 0 for n, _ in have]
        ys = [f["C"] for _, f in have]
        ax.plot(xs, ys, marker="o", color=BLUE, linewidth=2, markersize=7,
                markeredgecolor=SURFACE, markeredgewidth=1.8)
        ax.set_xlabel("aligned_alloc/free pairs per burst")
        ax.set_ylabel("C  (core cycles per burst)")
    ax.set_title("2. The allocator's real share of C", fontsize=11, loc="left", pad=10)

    ax = axes[2]
    if len(ds) >= 2:
        ax.plot([d for d, _ in ds], [f["P"] for _, f in ds], marker="o",
                color=BLUE, linewidth=2, label="P per packet")
        ax2 = ax.twinx()
        ax2.plot([d for d, _ in ds], [f["C"] for _, f in ds], marker="s",
                 color=ORANGE, linewidth=2, label="C per burst")
        ax2.set_ylabel("C  (cycles per burst)", color=ORANGE)
        ax.set_xscale("log", base=2)
        ax.set_xlabel("prefetch pipeline depth")
        ax.set_ylabel("P  (cycles per packet)", color=BLUE)
    ax.set_title("3. Is C the pipeline ramp?", fontsize=11, loc="left", pad=10)

    for a in axes:
        a.grid(True, color=GRID, linewidth=0.8); a.set_axisbelow(True)
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
    fig.tight_layout()
    out = DOCS / "matrix.png"
    fig.savefig(out, dpi=200)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
