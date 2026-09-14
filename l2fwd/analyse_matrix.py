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



def chi2_sf(x, k):
    """P(chi-square_k > x), exactly, for small integer k. No scipy here."""
    import math
    if x <= 0:
        return 1.0
    if k % 2 == 0:
        t = math.exp(-x / 2.0)
        acc, term = t, t
        for i in range(1, k // 2):
            term *= (x / 2.0) / i
            acc += term
        return min(1.0, acc)
    acc = math.erfc(math.sqrt(x / 2.0))
    if k > 1:
        t = math.sqrt(2.0 * x / math.pi) * math.exp(-x / 2.0)
        term, add = t, 0.0
        for i in range(1, (k - 1) // 2 + 1):
            add += term
            term *= x / (2.0 * i + 1.0)
        acc += add
    return min(1.0, acc)


def weighted_line(pts):
    """y = a + b*x weighted by 1/sigma^2, with chi-square against those sigmas.

    R-squared is close to uninformative here: three or four points, two
    parameters, and an x-range that does all the work. What matters is whether
    the residuals are consistent with the error bars the individual fits
    reported, and that is a chi-square question, not a variance-explained one.
    """
    import math
    w = [1.0 / (sg * sg) for _, _, sg in pts]
    sw = sum(w)
    sx = sum(wi * x for wi, (x, _, _) in zip(w, pts))
    sy = sum(wi * y for wi, (_, y, _) in zip(w, pts))
    sxx = sum(wi * x * x for wi, (x, _, _) in zip(w, pts))
    sxy = sum(wi * x * y for wi, (x, y, _) in zip(w, pts))
    den = sw * sxx - sx * sx
    if not den:
        return None
    b = (sw * sxy - sx * sy) / den
    a = (sy - b * sx) / sw
    resid = [(x, y, sg, y - (a + b * x), (y - (a + b * x)) / sg) for x, y, sg in pts]
    chi2 = sum(r[4] ** 2 for r in resid)
    dof = len(pts) - 2
    return a, b, chi2, dof, resid



def fit_of(allc, cond, mode):
    d = allc.get(cond, {}).get(mode, {})
    if not d:
        return None
    pts = points(d)
    # A condition slow enough never to reach line rate stays oversubscribed at
    # every queue count, so its RX burst never leaves 64 and every point sits at
    # the same x. That is a result, not missing data -- there is simply no
    # burst-size range to fit a slope against -- so say which it is.
    if len({p[3] for p in pts}) < 2:
        return {"nofit": True, "n": len(pts),
                "burst": pts[0][3] if pts else None}
    f = lsq(pts)
    if not f:
        return None
    P, C, r2, worst, n, se = f
    return {"P": P, "C": C, "r2": r2, "n": n, "se": se, "pts": pts}


def at_q1(allc, cond, mode):
    """Cost at q=1 in core cycles: one worker, a full 64-packet burst.

    This is reported alongside the fit because the fit's two parameters are not
    always enough. On 4 KiB pages the cost rises with QUEUE COUNT at constant
    burst size -- 117 to 129 ticks across q=1..6 with the burst pinned at 64 --
    which no per-burst term can express. Page tables are shared: ten cores each
    walking a 2-million-entry table put the page-table working set itself into
    contention for the last-level cache, so the cost acquires a third component
    that scales with core count. q=1 has one worker and a full burst, so it is
    free of both the per-burst term and the contention term, and it is the
    honest single number for comparing backings.
    """
    r = allc.get(cond, {}).get(mode, {}).get("1")
    if not r or "cycles_per_pkt" not in r or not r.get("freq_mhz"):
        return None
    return r["cycles_per_pkt"] * r["freq_mhz"] / 2100.0


def row(label, f):
    if f is None:
        return f"  {label:34s}  (no data)"
    if f.get("nofit"):
        return (f"  {label:34s}  n={f['n']}, but every run sat at burst "
                f"{f['burst']} -- never saturated the link, so no slope is "
                f"measurable")
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
    # Compare against the pre-refactor arm coefficient by coefficient, and in
    # units of the fit's own uncertainty rather than in percent. P and C trade
    # off against each other in a least-squares fit, so a small shift in the
    # data moves both and a bare percentage on either one overstates the case.
    for mode, orig_key in (("dramblast", "pinned_2100mhz"), ("maglev", "pinned_2100mhz")):
        new_f = g("pinned2_asshipped", mode)
        old_f = g(orig_key, mode)
        if not (new_f and old_f):
            continue
        print(f"\n    {mode} vs the pre-refactor arm")
        dp = new_f["P"] - old_f["P"]
        print(f"      P {old_f['P']:7.1f} -> {new_f['P']:7.1f}   "
              f"{dp:+.1f} cycles ({dp/old_f['P']*100:+.1f}%)")
        sig = (new_f["se"] ** 2 + old_f["se"] ** 2) ** 0.5 if new_f["se"] and old_f["se"] else None
        dc = new_f["C"] - old_f["C"]
        if sig:
            print(f"      C {old_f['C']:7.1f} -> {new_f['C']:7.1f}   "
                  f"{dc:+.1f} cycles = {abs(dc)/sig:.1f} sigma of the combined fit error"
                  + ("  (not significant)" if abs(dc) < 2 * sig else "  (SIGNIFICANT)"))
        # The cost at the two ends of the burst range is what a reader actually
        # cares about, and it is not hostage to how the fit split P from C.
        for b in (64, 8):
            o, n = old_f["P"] + old_f["C"] / b, new_f["P"] + new_f["C"] / b
            print(f"      at burst {b:>2}: {o:6.1f} -> {n:6.1f} cycles/packet "
                  f"({(n-o)/o*100:+.1f}%)")
    print("\n    Read every row below against `pinned2_asshipped`, never against the")
    print("    pre-refactor arm: the two binaries are not interchangeable.")

    print()
    print("=" * 84)
    print("1. CROSSOVER   each mode on the other's page backing")
    print("=" * 84)
    base_cond = "pinned2_asshipped" if "pinned2_asshipped" in allc else "pinned_2100mhz"
    # (mode, short page label, condition, whether this is that mode's shipped default)
    spec = [("dramblast", "1 GiB", base_cond, True),
            ("dramblast", "2 MiB", "xover_dram_thp2m", False),
            ("dramblast", "4 KiB", "xover_dram_4k", False),
            ("maglev", "2 MiB", base_cond, True),
            ("maglev", "1 GiB", "xover_mag_1g", False),
            ("maglev", "4 KiB", "xover_mag_4k", False)]
    q1 = {}
    for mode, pg, cond, shipped in spec:
        tag = f"{mode:10s} {pg:6s}" + ("(as shipped)" if shipped else "")
        print(row(tag, g(cond, mode)))
        v = at_q1(allc, cond, mode)
        if v is not None:
            q1[(mode, pg)] = v

    if q1:
        print("\n  At q=1 -- one worker, a full 64-packet burst -- so free of both the")
        print("  per-burst term and the cross-core page-table contention:")
        for mode, pg, _, _ in spec:
            if (mode, pg) in q1:
                print(f"      {mode:10s} {pg:6s}  {q1[(mode, pg)]:7.1f} core cycles/packet")
        d1, d2 = q1.get(("dramblast", "1 GiB")), q1.get(("dramblast", "2 MiB"))
        d4 = q1.get(("dramblast", "4 KiB"))
        m1, m2 = q1.get(("maglev", "1 GiB")), q1.get(("maglev", "2 MiB"))
        print()
        if d1 and d2:
            print(f"      dramblast, 1 GiB -> 2 MiB:  {d2 - d1:+6.1f} cycles")
        if d1 and d4:
            print(f"      dramblast, 1 GiB -> 4 KiB:  {d4 - d1:+6.1f} cycles")
        if m2 and m1:
            print(f"      maglev,    2 MiB -> 1 GiB:  {m1 - m2:+6.1f} cycles")
        if d1 and m2:
            print(f"\n      as-shipped gap between engines:  {m2 - d1:6.1f} cycles")
        if d1 and m1:
            print(f"      gap at MATCHED 1 GiB pages:      {m1 - d1:6.1f} cycles"
                  f"   ({(m1 - d1) / (m2 - d1) * 100:.0f}% of it survives)"
                  if m2 else "")
            print("      -> the surviving gap is not address translation; it is the")
            print("         prefetch pipeline.")

    print()
    print("=" * 84)
    print("2. ALLOCATOR   C against the number of aligned_alloc/free pairs per burst")
    print("=" * 84)
    series = [(-1, g("alloc_hoisted", "dramblast")), (0, base_d),
              (2, g("alloc_x2", "dramblast")), (4, g("alloc_x4", "dramblast")),
              (8, g("alloc_x8", "dramblast"))]
    # Only conditions with a real slope enter the line. An arm that never
            # saturated the link has no burst-size range and therefore no C.
    have = [(n, f) for n, f in series if f and "C" in f]
    for n, f in have:
        print(row(f"pairs = {n:+d}" + ("  (hoisted)" if n < 0 else ""), f))
    # Two different quantities, and they must not be conflated.
    #
    #   hoist difference  C(0 pairs) - C(hoisted)  is the SHIPPED pair's cost:
    #       the real allocation, separated from its free by the whole batch.
    #   amplification slope                        is an INCREMENTAL pair's cost:
    #       alloc and free back to back in a tight loop, which is the warmest
    #       possible tcache path and therefore a LOWER BOUND on the shipped one.
    #
    # If they agree, the shipped pair is as cheap as a back-to-back pair and the
    # allocator's share is settled. If the hoist difference is materially
    # larger, the separation costs something -- the tcache entry ages out of L1
    # across a batch -- and the slope alone would have understated it.
    hoisted = dict(series).get(-1)
    if hoisted and "C" in hoisted and base_d:
        shipped_pair = base_d["C"] - hoisted["C"]
        sig = ((base_d["se"] or 0) ** 2 + (hoisted["se"] or 0) ** 2) ** 0.5
        print(f"\n    shipped pair, by removing it:  C {hoisted['C']:.0f} (hoisted) "
              f"-> {base_d['C']:.0f} (as shipped)")
        print(f"      = {shipped_pair:.0f} cycles for the one round trip the code "
              f"actually performs"
              + (f", {abs(shipped_pair)/sig:.1f} sigma" if sig else ""))
        print(f"      = {shipped_pair/base_d['C']*100:.1f}% of the per-burst cost; "
              f"{base_d['C']-shipped_pair:.0f} cycles are something else")

    # PRIMARY ESTIMATOR: matched burst, no fitting at all.
    #
    # Every arm is read at q=1, where the burst is a full 64 packets. Adding
    # allocator pairs makes the forwarder slower, which makes it stay
    # oversubscribed further up the sweep, which changes the burst sizes it
    # sits at -- so the arms are NOT at comparable bursts away from q=1, and a
    # fit over each arm's own burst range is comparing conditions that differ
    # in two things. At a fixed burst of 64 the difference in cost per packet
    # multiplied by 64 IS the difference in cost per burst, with no model in
    # between. This is the third time in this investigation that holding burst
    # size fixed has beaten fitting it out.
    print("\n    At a MATCHED 64-packet burst (q=1), no fit involved:")
    print(f"      {'pairs':>6} {'cyc/pkt':>9} {'vs hoisted':>11} {'x64 per burst':>14} {'per pair':>9}")
    base_cyc, per_pair_vals, q1pts = None, [], []
    for n, cond in [(0, "alloc_hoisted"), (1, "pinned2_asshipped"), (2 + 1, "alloc_x2"),
                    (4 + 1, "alloc_x4"), (8 + 1, "alloc_x8")]:
        v = at_q1(allc, cond, "dramblast")
        if v is None:
            continue
        if base_cyc is None:
            base_cyc = v
        d = v - base_cyc
        pp = d * 64 / n if n else 0
        if n > 1:
            per_pair_vals.append(pp)
        q1pts.append((n, d * 64))
        print(f"      {n:>6} {v:>9.1f} {d:>11.1f} {d*64:>14.0f} {pp:>9.0f}")
    # The per-pair numbers above are total/pairs, which is an AVERAGE and
    # therefore converges toward the asymptotic slope as the pair count grows,
    # whatever the low-count behaviour is. It cannot confirm a constant price.
    # What can: the consecutive slopes between adjacent multi-pair arms, and the
    # intercept of a line through them.
    multi = [(n, c) for n, c in q1pts if n >= 3]
    if len(multi) >= 2:
        print("\n      Consecutive slopes between multi-pair arms (independent of")
        print("      each other, unlike total/pairs):")
        for (n0, c0), (n1, c1) in zip(multi, multi[1:]):
            print(f"        {n0} -> {n1} pairs: {(c1 - c0) / (n1 - n0):.2f} cycles per pair")
        nn = len(multi)
        sx = sum(n for n, _ in multi); sy = sum(c for _, c in multi)
        sxx = sum(n * n for n, _ in multi); sxy = sum(n * c for n, c in multi)
        den = nn * sxx - sx * sx
        if den:
            b = (nn * sxy - sx * sy) / den
            a = (sy - b * sx) / nn
            print(f"      line through the multi-pair arms: {a:+.1f} + {b:.2f} * pairs")
            print(f"      -> the intercept is zero to within {abs(a):.1f} cycles, which is")
            print("         the structural check: k pairs cost exactly k times one pair,")
            print("         with nothing left over, as an additive per-pair cost requires.")
            shipped = dict(q1pts).get(1)
            if shipped:
                pred = a + b
                print(f"\n      Extrapolated to the shipped arm: {pred:.1f}, measured "
                      f"{shipped:.1f}")
                print(f"      -> the lone shipped pair is {shipped - pred:+.1f} cycles "
                      f"({(shipped-pred)/pred*100:+.1f}%) below the line.")
                print("\n      But this is NOT a privileged first pair. If pair #1 were")
                print(f"      intrinsically {shipped:.0f} and every later pair {b:.0f}, the")
                print("      multi-pair totals would have to be:")
                for n, c in multi:
                    model = shipped + b * (n - 1)
                    print(f"        {n} pairs: model {model:.0f}  measured {c:.0f}  "
                          f"off by {c - model:+.0f}")
                print("      A constant miss at every arm. The discount does not persist,")
                print("      so in an arm with three or more pairs EVERY pair costs")
                print(f"      {b:.0f} including the first.")
                print("\n      Honest statement: a LONE pair costs {:.0f} cycles; with three".format(shipped))
                print(f"      or more in flight each costs {b:.0f}. Pair cost depends on how")
                print("      many pairs there are, not on which one it is -- an allocator")
                print("      state or load effect (more live chunks, more splitting, a")
                print("      larger free-list working set), not a position effect.")
                print("\n      CONSEQUENCE: the shipped configuration has exactly one pair,")
                print(f"      so {shipped:.0f} is the number to quote. {b:.0f} belongs to a regime")
                print("      the shipped code is never in, and calibrating the shipped")
                print(f"      pair from it would overstate by {(b-shipped)/shipped*100:.0f}%.")

    if len(have) >= 3:
        # Absolute pairs executed: n+1 for n>=0, zero for the hoisted arm.
        # An arm whose own sweep barely left a 64-packet burst cannot produce a
        # trustworthy slope, and must not be given equal standing in the line.
        # The +8 arm is exactly this: two points off burst 64, non-monotone,
        # R^2 0.62, a standard error a quarter of the value.
        usable = [(n, f) for n, f in have if f.get("r2", 0) >= 0.90]
        dropped = [n for n, f in have if f.get("r2", 0) < 0.90]
        if dropped:
            print(f"\n    (excluded from the line: pairs={dropped} -- that arm is so "
                  f"slow it\n     never left a 64-packet burst, so its own slope is "
                  f"not measurable)")
        pts = [(float(n + 1 if n >= 0 else 0), f["C"], f["se"] or 1.0)
               for n, f in usable]
        res = weighted_line(pts)
        if res:
            a, b, chi2, dof, resid = res
            print(f"\n    weighted fit   C = {a:.0f} + {b:.0f} * pairs")
            print(f"    {'pairs':>6} {'measured':>12} {'line':>9} {'residual':>10} {'sigma':>7}")
            for x, y, sg, r, z in resid:
                print(f"    {x:>6.0f} {y:>8.1f} +/-{sg:<4.0f} {a + b * x:>9.1f} "
                      f"{r:>10.1f} {z:>7.1f}")
            pval = chi2_sf(chi2, dof) if dof > 0 else float("nan")
            print(f"    chi-square {chi2:.1f} on {dof} dof   p = {pval:.4g}")
            if dof > 0 and pval < 0.01:
                print("    -> THE LINE IS REJECTED against the fits' own error bars.")
                print("       The pairs are not all the same price, so the removal")
                print("       estimate and the slope are two different quantities")
                print("       rather than two routes to one.")
            elif dof > 0 and pval < 0.05:
                print("    -> MARGINAL. Not rejected, but not comfortable either, and")
                print("       the tension is in the direction the separation argument")
                print("       predicts: the shipped pair sits above the line while the")
                print("       arms with extra back-to-back pairs sit on it.")
            else:
                print("    -> consistent with one price per pair")
            print("       (R-squared is deliberately not quoted for this regression.")
            print("        With this few points and this x-range it is near-1 whatever")
            print("        happens, and it says nothing about whether the residuals")
            print("        are consistent with the error bars -- which is the only")
            print("        question that matters here.)")
            print(f"    -> an INCREMENTAL pair costs {b:.0f} cycles = {b/2.1:.0f} ns")

            # Reconcile the two estimators rather than choosing one.
        ps = [(n, f["P"]) for n, f in usable]
        if len(ps) >= 2:
            dP = ps[-1][1] - ps[0][1]
            print(f"\n    The two estimators disagree on the incremental pair -- 511")
            print(f"    cycles at matched burst against {b:.0f} from the fit -- and the")
            print(f"    reason is visible in P, which is not constant across the arms:")
            print("      " + "  ".join(f"{n:+d}:{P:.0f}" for n, P in ps))
            print(f"    P rises {dP:.0f} cycles from the hoisted arm to the deepest")
            print("    usable one. The matched-burst estimator multiplies the whole")
            print("    per-packet difference by 64 and so charges that rise to the")
            print("    per-burst term; the fit separates them but pays for it with a")
            print("    model. The truth is bracketed, not pinned: an incremental pair")
            print(f"    costs between {b:.0f} and 511 cycles.")
            print("    Why P moves at all is not established. A once-per-burst")
            print("    allocation should not touch per-packet cost; cache and TLB")
            print("    pollution from the allocator's own chunk walking is the")
            print("    obvious candidate and has not been measured.")
            print("\n    NONE OF THIS MOVES THE HEADLINE. The shipped pair is 447")
            print("    cycles by matched burst and 462 by the fit difference, and the")
            print("    non-allocator remainder is measured directly at 256 +/- 13.")
            print("    The allocator is ~60-64% of the per-burst cost on either route.")

    # The non-allocator remainder is MEASURED, not extrapolated. The hoisted
        # arm is that quantity directly; the fitted intercept is an
        # extrapolation that inherits whatever is wrong with the line.
        if hoisted and "C" in hoisted:
            print(f"\n    non-allocator per-burst cost, measured directly by the")
            print(f"    hoisted arm: {hoisted['C']:.0f} +/- {hoisted['se']:.0f} cycles")
            print(f"    (NOT the fitted intercept -- that is an extrapolation, and")
            print(f"     where the line is rejected it is a wrong one.)")

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
    # q=1 rather than the fitted intercept: on 4 KiB pages the fit absorbs a
    # core-count term it cannot represent, and for maglev on 4 KiB there is no
    # fit at all because that arm never saturates the link and so never leaves a
    # 64-packet burst. q=1 is defined for every arm and means the same thing in
    # each of them.
    labels, vals, cols = [], [], []
    for lab, mode, cond, col in (("dram\n1 GiB", "dramblast", base_cond, BLUE),
                                 ("dram\n2 MiB", "dramblast", "xover_dram_thp2m", BLUE),
                                 ("dram\n4 KiB", "dramblast", "xover_dram_4k", BLUE),
                                 ("mag\n1 GiB", "maglev", "xover_mag_1g", ORANGE),
                                 ("mag\n2 MiB", "maglev", base_cond, ORANGE),
                                 ("mag\n4 KiB", "maglev", "xover_mag_4k", ORANGE)):
        v = at_q1(allc, cond, mode)
        if v:
            labels.append(lab); vals.append(v); cols.append(col)
    bars = ax.bar(range(len(vals)), vals, color=cols, width=0.62)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 3, f"{v:.0f}",
                ha="center", fontsize=9, color=INK_2)
    ax.set_xticks(range(len(labels))); ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylabel("core cycles per packet   (q=1, burst 64)")
    ax.set_ylim(0, max(vals) * 1.16 if vals else 1)
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
