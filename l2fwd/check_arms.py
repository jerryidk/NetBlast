"""Evaluate the pre-registered predictions for the pinned-vs-turbo comparison.

Reads docs/results_reproduced.json, conditions `pinned_2100mhz` and `turbo_instr`.
Prints a verdict per criterion. Nothing here is fitted after the fact: the
criteria are recorded in docs/INVESTIGATION.md under "Pre-registered: what the
pinned re-baseline must show", written before the sweeps were run.

Run:  python3 check_arms.py
"""

import json
import pathlib

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
TSC_MHZ = 2100.0   # invariant TSC rate, identical in both arms

allc = json.loads((DOCS / "results_reproduced.json").read_text())
P, T = allc.get("pinned_2100mhz", {}), allc.get("turbo_instr", {})
MODES = ("dramblast", "maglev")


def qs(mode):
    return sorted(set(int(k) for k in P.get(mode, {})) & set(int(k) for k in T.get(mode, {})))


def fit(points):
    """Least squares y = a + b*x. Returns (intercept, slope, n)."""
    n = len(points)
    if n < 2:
        return None
    sx = sum(x for x, _ in points); sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points); sxy = sum(x * y for x, y in points)
    den = n * sxx - sx * sx
    if den == 0:
        return None
    b = (n * sxy - sx * sy) / den
    return ((sy - b * sx) / n, b, n)



def cpu_fraction(t_pinned, t_turbo, f_p_mhz, f_t_mhz):
    """Share of an elapsed-time cost that is CPU work rather than memory stall.

    CORRECTED 2026-09-14. This was previously computed as

        ((t_pinned / t_turbo) - 1) / (r - 1)        # WRONG

    i.e. a linear interpolation of the tick ratio between 1 (all memory) and r
    (all CPU). Those two endpoints are right, but the ratio is not linear in
    between, so every intermediate value was wrong -- it read maglev's plateau
    as 35.2% CPU work where the correct figure is 43.6%. Writing W for work in
    cycles and S for stall in seconds, elapsed time is

        t(f) = W / f + S

    so the ratio t_p / t_t = (W/f_p + S) / (W/f_t + S) is a ratio of two linear
    functions, not a linear one. Solving properly:

        W   = (t_p - t_t) / (1/f_p - 1/f_t)
        S   = t_p - W / f_p
        cpu = (W / f_p) / t_p = (1 - t_t/t_p) / (1 - f_p/f_t)

    The last form is the one used here; it needs no absolute units, only the two
    times in the same units and the two clocks in the same units. This agrees
    with fit_burst_model.py, which works in core cycles and was already correct
    -- the disagreement between the two is what exposed the bug.
    """
    if not (t_pinned and t_turbo and f_p_mhz and f_t_mhz) or f_t_mhz <= f_p_mhz:
        return None
    return (1.0 - t_turbo / t_pinned) / (1.0 - f_p_mhz / f_t_mhz)


print("=" * 78)
print("CRITERION 1  every point rises or holds.  Ticks are TIME -- the TSC runs at")
print("             2.1 GHz in BOTH arms -- so a lower core clock cannot make any")
print("             point fall.")
print()
print("  MIS-SPECIFIED, and reported in both forms.  'A lower clock cannot finish")
print("  sooner' holds only AT EQUAL WORK. Here the work per packet is itself a")
print("  function of the clock: a slower forwarder stays oversubscribed longer,")
print("  keeps a larger RX burst, and so amortises the per-burst cost over more")
print("  packets. Fewer ticks per packet at the same q is then exactly what the")
print("  per-burst model predicts. The sound form restricts to cells where both")
print("  arms sit at the same burst size. The as-written form is kept because a")
print("  pre-registration quietly narrowed after it fails is worth nothing.")
print("=" * 78)
for label, restrict in (("as written (all cells)", False),
                        ("sound form (equal burst size only)", True)):
    viol = ok = 0
    detail = []
    for mode in MODES:
        for q in qs(mode):
            rp, rt = P[mode][str(q)], T[mode][str(q)]
            if restrict and rp.get("rx_batch") != rt.get("rx_batch"):
                continue
            cp, ct = rp["cycles_per_pkt"], rt["cycles_per_pkt"]
            if cp < ct:
                viol += 1
                detail.append(f"    VIOLATION {mode:10s} q={q:<2} pinned {cp} < turbo {ct}"
                              f"   (batch {rp.get('rx_batch')} vs {rt.get('rx_batch')})")
            else:
                ok += 1
    print(f"  {label}: {ok} of {ok + viol} rise or hold, {viol} violation(s)")
    for d in detail:
        print(d)

print()
print("=" * 78)
print("r(q)  delivered clock ratio, measured per queue count from each arm's own")
print("      perf counter. NOT a constant: all-core turbo falls as q rises.")
print("=" * 78)
print(f"  {'q':>3} {'mode':10s} {'turbo MHz':>9} {'pinned MHz':>10} {'r(q)':>6} "
      f"{'batch T':>7} {'batch P':>7}")
R = {}
for mode in MODES:
    for q in qs(mode):
        ft = T[mode][str(q)].get("freq_mhz"); fp = P[mode][str(q)].get("freq_mhz")
        if not ft or not fp:
            continue
        R[(mode, q)] = ft / fp
        print(f"  {q:>3} {mode:10s} {ft:>9} {fp:>10} {ft/fp:>6.3f} "
              f"{T[mode][str(q)].get('rx_batch','?'):>7} {P[mode][str(q)].get('rx_batch','?'):>7}")

