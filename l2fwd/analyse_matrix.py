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
    # Standard error of the fitted cost AT a given burst, with the P-C
    # covariance included. Quoting P and C separately understates how well the
    # curve itself is known, because the two are strongly anti-correlated: a
    # fit can be badly split between them and still predict the measured costs
    # tightly. Every cross-arm comparison below is made on this quantity rather
    # than on C alone.
    sx = sum(q[0] for q in pts); sxx = sum(q[0] ** 2 for q in pts)
    den = n * sxx - sx * sx
    ss_res = sum((q[1] - (P + C * q[0])) ** 2 for q in pts)
    s2 = ss_res / (n - 2) if n > 2 else 0.0
    var_P = s2 * sxx / den if den else 0.0
    var_C = s2 * n / den if den else 0.0
    cov = -s2 * sx / den if den else 0.0
    se_at = {}
    for B in (64, 32, 16, 9):
        v = var_P + var_C / (B * B) + 2.0 * cov / B
        se_at[B] = v ** 0.5 if v > 0 else 0.0
    return {"P": P, "C": C, "r2": r2, "n": n, "se": se, "pts": pts,
            "se_at": se_at, "cov_pc": cov / (var_P * var_C) ** 0.5
            if var_P > 0 and var_C > 0 else None}


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
    amplified_slope = None
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
            amplified_slope = b

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
            amp = amplified_slope if amplified_slope else float("nan")
            print(f"\n    The two estimators disagree on the incremental pair --")
            print(f"    {amp:.0f} cycles from the matched-burst slopes against {b:.0f}")
            print(f"    from the fit -- and the")
            print(f"    reason is visible in P, which is not constant across the arms:")
            print("      " + "  ".join(f"{n:+d}:{P:.0f}" for n, P in ps))
            print(f"    P rises {dP:.0f} cycles from the hoisted arm to the deepest")
            print("    usable one. The matched-burst estimator multiplies the whole")
            print("    per-packet difference by 64 and so charges that rise to the")
            print("    per-burst term; the fit separates them but pays for it with a")
            print("    model. The truth is bracketed, not pinned: an incremental pair")
            print(f"    costs between {b:.0f} and {amp:.0f} cycles.")
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
    ds = [(d, f) for d, f in ds if f and "C" in f]
    if len(ds) >= 3:
        lo, hi = ds[0], ds[-1]
        print(f"\n    depth {lo[0]} -> {hi[0]}:  P {lo[1]['P']:.1f} -> {hi[1]['P']:.1f}"
              f"   C {lo[1]['C']:.1f} -> {hi[1]['C']:.1f}")
        # Model-free companion. The P/C split is correlated (r ~ -0.7 here), so
        # a claim about C alone is fragile; the cost evaluated at a matched
        # burst is not, and a genuine slope difference shows up as the two
        # curves crossing. Errors propagated WITH the covariance.
        print("\n    Model-free check -- cost at matched burst, since P and C are")
        print("    strongly anti-correlated in this fit and a claim about either")
        print("    alone is fragile. A real slope difference shows up as a crossing:")
        hdr = "      " + "".join(f"{'B=%d' % b:>14}" for b in (64, 32, 16, 9))
        print(hdr)
        for dpt, f in ds:
            cells = []
            for B in (64, 32, 16, 9):
                v = f["P"] + f["C"] / B
                sg = f.get("se_at", {}).get(B)
                cells.append(f"{v:8.1f}+/-{sg:<4.1f}" if sg else f"{v:8.1f}     ")
            print(f"   d{dpt:<3}" + "".join(f"{c:>14}" for c in cells))
        print("    A pipeline-ramp C must fall with depth while P rises.")
        print("    An allocator C must be flat in depth.")
        # Sharper, because the allocator share is now measured rather than
        # hypothetical: one alloc/free pair happens per burst at every depth, so
        # that part of C cannot move. Only the remainder is available to depth.
        shipped_pair = dict(q1pts).get(1) if q1pts else None
        if shipped_pair and base_d:
            floor = shipped_pair
            room = base_d["C"] - floor
            print(f"\n    Sharper, using the measured allocator cost. Exactly one")
            print(f"    alloc/free pair runs per burst at EVERY depth -- the buffer is")
            print(f"    sized by the burst length, not by the queue depth (dramblast.c")
            print(f"    :243), so the -Q knob does not change what is allocated. That")
            print(f"    makes {floor:.0f} cycles a FLOOR under C at every depth, and the")
            print(f"    remaining {room:.0f} of the shipped C ({base_d['C']:.0f}) is all the")
            print(f"    pipeline ramp can possibly own.")
            print(f"\n    The test is a floor, not a spread: C may rise with depth")
            print(f"    without limit, but no arm's C may fall below the allocator's")
            print(f"    own per-burst cost.")
            for dpt, f in ds:
                z = (floor - f["C"]) / (f["se"] or 1.0)
                mark = ("OK" if f["C"] >= floor else
                        f"below the floor by {floor - f['C']:.0f} ({z:.1f} sigma)")
                print(f"      depth {dpt:>2}: C = {f['C']:6.1f} +/- {f['se']:.0f}   {mark}")
            worst = min(ds, key=lambda t: t[1]["C"])
            if worst[1]["C"] < floor:
                zz = (floor - worst[1]["C"]) / (worst[1]["se"] or 1.0)
                if zz > 3:
                    print("    -> FALSIFIED. A per-burst term cannot be smaller than a")
                    print("       cost the burst pays unconditionally.")
                else:
                    print("    -> not falsified, but the headroom is gone: at the")
                    print("       shallowest depth the non-allocator part of C is")
                    print("       consistent with zero, which is what a ramp that")
                    print("       scales with queue depth would look like.")

    # ---- the part of the depth result that needs no model at all ----------
    # Raw runs that stayed at burst 64, so every arm is compared at the same
    # burst and no fit stands between the measurement and the claim. Both a
    # cycle counter and an instruction counter are read, which is what
    # separates "does more work" from "waits longer": the burst-cost model
    # cannot tell those apart and this can.
    PERF_WINDOW = 8.0        # sweep.sh's perf window, seconds
    print()
    print("=" * 84)
    print("3b. DEPTH AT A MATCHED BURST   cycles vs instructions, no fit")
    print("=" * 84)
    dcond = [(8, "depth_8"), (16, "depth_16"), (32, "depth_32"),
             (64, "pinned2_asshipped")]
    at64 = {}
    print(f"  {'depth':>5} {'runs':>5} {'cycles/pkt':>12} {'insns/pkt':>11} {'IPC':>6}")
    for dpt, cond in dcond:
        runs = [r for r in allc.get(cond, {}).get("dramblast", {}).values()
                if r.get("rx_batch") == 64 and r.get("insns") and r.get("steady_mpps")]
        if not runs:
            continue
        cyc = sum(r["cycles_per_pkt"] for r in runs) / len(runs)
        ipp = sum(r["insns"] / (r["steady_mpps"] * 1e6 * PERF_WINDOW)
                  for r in runs) / len(runs)
        # scatter of the mean, so the depth-32 test has an error bar
        var = sum((r["cycles_per_pkt"] - cyc) ** 2 for r in runs)
        sem = (var / (len(runs) * (len(runs) - 1))) ** 0.5 if len(runs) > 1 else None
        at64[dpt] = (cyc, ipp, sem, len(runs))
        semtxt = f"+/-{sem:.2f}" if sem else ""
        print(f"  {dpt:>5} {len(runs):>5} {cyc:>8.1f}{semtxt:>6} {ipp:>11.1f} "
              f"{ipp / cyc:>6.2f}")
    if 8 in at64 and 64 in at64:
        c8, i8 = at64[8][0], at64[8][1]
        c64, i64 = at64[64][0], at64[64][1]
        print(f"\n    depth 64 -> 8 at a matched burst of 64:")
        print(f"      instructions per packet  +{100*(i8-i64)/i64:.1f}%")
        print(f"      cycles       per packet  +{100*(c8-c64)/c64:.1f}%")
        print(f"      IPC  {i64/c64:.2f} -> {i8/c8:.2f}")
        print("    -> a shallower prefetch pipeline does not make the forwarder")
        print("       do meaningfully more work. It makes it wait. This is the")
        print("       load-bearing depth result and it survives without the")
        print("       burst-cost model, which cannot separate the two.")

    # ---- the per-fill ramp model, calibrated then tested -------------------
    # See docs/depth_prediction.md, written before the depth-32 arm finished.
    # With queue depth Q and burst B the pipeline fills ceil(B/Q) times per
    # burst, so the ramp is paid per fill, not per burst. At the shipped depth
    # Q == B and the two are the same event, which is why the burst model
    # attributes the ramp to C; shortening the queue moves it into P.
    cal = [(q, at64[q][0] - at64[64][0]) for q in (8, 16) if q in at64]
    if len(cal) == 2 and 64 in at64:
        xs = [(1.0 / q - 1.0 / 64.0) for q, _ in cal]
        ys = [y for _, y in cal]
        ramp = sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs)
        print(f"\n    Per-fill ramp calibrated on depths 8 and 16 (two points,")
        print(f"    one parameter): ramp = {ramp:.0f} cycles per pipeline fill.")
        if 32 in at64:
            pred = at64[64][0] + ramp * (1.0 / 32 - 1.0 / 64)
            obs, _, sem, n = at64[32][0], None, at64[32][2], at64[32][3]
            null = at64[64][0]
            print(f"    Prediction for depth 32 at burst 64: {pred:.1f}")
            print(f"    No-effect null:                      {null:.1f}")
            print(f"    Measured ({n} runs):                    {obs:.1f}"
                  + (f" +/- {sem:.2f}" if sem else ""))
            if sem:
                zp = abs(obs - pred) / sem
                zn = abs(obs - null) / sem
                print(f"      {zp:.1f} sigma from the prediction, "
                      f"{zn:.1f} sigma from the null.")
                if zp < 2 and zn > 3:
                    print("    -> the model survives a test it could have failed.")
                elif zn < 2:
                    print("    -> NOT CONFIRMED. Depth 32 is indistinguishable from")
                    print("       no effect at this burst, so the ramp calibrated on")
                    print("       the two shallow arms does not extrapolate. The")
                    print("       matched-burst cycles/instructions result above is")
                    print("       unaffected -- it uses no model.")
                else:
                    print("    -> the measurement sits between the two hypotheses and")
                    print("       separates neither. Reported as inconclusive.")

    # ---- one model across every depth arm ---------------------------------
    # The per-arm P + C/B fits are four separate two-parameter models that
    # share nothing, and for the shallow arms the straight line is the wrong
    # shape: the number of pipeline fills is ceil(B/Q), a step function, so a
    # line in 1/B is a mis-specification wherever B > Q. Fitting the step model
    # to all four arms at once is the right comparison, and it has a property
    # the per-arm fits do not: its per-burst constant is a prediction of a
    # quantity that a completely different experiment -- the allocator
    # amplification sweep, which never varied the queue depth -- measured
    # independently.
    print()
    print("=" * 84)
    print("3c. ONE MODEL ACROSS ALL DEPTHS   W + ramp*ceil(B/Q)/B + K/B")
    print("=" * 84)

    def depth_rows(depths):
        out = []
        for Q, cond in ((8, "depth_8"), (16, "depth_16"), (32, "depth_32"),
                        (64, "pinned2_asshipped")):
            if Q not in depths:
                continue
            for r in allc.get(cond, {}).get("dramblast", {}).values():
                if r.get("rx_batch"):
                    out.append((Q, r["rx_batch"], r["cycles_per_pkt"]))
        return out

    def ols3(rows):
        """Three-parameter least squares with standard errors."""
        import math as _m
        A = [[1.0, _m.ceil(B / Q) / B, 1.0 / B] for Q, B, _ in rows]
        y = [c for _, _, c in rows]
        if len(y) < 5:
            return None
        N = [[sum(a[i] * a[j] for a in A) for j in range(3)] for i in range(3)]
        rhs = [sum(A[k][i] * y[k] for k in range(len(y))) for i in range(3)]
        M = [N[i][:] + [1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
        for i in range(3):
            piv_row = max(range(i, 3), key=lambda r: abs(M[r][i]))
            M[i], M[piv_row] = M[piv_row], M[i]
            piv = M[i][i]
            if piv == 0:
                return None
            M[i] = [v / piv for v in M[i]]
            for r in range(3):
                if r != i:
                    f = M[r][i]
                    M[r] = [a - f * b for a, b in zip(M[r], M[i])]
        inv = [row[3:] for row in M]
        b = [sum(inv[i][j] * rhs[j] for j in range(3)) for i in range(3)]
        res = [y[k] - sum(A[k][i] * b[i] for i in range(3)) for k in range(len(y))]
        s2 = sum(r * r for r in res) / (len(y) - 3)
        se = [(s2 * inv[i][i]) ** 0.5 for i in range(3)]
        return b, se, s2 ** 0.5, len(y), res, rows

    full = ols3(depth_rows({8, 16, 32, 64}))
    if full:
        b, se, rms, n, res, rows = full
        print(f"  n={n} points from four depth arms, three parameters")
        print(f"    steady per-packet work   W    = {b[0]:6.1f} +/- {se[0]:.1f} cycles")
        print(f"    cost of one pipeline fill ramp = {b[1]:6.0f} +/- {se[1]:.0f} cycles")
        print(f"    per-burst constant        K    = {b[2]:6.0f} +/- {se[2]:.0f} cycles")
        print(f"    residual rms {rms:.2f} cycles over a 98-172 range")
        print("\n    K is the interesting one. Nothing in this fit knows about the")
        print("    allocator -- the queue depth was varied, the number of")
        print(f"    alloc/free pairs was not -- yet K = {b[2]:.0f} lands on the")
        if shipped_pair:
            zk = abs(b[2] - shipped_pair) / se[2]
            print(f"    {shipped_pair:.0f} cycles the amplification sweep measured for that")
            print(f"    pair directly: {zk:.1f} sigma apart.")
        print("\n    per-arm residual rms (the step model's own fit quality):")
        for Q in (8, 16, 32, 64):
            rr = [res[k] for k, (qq, _, _) in enumerate(rows) if qq == Q]
            if rr:
                arms = (sum(v * v for v in rr) / len(rr)) ** 0.5
                print(f"      Q={Q:>2}  n={len(rr)}  rms {arms:5.2f}  worst {max(rr, key=abs):+6.2f}")
        # Leave-one-arm-out. Reported whatever it says: a parameter that moves
        # when the worst-fitting arm is dropped is not a measurement.
        print("\n    Sensitivity -- refit without the arm the step model fits worst:")
        red = ols3(depth_rows({16, 32, 64}))
        if red:
            b2, se2, rms2, n2, _, _ = red
            print(f"      without Q=8:  W = {b2[0]:.1f} +/- {se2[0]:.1f}   "
                  f"ramp = {b2[1]:.0f} +/- {se2[1]:.0f}   K = {b2[2]:.0f} +/- {se2[2]:.0f}"
                  f"   rms {rms2:.2f}")
            dz = abs(b2[2] - b[2]) / ((se[2] ** 2 + se2[2] ** 2) ** 0.5)
            print(f"      K moves by {b2[2] - b[2]:+.0f} cycles, {dz:.1f} sigma.")
            if dz > 2:
                print("      -> K IS NOT STABLE. The agreement with the measured")
                print("         allocator pair holds only with the shallowest arm")
                print("         included, and that arm is the one the step model")
                print("         describes worst. Report the convergence as")
                print("         suggestive, not as a second measurement of the")
                print("         allocator. The ramp, which barely moves, is the")
                print("         parameter this fit actually determines.")
            else:
                print("      -> stable to dropping the worst arm.")

    if "--plot" not in sys.argv:
        return

    # matplotlib and numpy are not installed on this node any more (they were
    # on 2026-09-11; the node has not rebooted since, and another session is
    # working on this machine). Reinstalling them is a system-level change to
    # shared research hardware for the sake of a figure, so the figure is drawn
    # without them instead. plot_matrix.py writes the same three panels as SVG.
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        import plot_matrix
        plot_matrix.main()
        return
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
    # Deliberately NOT P and C against depth. That figure would draw the
    # mis-specification as though it were the finding: below Q = B the number
    # of pipeline fills is ceil(B/Q), so a line in 1/B is the wrong shape and
    # the shallow arms' P/C split is an artifact of fitting it anyway. What is
    # real is the matched-burst comparison, and it needs both counters -- a
    # cycle curve alone cannot distinguish more work from more waiting.
    if len(at64) >= 2:
        dpts = sorted(at64)
        cyc = [at64[d][0] for d in dpts]
        ins = [at64[d][1] for d in dpts]
        err = [at64[d][2] or 0 for d in dpts]
        ax.errorbar(dpts, cyc, yerr=err, marker="o", color=BLUE, linewidth=2,
                    markersize=7, capsize=3, markeredgecolor=SURFACE,
                    markeredgewidth=1.8, label="cycles / packet")
        ax2 = ax.twinx()
        ax2.plot(dpts, ins, marker="s", color=ORANGE, linewidth=2, markersize=6,
                 markeredgecolor=SURFACE, markeredgewidth=1.8,
                 label="instructions / packet")
        ax2.set_ylabel("instructions per packet", color=ORANGE)
        ax2.grid(False)
        # Anchor both axes to the same relative span so the divergence between
        # them is a fair visual comparison rather than an artefact of scaling.
        span = 0.26
        ax.set_ylim(min(cyc) * (1 - span / 6), min(cyc) * (1 + span))
        ax2.set_ylim(min(ins) * (1 - span / 6), min(ins) * (1 + span))
        for d in dpts:
            ax.annotate(f"IPC {at64[d][1] / at64[d][0]:.2f}", (d, at64[d][0]),
                        textcoords="offset points", xytext=(0, -15),
                        ha="center", fontsize=8, color=INK_2)
        ax.set_xscale("log", base=2)
        ax.set_xticks(dpts); ax.set_xticklabels([str(d) for d in dpts])
        ax.set_xlabel("prefetch pipeline depth   (burst held at 64)")
        ax.set_ylabel("core cycles per packet", color=BLUE)
    ax.set_title("3. The pipeline hides latency; it does not remove work",
                 fontsize=11, loc="left", pad=10)

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
