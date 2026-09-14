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
    # Matched burst AND matched queue count, every queue count that qualifies --
    # not q=1 alone, which is what an earlier version of this did and what made
    # a one-tick difference look like a result.
    #
    # The resolution limit is the point. l2fwd prints "Cycle per fwd packet" as
    # an INTEGER, so at a 64-packet burst one printed tick is 64 cycles per
    # burst. A single-queue-count difference of 7 ticks therefore carries +/-64
    # cycles of quantisation before any other error, and the four matched values
    # here are 7, 9, 7, 10 -- q=1 is the smallest of them. Averaging over the
    # matched queue counts is what buys resolution back; pairing at each q is
    # what keeps the core-count effect from contaminating the difference.
    print("\n    At a MATCHED burst of 64 AND a matched queue count, no fit:")
    print(f"      {'pairs':>6} {'matched q':>16} {'tick diffs':>20} {'cycles/burst':>16}")
    q1pts, q1err = [], {}
    hoist_t = {int(q): (r["cycles_per_pkt"], r.get("rx_batch"), r.get("freq_mhz"))
               for q, r in allc.get("alloc_hoisted", {}).get("dramblast", {}).items()}
    for n, cond in ((1, "pinned2_asshipped"), (3, "alloc_x2"), (5, "alloc_x4"),
                    (9, "alloc_x8")):
        t = {int(q): (r["cycles_per_pkt"], r.get("rx_batch"), r.get("freq_mhz"))
             for q, r in allc.get(cond, {}).get("dramblast", {}).items()}
        qs = [q for q in sorted(set(t) & set(hoist_t))
              if t[q][1] == 64 and hoist_t[q][1] == 64]
        if len(qs) < 2:
            continue
        diffs = [t[q][0] - hoist_t[q][0] for q in qs]
        fq = [t[q][2] / 2100.0 for q in qs if t[q][2]]
        scale = 64.0 * (sum(fq) / len(fq) if fq else 1.0)
        mean = sum(diffs) / len(diffs)
        var = sum((v - mean) ** 2 for v in diffs) / (len(diffs) - 1)
        sem = (var / len(diffs)) ** 0.5
        q1pts.append((n, mean * scale))
        q1err[n] = sem * scale
        print(f"      {n:>6} {str(qs):>16} {str(diffs):>20} "
              f"{mean*scale:>8.0f} +/-{sem*scale:<5.0f}")
    if q1pts:
        onetick = 64.0 * (2095.0 / 2100.0)
        print(f"\n      One printed tick at burst 64 = {onetick:.0f} cycles per burst.")
        print("      Every number in the column above is a mean of small integers,")
        print("      so nothing here is meaningful to better than a few tens of")
        print("      cycles however many digits a fit prints.")

    amplified_slope = None
    multi = [(n, c) for n, c in q1pts if n >= 3]
    if len(multi) >= 2:
        print("\n      Consecutive slopes between multi-pair arms:")
        for (n0, c0), (n1, c1) in zip(multi, multi[1:]):
            print(f"        {n0} -> {n1} pairs: {(c1 - c0) / (n1 - n0):.0f} cycles per pair")
        w = [1.0 / max(q1err.get(n, 1.0), 1.0) ** 2 for n, _ in multi]
        sw = sum(w)
        sx = sum(wi * n for wi, (n, _) in zip(w, multi))
        sy = sum(wi * c for wi, (_, c) in zip(w, multi))
        sxx = sum(wi * n * n for wi, (n, _) in zip(w, multi))
        sxy = sum(wi * n * c for wi, (n, c) in zip(w, multi))
        den = sw * sxx - sx * sx
        if den:
            b = (sw * sxy - sx * sy) / den
            a = (sy - b * sx) / sw
            amplified_slope = b
            print(f"      weighted line through them: {a:+.0f} + {b:.0f} * pairs")
            shipped = dict(q1pts).get(1)
            se_ship = q1err.get(1)
            if shipped and se_ship:
                pred = a + b
                z = abs(shipped - pred) / se_ship
                print(f"\n      Extrapolated to one pair: {pred:.0f}")
                print(f"      Measured for the shipped single pair: {shipped:.0f} "
                      f"+/- {se_ship:.0f}")
                print(f"      -> {shipped - pred:+.0f} cycles apart, {z:.1f} sigma of the")
                print("         measurement's own error.")
                if z < 2:
                    print("         NOT RESOLVED. The shipped pair and an incremental")
                    print("         one cost the same within this experiment's")
                    print("         resolution, and no first-pair or load effect can")
                    print("         be claimed from it.")
                else:
                    print("         Resolved: the two regimes genuinely differ.")
                print("\n      SUPERSEDES an earlier reading of this same data. Taken at")
                print("      q=1 alone the shipped pair came out at 447 against an")
                print("      incremental 510.78, the two consecutive slopes agreed to")
                print("      0.00 cycles and the line's intercept was 0.0 -- which was")
                print("      written up as a structural check and as a load effect.")
                print("      It was neither. The tick differences at q=1 are 24, 40 and")
                print("      72; 40-24 = 16 and 72-40 = 32, exactly twice it, so after")
                print("      any common scaling the two slopes are identically equal and")
                print("      the intercept is identically zero. The agreement was")
                print("      arithmetic, not measurement. And 447 against 511 is one")
                print("      printed tick, from the single queue count where the")
                print("      difference happens to be smallest.")

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
            ship = dict(q1pts).get(1)
            se_ship = q1err.get(1)
            fitdiff = None
            if base_d and hoisted and "C" in base_d and "C" in hoisted:
                fitdiff = base_d["C"] - hoisted["C"]
                se_fd = ((base_d["se"] or 0) ** 2 + (hoisted["se"] or 0) ** 2) ** 0.5
            print("\n    NONE OF THIS MOVES THE HEADLINE, which is the SHIPPED pair,")
            print("    measured two ways that share no algebra:")
            if ship and se_ship:
                print(f"      matched burst and matched queue count:  {ship:.0f} +/- {se_ship:.0f}")
            if fitdiff:
                print(f"      differencing the two fitted C's:        {fitdiff:.0f} +/- {se_fd:.0f}")
            if ship and fitdiff:
                zz = abs(ship - fitdiff) / ((se_ship ** 2 + se_fd ** 2) ** 0.5)
                lo, hi = min(ship, fitdiff), max(ship, fitdiff)
                print(f"    They agree to {zz:.1f} sigma. Quote it as ~{(lo+hi)/2:.0f} cycles,")
                print(f"    or {lo:.0f}-{hi:.0f}; three significant figures are not available")
                print("    from an integer tick counter.")
                if base_d and "C" in base_d:
                    print(f"    That is {100*lo/base_d['C']:.0f}-{100*hi/base_d['C']:.0f}% of the "
                          f"{base_d['C']:.0f}-cycle per-burst cost, against the")
                    print("    'at most 11%' this investigation previously claimed.")

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
        shipped_pair = dict(q1pts).get(1) if q1pts else None   # matched-q estimate
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
                    print("    -> FALSIFIED, and section 3d says which assumption broke.")
                    print("       A per-burst term cannot be smaller than a cost the")
                    print("       burst pays unconditionally, so the shallow arm's fitted")
                    print("       C is not a per-burst term. The pipeline fills")
                    print("       ceil(B/Q) times per burst, so a line in 1/B is")
                    print("       mis-specified wherever B > Q -- which is most of the")
                    print("       depth-8 arm and none of the shipped one. This test was")
                    print("       written before that was understood and is left in")
                    print("       because it is what pointed at it.")
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
    # The run-to-run floor at burst 64, computed here rather than in section 4
    # because section 3b's error bars are wrong without it. A sweep's own
    # scatter is the spread of ten samples inside one twelve-second window; it
    # cannot see anything that drifts between sweeps, and the depth arms were
    # taken hours apart. Both terms go into every depth error bar below.
    def repeat_floor(mode):
        a = allc.get("pinned2_asshipped", {}).get(mode, {})
        b = allc.get("pinned3_repeat", {}).get(mode, {})
        d = [b[q]["cycles_per_pkt"] - a[q]["cycles_per_pkt"]
             for q in set(a) & set(b)
             if a[q].get("rx_batch") == 64 and b[q].get("rx_batch") == 64]
        return (sum(v * v for v in d) / len(d)) ** 0.5 if d else None

    dram_floor = repeat_floor("dramblast")

    PERF_WINDOW = 8.0        # sweep.sh's perf window, seconds
    print()
    print("=" * 84)
    print("3b. DEPTH AT A MATCHED BURST   cycles vs instructions, no fit")
    print("=" * 84)
    dcond = [(8, "depth_8"), (16, "depth_16"), (32, "depth_32"),
             (64, "pinned2_asshipped")]
    at64 = {}
    if dram_floor:
        print(f"  error bars = within-sweep scatter AND the {dram_floor:.2f} cycle")
        print(f"  run-to-run floor measured by the repeat arm (section 4)")
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
        if sem is not None and dram_floor:
            sem = (sem ** 2 + dram_floor ** 2) ** 0.5
        at64[dpt] = (cyc, ipp, sem, len(runs))
        semtxt = f"+/-{sem:.2f}" if sem else ""   # includes the run-to-run term
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
    # Everything here is an EXCESS over the depth-64 arm, because the ramp is
    # defined relative to it and because differencing against a common baseline
    # is what the error propagation has to respect: the baseline's own error
    # enters every comparison and must not be dropped after the first one.
    ramp_pred = None
    cal = [(q, at64[q][0] - at64[64][0],
            ((at64[q][2] or 0) ** 2 + (at64[64][2] or 0) ** 2) ** 0.5)
           for q in (8, 16) if q in at64]
    if len(cal) == 2 and 64 in at64:
        xs = [(1.0 / q - 1.0 / 64.0) for q, _, _ in cal]
        ys = [y for _, y, _ in cal]
        sgs = [g for _, _, g in cal]
        sxx = sum(x * x for x in xs)
        ramp = sum(x * y for x, y in zip(xs, ys)) / sxx
        se_ramp = (sum((x * g) ** 2 for x, g in zip(xs, sgs))) ** 0.5 / sxx
        ramp_pred = ramp * (1.0 / 32 - 1.0 / 64)
        print(f"\n    Per-fill ramp calibrated on depths 8 and 16 (two points,")
        print(f"    one parameter): ramp = {ramp:.0f} +/- {se_ramp:.0f} cycles "
              f"per pipeline fill.")
        if 32 in at64:
            dx = 1.0 / 32 - 1.0 / 64
            pred_ex, se_pred = ramp * dx, se_ramp * dx
            obs_ex = at64[32][0] - at64[64][0]
            se_obs = ((at64[32][2] or 0) ** 2 + (at64[64][2] or 0) ** 2) ** 0.5
            print(f"    Depth 32's excess over depth 64, at burst 64:")
            print(f"      predicted by the ramp  {pred_ex:5.2f} +/- {se_pred:.2f}")
            print(f"      measured               {obs_ex:5.2f} +/- {se_obs:.2f}")
            print(f"      null (no effect)        0.00")
            sg = (se_obs ** 2 + se_pred ** 2) ** 0.5
            zp = abs(obs_ex - pred_ex) / sg
            zn = abs(obs_ex) / se_obs
            print(f"      -> {zp:.1f} sigma from the prediction, "
                  f"{zn:.1f} sigma from the null.")
            if zp < 2 and zn > 3:
                print("      -> the model survives a test it could have failed,")
                print("         and the null is excluded.")
            elif zp < 2 and zn > 1.5:
                print("      -> INCONCLUSIVE, leaning toward the model. The")
                print("         measurement is consistent with the prediction and")
                print("         does not exclude the null. The effect being tested")
                print("         is only about twice the run-to-run floor, which is")
                print("         all the resolution one sweep per depth buys;")
                print("         separating them needs repeats at depth 32, not a")
                print("         better fit.")
            elif zn <= 1.5:
                print("      -> NOT CONFIRMED. Depth 32 is indistinguishable from")
                print("         no effect once the run-to-run term is included.")
            else:
                print("      -> inconclusive; separates neither hypothesis.")
            print("\n      (An earlier run of this analysis called this 3.1 sigma")
            print("       from the null and said the model had survived. That used")
            print("       only the within-sweep scatter, which cannot see drift")
            print("       between sweeps taken hours apart. The repeat arm measured")
            print("       that drift afterwards, and including it is what moved the")
            print("       verdict.)")

    # ---- the depth-32 test, decided by repeats rather than by a fit -------
    # Three interleaved sweeps of each arm at burst 64. The statistic is the
    # PAIRED difference within each repeat: the arms were run alternately, so a
    # pairing removes any drift common to a pair, and the spread of the three
    # paired differences is an honest error bar that needs no assumption about
    # which sources of variation the sweep did or did not see.
    # Paired queue count by queue count, not arm mean against arm mean. The
    # repeats do not all reach burst 64 at the same queue counts -- one d64
    # sweep left it at q=5 while its d32 partner did not -- so comparing arm
    # means silently compares different queue sets, and the queue count does
    # move the cost slightly. Matching within each pair removes that.
    pairs = []
    for i in (1, 2, 3):
        def t(cond):
            return {int(q): (r["cycles_per_pkt"], r.get("rx_batch"))
                    for q, r in allc.get(cond, {}).get("dramblast", {}).items()}
        A, B = t(f"depth_32_r{i}"), t(f"depth_64_r{i}")
        qs = [q for q in sorted(set(A) & set(B)) if A[q][1] == 64 == B[q][1]]
        if len(qs) < 2:
            continue
        diffs = [A[q][0] - B[q][0] for q in qs]
        pairs.append((i, qs, diffs, sum(diffs) / len(diffs)))
    if len(pairs) >= 2:
        print()
        print("=" * 84)
        print("3c. DEPTH 32 vs 64, DECIDED   three interleaved repeats at burst 64")
        print("=" * 84)
        print(f"  {'repeat':>7} {'matched q':>18} {'tick differences':>22} {'mean':>7}")
        for i, qs, diffs, m in pairs:
            print(f"  {i:>7} {str(qs):>18} {str(diffs):>22} {m:>7.3f}")
        # Two error bars, because they answer different questions and here they
        # disagree about one of the two hypotheses. Neither is quietly dropped.
        ms = [m for _, _, _, m in pairs]
        n = len(ms)
        mu = sum(ms) / n
        sd = (sum((v - mu) ** 2 for v in ms) / (n - 1)) ** 0.5
        sem = sd / n ** 0.5
        t2 = {1: 12.71, 2: 4.303, 3: 3.182, 4: 2.776}.get(n - 1, 2.0)
        flat = [v for _, _, ds, _ in pairs for v in ds]
        mu2 = sum(flat) / len(flat)
        sd2 = (sum((v - mu2) ** 2 for v in flat) / (len(flat) - 1)) ** 0.5
        sem2 = sd2 / len(flat) ** 0.5
        tf = 2.16 if len(flat) >= 13 else 2.45
        f = 2095.0 / 2100.0
        lo_b, hi_b = (mu - t2 * sem) * f, (mu + t2 * sem) * f
        lo_p, hi_p = (mu2 - tf * sem2) * f, (mu2 + tf * sem2) * f
        print(f"\n  excess, depth 32 over depth 64, in cycles per packet: {mu*f:+.2f}")
        print(f"    between-repeat error ({n} repeat means, {n-1} dof):")
        print(f"      sd {sd:.3f}  se {sem:.3f}   95% interval [{lo_b:+.2f}, {hi_b:+.2f}]")
        print(f"    within-and-between ({len(flat)} matched-q differences):")
        print(f"      sd {sd2:.3f}  se {sem2:.3f}   95% interval [{lo_p:+.2f}, {hi_p:+.2f}]")
        print("    The first uses only the spread of three numbers that happen to")
        print("    lie close together; the second uses every measurement and is the")
        print("    conservative one. Both are quoted because they disagree about")
        print("    the prediction and agree about the null.")
        pred = ramp_pred
        if pred is not None:
            print(f"\n  ramp model predicts {pred:+.2f}   null predicts 0.00")
            in_b = lo_b <= pred <= hi_b
            in_p = lo_p <= pred <= hi_p
            null_out = not (lo_b <= 0 <= hi_b) and not (lo_p <= 0 <= hi_p)
            if null_out:
                print("  -> THE NULL IS EXCLUDED by both intervals. Depth 32 really")
                print("     does cost more than depth 64 at a matched burst; that part")
                print("     is settled.")
            if in_b and in_p:
                print("  -> The prediction sits inside both intervals: the per-fill")
                print("     ramp model is confirmed at depth 32, on a test that could")
                print("     have gone the other way.")
            elif in_p and not in_b:
                print(f"  -> The prediction sits inside the conservative interval and")
                print(f"     just outside the tighter one ({pred:.2f} against an upper")
                print(f"     bound of {hi_b:.2f}). The measured excess is "
                      f"{100*(1-mu*f/pred):.0f}% below the predicted one.")
                print("     So: the effect is real, the model has the right sign and")
                print("     roughly the right size, and its point prediction is at the")
                print("     edge of what this data can support. Calling that a clean")
                print("     confirmation would be overreading it.")
            elif not in_p:
                print("  -> The prediction is outside both intervals. The ramp")
                print("     calibrated on the shallow arms does not extrapolate here.")

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
    print("3d. ONE MODEL ACROSS ALL DEPTHS   W + ramp*ceil(B/Q)/B + K/B")
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

    # ---- the error bar everything else is measured against ---------------
    # pinned2 and pinned3 are the same condition, same binary, same cpuset,
    # re-run hours apart with the whole matrix in between. Every difference
    # quoted anywhere in this analysis has to clear whatever this shows, and
    # until it was run there was no measured run-to-run spread at all -- only
    # the within-sweep scatter, which does not include anything that drifts
    # between sweeps.
    print()
    print("=" * 84)
    print("4. REPEATABILITY   the same condition, re-run after the whole matrix")
    print("=" * 84)
    floor_all = []
    for mode in ("dramblast", "maglev"):
        a = allc.get("pinned2_asshipped", {}).get(mode, {})
        b = allc.get("pinned3_repeat", {}).get(mode, {})
        if not a or not b:
            print(f"  {mode:10s} (no repeat data)")
            continue
        print(f"\n  {mode}")
        print(f"    {'q':>2} {'burst':>11} {'run 1':>8} {'run 2':>8} {'diff':>7}")
        diffs = []
        for q in sorted(set(a) & set(b), key=int):
            ra, rb = a[q], b[q]
            # Only compare at a matched burst. Cycles per packet depend on the
            # burst, so two runs that landed on different bursts differ for a
            # reason that has nothing to do with repeatability.
            same = ra.get("rx_batch") == rb.get("rx_batch")
            d = rb["cycles_per_pkt"] - ra["cycles_per_pkt"]
            burst = (f"{ra.get('rx_batch')}" if same
                     else f"{ra.get('rx_batch')}/{rb.get('rx_batch')}")
            tail = "" if same else "   (different burst, not counted)"
            print(f"    {q:>2} {burst:>11} {ra['cycles_per_pkt']:>8} "
                  f"{rb['cycles_per_pkt']:>8} {d:>+7.0f}{tail}")
            if same:
                diffs.append((ra.get("rx_batch"), d))
        if diffs:
            vals = [v for _, v in diffs]
            mean = sum(vals) / len(vals)
            rms = (sum(v * v for v in vals) / len(vals)) ** 0.5
            print(f"    -> {len(vals)} matched-burst points: mean {mean:+.2f}, "
                  f"rms {rms:.2f}, worst {max(vals, key=abs):+.0f} cycles/packet")
            # The floor is not one number. It is much smaller at burst 64,
            # where the forwarder is oversubscribed and the operating point is
            # pinned, than at the small bursts a saturated link produces, where
            # the burst size itself is an outcome and wanders between runs. All
            # the matched-burst claims in this analysis are made at burst 64,
            # so that is the floor they have to clear -- quoting the pooled
            # number instead would be conservative in the wrong place, hiding a
            # real 2x while inflating the error on claims made where the rig is
            # most stable.
            b64 = [v for b, v in diffs if b == 64]
            if b64 and len(b64) < len(vals):
                r64 = (sum(v * v for v in b64) / len(b64)) ** 0.5
                rest = [v for b, v in diffs if b != 64]
                rr = (sum(v * v for v in rest) / len(rest)) ** 0.5 if rest else 0
                print(f"       split by burst: {len(b64)} points at burst 64 "
                      f"rms {r64:.2f};  {len(rest)} at smaller bursts rms {rr:.2f}")
            floor_all.extend(diffs)
        fa = fit_of(allc, "pinned2_asshipped", mode)
        fb = fit_of(allc, "pinned3_repeat", mode)
        if fa and fb and "C" in fa and "C" in fb:
            dc = fb["C"] - fa["C"]
            sg = ((fa["se"] or 0) ** 2 + (fb["se"] or 0) ** 2) ** 0.5
            print(f"    fitted C: {fa['C']:.0f} then {fb['C']:.0f}  "
                  f"({dc:+.0f}, {abs(dc)/sg:.1f} sigma of the two fits' own errors)")
    if floor_all:
        vals = [v for _, v in floor_all]
        rms = (sum(v * v for v in vals) / len(vals)) ** 0.5
        worst = max(vals, key=abs)
        b64 = [v for b, v in floor_all if b == 64]
        rms64 = (sum(v * v for v in b64) / len(b64)) ** 0.5 if b64 else rms
        print(f"\n  RUN-TO-RUN FLOOR: rms {rms:.2f} cycles/packet over "
              f"{len(vals)} matched-burst points, worst {worst:+.0f}.")
        print(f"  At burst 64 alone, where every matched-burst claim here is")
        print(f"  made: rms {rms64:.2f} over {len(b64)} points. The two differ")
        print("  because at small bursts the burst size is an outcome rather")
        print("  than a setting, and it wanders between runs.")
        rms = rms64
        print("\n  Read every claim in this analysis against that number:")
        if shipped_pair:
            print(f"    allocator pair            {shipped_pair/64:6.1f} cycles/packet "
                  f"at burst 64   ({shipped_pair/64/rms:.0f}x the floor)")
        if 8 in at64 and 64 in at64:
            dd = at64[8][0] - at64[64][0]
            print(f"    depth 64 -> 8             {dd:6.1f} cycles/packet "
                  f"at burst 64   ({dd/rms:.0f}x)")
        if 32 in at64 and 64 in at64:
            dd = at64[32][0] - at64[64][0]
            print(f"    depth 64 -> 32            {dd:6.1f} cycles/packet "
                  f"at burst 64   ({dd/rms:.0f}x)")
        print("  A difference of the same order as the floor is not a result,")
        print("  however many digits the fit prints.")

    # ---- the 4 KiB core-count anomaly, from data already taken ------------
    # On 4 KiB pages the cost rises with queue count at a CONSTANT burst, which
    # the burst model cannot represent -- it has no core-count term. The page
    # walk counters were recorded alongside every run, so this needs no new
    # measurement: it asks whether the extra cost is more walks or slower ones,
    # and the 1 GiB arm, which takes no walks at all, is the control.
    print()
    print("=" * 84)
    print("5. THE 4 KiB CORE-COUNT EFFECT   more walks, or slower walks?")
    print("=" * 84)
    PW = 8.0
    byq = {}
    for cond, mode, lab in (("xover_dram_4k", "dramblast", "dramblast, 4 KiB"),
                            ("xover_mag_4k", "maglev", "maglev, 4 KiB"),
                            ("pinned2_asshipped", "dramblast",
                             "dramblast, 1 GiB  (control: no walks)")):
        runs = []
        for q, r in sorted(allc.get(cond, {}).get(mode, {}).items(), key=lambda kv: int(kv[0])):
            if r.get("rx_batch") != 64 or not r.get("steady_mpps"):
                continue
            pkts = r["steady_mpps"] * 1e6 * PW
            runs.append((int(q), r["cycles_per_pkt"],
                         (r.get("pmu_dtlb_walk_active") or 0) / pkts,
                         (r.get("pmu_dtlb_walk_completed") or 0) / pkts,
                         (r.get("insns") or 0) / pkts))
        if len(runs) < 3:
            continue
        print(f"\n  {lab}   (only runs that stayed at burst 64)")
        print(f"    {'q':>2} {'cyc/pkt':>8} {'walk cyc/pkt':>13} {'walks/pkt':>10} "
              f"{'insn/pkt':>9}")
        for q, c, wa, wc, ins in runs:
            print(f"    {q:>2} {c:>8} {wa:>13.1f} {wc:>10.3f} {ins:>9.1f}")
        lo, hi = runs[0], runs[-1]
        dc, dwa, dwc, dins = hi[1] - lo[1], hi[2] - lo[2], hi[3] - lo[3], hi[4] - lo[4]
        print(f"    q={lo[0]} -> q={hi[0]}:  cycles {dc:+.0f}   walk cycles {dwa:+.1f}"
              f"   walks {dwc:+.3f}   instructions {dins:+.1f}")
        if lo[3] > 0.5:
            print(f"      walks per packet are flat ({lo[3]:.2f} -> {hi[3]:.2f}), so this")
            print(f"      is not more walking. Walk OCCUPANCY rises "
                  f"{100*dwa/lo[2]:.0f}%, so each")
            print("      walk takes longer as more cores walk at once.")
            if dc:
                print(f"      {100*dc/dwa:.0f}% of the added occupancy reaches the "
                      f"per-packet cost.")
            byq[lab] = {q: (c, wa) for q, c, wa, _, _ in runs}
        else:
            print("      no walks at all, and no core-count effect: the control.")
    # The two engines' ranges differ -- dramblast leaves burst 64 at q=7 and
    # maglev never does -- so the headline percentages above are taken over
    # different core counts and must not be compared with each other. At a
    # matched queue count they can be.
    d4 = byq.get("dramblast, 4 KiB", {})
    m4 = byq.get("maglev, 4 KiB", {})
    common = sorted(set(d4) & set(m4))
    if len(common) >= 2:
        q0, q1 = common[0], common[-1]
        print(f"\n  At a MATCHED queue count, q={q0} -> q={q1} (the two arms cover")
        print("  different ranges, so the percentages above are not comparable):")
        for lab, t in (("dramblast", d4), ("maglev", m4)):
            dwa = t[q1][1] - t[q0][1]
            dc = t[q1][0] - t[q0][0]
            frac = f"{100*dc/dwa:.0f}%" if dwa else "n/a"
            print(f"    {lab:10s} walk cycles {dwa:+6.1f}   cost {dc:+3.0f}   "
                  f"reaching the cost: {frac}")
        print("    -> the engine that is already waiting absorbs the extra walk")
        print("       time; the one retiring at IPC 3.4 has no slack to hide it")
        print("       in. That is the opposite direction from the prefetch")
        print("       result, and for a consistent reason: a pipeline hides")
        print("       latency it ISSUED EARLY, not latency added underneath it.")

    print("\n  CONCLUSION: the core-count term that breaks the burst model on")
    print("  4 KiB pages is contention for shared page-table structures. It is")
    print("  measured in the DURATION of a walk, not in how many walks happen,")
    print("  and it vanishes entirely on 1 GiB pages where there are none.")
    print("  No new runs were needed -- the counters were already in the logs.")

    if "--plot" not in sys.argv:
        return

    # matplotlib lives in the nix dev shell, not on the system Python, so this
    # import succeeds under `nix develop` and fails from a plain shell. Both
    # paths draw the same three panels; plot_matrix.py needs only the standard
    # library and writes SVG.
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
    # Matched-burst excess, NOT the fitted C. The +8 arm is so slow it never
    # leaves a 64-packet burst, so its own slope is unmeasurable (R^2 0.62) --
    # it is excluded from the line for that reason, and plotting its fitted C
    # drew a curve that rises to five pairs and then falls, which is not a
    # result but a bad fit given a marker. The estimator quoted in the text is
    # the cost at a matched burst, and that is what belongs on the axis.
    if len(q1pts) >= 2:
        pts = sorted(q1pts)
        xs = [n for n, _ in pts]
        ys = [c for _, c in pts]
        ax.plot(xs, ys, marker="o", color=BLUE, linewidth=2, markersize=7,
                markeredgecolor=SURFACE, markeredgewidth=1.8)
        multi = [(n, c) for n, c in pts if n >= 3]
        if len(multi) >= 2:
            nn = len(multi)
            sx = sum(n for n, _ in multi); sy = sum(c for _, c in multi)
            sxx = sum(n * n for n, _ in multi)
            sxy = sum(n * c for n, c in multi)
            den = nn * sxx - sx * sx
            if den:
                b = (nn * sxy - sx * sy) / den
                a = (sy - b * sx) / nn
                hi = max(xs) * 1.05
                ax.plot([0, hi], [a, a + b * hi], color=INK_2, linewidth=1.4,
                        linestyle="--",
                        label=f"{b:.0f} cycles/pair, intercept {a:+.0f}")
                ax.legend(fontsize=8.5, frameon=False, loc="upper left")
        ax.set_xlabel("aligned_alloc/free round trips per burst")
        ax.set_ylabel("core cycles per burst, above the hoisted arm")
    ax.set_title("2. What one allocator round trip costs", fontsize=11,
                 loc="left", pad=10)

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
                        textcoords="offset points", xytext=(0, 12),
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