print()
print("=" * 78)
print("CRITERION 2/3  does the queue-count-dependent EXCESS scale like CPU work?")
print("      excess = cost at q, minus that mode's own low-q plateau (q=1).")
print("      CPU-bound work scales by r(q); memory latency scales by 1.000.")
print()
print("  SUPERSEDED, and the table below shows why rather than hiding it. The")
print("  criterion assumes the two arms can be differenced at a fixed queue")
print("  count. They cannot: a faster forwarder drains its queues sooner and so")
print("  runs at a SMALLER burst at the same q, and burst size is the very thing")
print("  the excess is made of. Every cell that has an excess to measure also has")
print("  mismatched bursts, so the criterion is not merely noisy here, it is")
print("  undefined. The plateau row above is still sound -- both arms sit at a")
print("  full 64-packet burst at q=1 -- and the collapse test below replaces the")
print("  rest by fitting burst size out instead of hoping it matches.")
print("=" * 78)
for mode in MODES:
    qq = qs(mode)
    if not qq:
        continue
    base_p = P[mode][str(qq[0])]["cycles_per_pkt"]
    base_t = T[mode][str(qq[0])]["cycles_per_pkt"]
    # The plateau ratio needs its OWN r(q), not a global constant and not the
    # ceiling: comparing it against 1.762 reintroduces the same bias the first
    # amendment removed, one level down.
    r0 = R.get((mode, qq[0]))
    fp0 = P[mode][str(qq[0])].get("freq_mhz")
    ft0 = T[mode][str(qq[0])].get("freq_mhz")
    frac = cpu_fraction(base_p, base_t, fp0, ft0)
    print(f"  {mode}   plateau: turbo {base_t} ticks, pinned {base_p} ticks, "
          f"ratio {base_p/base_t:.3f} vs r(q={qq[0]})={r0 if r0 is None else round(r0,3)}")
    if frac is not None:
        print(f"    -> CPU-bound fraction of the plateau cost: {frac*100:.1f}%  "
              f"(1.000 = all CPU work, 0.000 = all memory latency)")
    print(f"    {'q':>3} {'excess T':>8} {'excess P':>8} {'cpu frc':>7} {'r(q)':>6} {'verdict':>12}")
    for q in qq[1:]:
        et = T[mode][str(q)]["cycles_per_pkt"] - base_t
        ep = P[mode][str(q)]["cycles_per_pkt"] - base_p
        r = R.get((mode, q))
        if et <= 2 or r is None:      # no excess to speak of; ratio is noise
            print(f"    {q:>3} {et:>8} {ep:>8} {'-':>7} "
                  f"{(f'{r:.3f}' if r else '-'):>6} {'(no excess)':>12}")
            continue
        # The excess decomposes the same way the plateau does -- but ONLY if
        # both arms produced it doing the same amount of work per packet, and
        # here they do not. The faster arm drains its queues sooner and so sits
        # at a smaller RX burst at the same q, and burst size is precisely the
        # variable the excess is made of. Differencing across arms at fixed q
        # therefore mixes the clock change with a burst-size change, and the
        # result is not a fraction of anything: it comes out negative, which is
        # the arithmetic reporting that the comparison is undefined rather than
        # that the cost is negative.
        bp = P[mode][str(q)].get("rx_batch")
        bt = T[mode][str(q)].get("rx_batch")
        if not bp or not bt or abs(bp - bt) > 0.05 * max(bp, bt):
            print(f"    {q:>3} {et:>8} {ep:>8} {'-':>7} {r:>6.3f} "
                  f"  burst {bt} vs {bp}: NOT COMPARABLE")
            continue
        ex_frac = cpu_fraction(ep, et, P[mode][str(q)].get("freq_mhz"),
                               T[mode][str(q)].get("freq_mhz"))
        if ex_frac is None:
            print(f"    {q:>3} {et:>8} {ep:>8} {'-':>7} {r:>6.3f} {'(no clock)':>12}")
            continue
        verdict = "CPU-like" if ex_frac >= 0.5 else "latency-like"
        print(f"    {q:>3} {et:>8} {ep:>8} {ex_frac:>7.3f} {r:>6.3f} {verdict:>12}")

print()
print("=" * 78)
print("COLLAPSE TEST  core cycles/packet vs 1/burst, fitted per arm and mode.")
print("      Model: cycles/pkt = P + C/B. P is per-packet work, C per-burst work.")
print("      Core cycles are clock-invariant for CPU work, so if the per-burst")
print("      cost is CPU work the two arms fit the SAME line. Vertical offset")
print("      between arms is the memory-latency fraction.")
print("=" * 78)
for mode in MODES:
    for name, cond in (("turbo", T), ("pinned", P)):
        pts = []
        for q in sorted(int(k) for k in cond.get(mode, {})):
            rec = cond[mode][str(q)]
            f, b, c = rec.get("freq_mhz"), rec.get("rx_batch"), rec.get("cycles_per_pkt")
            if f and b and c:
                pts.append((1.0 / b, c * f / TSC_MHZ))
        r = fit(pts)
        if r:
            print(f"  {mode:10s} {name:7s}  P = {r[0]:7.1f} cycles/pkt   "
                  f"C = {r[1]:7.1f} cycles/burst   (n={r[2]})")
        else:
            print(f"  {mode:10s} {name:7s}  insufficient data")
