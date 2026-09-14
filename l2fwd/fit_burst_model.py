"""Fit the per-burst cost model to every condition present, and plot it.

    cycles per forwarded packet  =  P  +  C / B

P is the irreducible per-packet cost; C is a fixed cost paid once per RX burst
and amortised over the B packets that burst returned. The queue-pair sweep is
what makes this fittable at all: offered load is constant, so raising the queue
count divides the same packet stream over more queues and shrinks B, sweeping
1/B over a wide range without changing anything else about the workload.

Why the fit is in CORE CYCLES and not in the TSC ticks l2fwd prints
------------------------------------------------------------------
l2fwd reports "Cycle per fwd packet", but it measures with rte_rdtsc() and this
SKU's TSC is invariant at 2.100 GHz (verified: the kernel calibrated it against
HPET/PIT at boot, before switching its clocksource to the TSC -- dmesg
"tsc: Detected 2100.000 MHz processor"). So the printed number is elapsed TIME,
and it equals core cycles only when the core clock is also 2.1 GHz. Every
condition that carries a measured freq_mhz is converted; one that does not is
skipped rather than guessed, because multiplying by a nominal boost ratio is
precisely the bug that made the first attempt at this fit unstable.

Usage: nix develop .. -c python3 fit_burst_model.py [--plot]
"""

import json
import pathlib
import sys

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
TSC_MHZ = 2100.0

# Conditions worth fitting, in the order they should be read. Anything else in
# the results file is ignored: the historical arms have no recorded delivered
# clock, so their ticks cannot be put on a cycles axis at all.
ORDER = ["pinned_2100mhz", "turbo_instr",
         "xover_dram_thp2m", "xover_dram_4k",
         "xover_mag_1g", "xover_mag_4k"]

BLUE, ORANGE = "#2a78d6", "#eb6834"
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3de"


def core_cycles(rec):
    """TSC ticks -> core cycles, using the clock actually delivered in that run."""
    f = rec.get("freq_mhz")
    if f is None or "cycles_per_pkt" not in rec:
        return None
    return rec["cycles_per_pkt"] * f / TSC_MHZ


def points(rec_by_q):
    """[(1/B, cycles_per_pkt, q, B)] for every run with a usable burst size."""
    out = []
    for q, rec in sorted(rec_by_q.items(), key=lambda kv: int(kv[0])):
        y, b = core_cycles(rec), rec.get("rx_batch")
        if y is None or not b:
            continue
        out.append((1.0 / b, y, int(q), b))
    return out


def lsq(pts):
    """y = P + C*x by least squares, plus R^2 and the worst residual."""
    n = len(pts)
    if n < 3:
        return None
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    den = n * sxx - sx * sx
    if den == 0:
        return None
    C = (n * sxy - sx * sy) / den
    P = (sy - C * sx) / n
    ybar = sy / n
    ss_tot = sum((p[1] - ybar) ** 2 for p in pts)
    ss_res = sum((p[1] - (P + C * p[0])) ** 2 for p in pts)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    worst = max(abs(p[1] - (P + C * p[0])) for p in pts)
    # Standard error of the slope. Needed because a slope of "about zero" and a
    # slope that is genuinely resolved must not be reported the same way: the
    # work/stall decomposition below divides by C, so an unresolved C produces
    # a confident-looking percentage out of noise. maglev is exactly this case.
    se_c = None
    if n > 2:
        var = ss_res / (n - 2)
        se_c = (var * n / den) ** 0.5
    return P, C, r2, worst, n, se_c


def two_point(pts):
    """The same slope from the two extreme bursts only -- an estimator that
    shares no algebra with least squares, so agreement between them is
    evidence the model fits rather than evidence the fit converged."""
    if len(pts) < 2:
        return None
    lo = min(pts, key=lambda p: p[0]); hi = max(pts, key=lambda p: p[0])
    if hi[0] == lo[0]:
        return None
    return (hi[1] - lo[1]) / (hi[0] - lo[0])


def main():
    allc = json.loads((DOCS / "results_reproduced.json").read_text())
    fits = {}

    print("=" * 92)
    print("PER-BURST COST MODEL      cycles/packet = P + C/B      (core cycles at the")
    print("                          delivered clock of each run)")
    print("=" * 92)
    for cond in ORDER:
        if cond not in allc:
            continue
        for mode in ("dramblast", "maglev"):
            pts = points(allc[cond].get(mode, {}))
            if not pts:
                continue
            f = lsq(pts)
            if not f:
                continue
            P, C, r2, worst, n, se_c = f
            tp = two_point(pts)
            fits[(cond, mode)] = (P, C, r2, pts, se_c)
            bs = sorted({p[3] for p in pts})
            print(f"\n  {cond:20s} {mode:10s}  n={n}  burst {bs[0]}..{bs[-1]}")
            print(f"      P = {P:8.1f} cycles/packet      (irreducible per-packet cost)")
            print(f"      C = {C:8.1f} cycles/burst       (fixed cost, amortised over B)"
            + (f"  +/- {se_c:.0f}" + ("   NOT RESOLVED (|C| < 2 s.e.)"
                                      if abs(C) < 2 * se_c else "")
               if se_c else ""))
            print(f"      R^2 = {r2:.4f}   worst residual {worst:.1f} cycles", end="")
            if tp is not None:
                sep = abs(tp - C) / abs(C) * 100 if C else float('nan')
                print(f"   two-point C = {tp:.1f} ({sep:.1f}% from LSQ)")
            else:
                print()

    # ---- decompose each fitted coefficient into work and exposed stall ------
    #
    # A core cycle count measured at two different clocks separates CPU work
    # from time spent waiting on memory, because the two scale differently:
    # instructions retire in a fixed number of CYCLES regardless of clock,
    # while a DRAM access takes a fixed number of NANOSECONDS and therefore
    # costs more cycles the faster the core runs. Writing X for a coefficient
    # in core cycles, W for its CPU work in cycles and T for its exposed stall
    # in seconds,
    #
    #     X(f) = W + T * f          so      T = (X_turbo - X_pinned) / (f_t - f_p)
    #
    # and the memory-bound fraction at the pinned clock is T * f_pinned / X.
    # This is done on the FITTED coefficients rather than point by point on
    # purpose: the two arms do not sit at the same burst sizes (a faster
    # forwarder drains its queues and gets smaller bursts), so a q-for-q
    # comparison confounds clock with burst size. P and C are already free of
    # burst size by construction, which is exactly what makes them comparable.
    def arm_freq(cond, mode):
        recs = allc.get(cond, {}).get(mode, {}).values()
        fs = [r["freq_mhz"] for r in recs if r.get("freq_mhz")]
        return sum(fs) / len(fs) if fs else None

    print()
    print("=" * 92)
    print("WORK versus EXPOSED MEMORY STALL, from the two clock arms")
    print("=" * 92)
    for mode in ("dramblast", "maglev"):
        kp, kt = ("pinned_2100mhz", mode), ("turbo_instr", mode)
        if kp not in fits or kt not in fits:
            continue
        fp, ft = arm_freq(*kp), arm_freq(*kt)
        if not fp or not ft or abs(ft - fp) < 1:
            continue
        print(f"\n  {mode}   pinned {fp:.0f} MHz -> turbo {ft:.0f} MHz   "
              f"(clock ratio {ft / fp:.3f})")
        for name, i in (("P  per packet", 0), ("C  per burst ", 1)):
            xp, xt = fits[kp][i], fits[kt][i]
            if i == 1:
                sp, st = fits[kp][4], fits[kt][4]
                if sp and st and (abs(xp) < 2 * sp or abs(xt) < 2 * st):
                    print(f"      {name}  pinned {xp:8.1f}  turbo {xt:8.1f} cycles")
                    print("          -> NOT DECOMPOSED: C is indistinguishable from zero "
                          "in at least one arm,")
                    print("             so work and stall shares of it are a ratio of "
                          "noise to noise.")
                    continue
            T_ns = (xt - xp) / ((ft - fp) / 1000.0)     # MHz -> cycles/ns
            stall_p = T_ns * fp / 1000.0                # cycles of stall at f_pinned
            work = xp - stall_p
            frac = stall_p / xp * 100 if xp else float("nan")
            print(f"      {name}  pinned {xp:8.1f}  turbo {xt:8.1f} cycles")
            print(f"          -> exposed stall {T_ns:7.2f} ns  = {stall_p:7.1f} cycles "
                  f"at 2.1 GHz  ({frac:5.1f}% of the cost)")
            print(f"          -> CPU work      {work:7.1f} cycles  ({100 - frac:5.1f}%)")

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

    fig, ax = plt.subplots(figsize=(10, 6.2))
    styles = {"dramblast": BLUE, "maglev": ORANGE}
    dashes = {"pinned_2100mhz": (0, ()), "turbo_instr": (0, (5, 3)),
              "xover_dram_thp2m": (0, (1, 2)), "xover_dram_4k": (0, (3, 1, 1, 1)),
              "xover_mag_1g": (0, (1, 2)), "xover_mag_4k": (0, (3, 1, 1, 1))}

    for (cond, mode), (P, C, r2, pts, _se) in fits.items():
        col = styles[mode]
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        ax.scatter(xs, ys, s=42, color=col, edgecolor=SURFACE, linewidth=1.6,
                   zorder=3)
        lo, hi = 0.0, max(xs) * 1.05
        ax.plot([lo, hi], [P, P + C * hi], color=col, linewidth=1.8,
                linestyle=dashes.get(cond, (0, ())), alpha=0.9, zorder=2,
                label=f"{mode} / {cond}   P={P:.0f}  C={C:.0f}")

    ax.set_xlabel("1 / RX burst size   (packets$^{-1}$)", fontsize=10)
    ax.set_ylabel("Core cycles per forwarded packet", fontsize=10)
    ax.set_title("A fixed per-burst cost, exposed by shrinking the burst",
                 fontsize=13, color=INK, loc="left", pad=12, fontweight="medium")
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_xlim(left=0)
    ax.legend(loc="upper left", frameon=False, fontsize=8.5, labelcolor=INK_2)
    fig.text(0.012, 0.015,
             "Each point is one queue-pair count. The intercept is the per-packet cost; "
             "the slope is the cost paid once per burst.",
             fontsize=8.5, color=INK_2)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    out = DOCS / "burst_model.png"
    fig.savefig(out, dpi=200)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
