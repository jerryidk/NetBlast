"""Generate docs/report.html — the findings page — from the measured results.

Everything on the page is read from docs/results_reproduced.json and
docs/results.json at build time, so the report cannot drift from the data. If a
condition has not been measured, its section is omitted rather than stubbed.

Run:  nix develop .. -c python3 make_report.py
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from analyse_matrix import fit_of            # noqa: E402  (fit errors, shared)

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
TSC_MHZ = 2100.0


# ---------------------------------------------------------------- data helpers
def load():
    return (json.loads((DOCS / "results.json").read_text()),
            json.loads((DOCS / "results_reproduced.json").read_text()))


def core_cycles(rec):
    f = rec.get("freq_mhz")
    if f is None or "cycles_per_pkt" not in rec:
        return None
    return rec["cycles_per_pkt"] * f / TSC_MHZ


def series(cond, mode):
    out = []
    for q in sorted(cond.get(mode, {}), key=int):
        r = cond[mode][q]
        y, b = core_cycles(r), r.get("rx_batch")
        if y and b:
            out.append((1.0 / b, y, int(q), b, r))
    return out


def lsq(pts):
    n = len(pts)
    if n < 3:
        return None
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    den = n * sxx - sx * sx
    if not den:
        return None
    C = (n * sxy - sx * sy) / den
    P = (sy - C * sx) / n
    ybar = sy / n
    sst = sum((p[1] - ybar) ** 2 for p in pts)
    ssr = sum((p[1] - (P + C * p[0])) ** 2 for p in pts)
    return P, C, (1 - ssr / sst if sst else float("nan"))


def decompose(xp, xt, fp, ft):
    """(work cycles, stall ns, memory-bound share) from one coefficient in two arms."""
    T = (xt - xp) / ((ft - fp) / 1000.0)
    stall = T * fp / 1000.0
    return xp - stall, T, stall / xp


def armfreq(cond, mode):
    fs = [r["freq_mhz"] for r in cond.get(mode, {}).values() if r.get("freq_mhz")]
    return sum(fs) / len(fs) if fs else None


# ------------------------------------------------------------------ svg pieces
def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ylabel(x, top, bottom, text):
    """Rotated y-axis caption, centred on the plot area.

    rotate(-90) makes text run upward from its anchor point, so anchoring at
    the top of the plot puts the whole string above the viewBox. Every one of
    these was clipped. Centre it and anchor in the middle instead, which is
    also where a reader looks for it.
    """
    cy = (top + bottom) / 2
    return (f'<text x="{x}" y="{cy:.1f}" text-anchor="middle" class="axis" '
            f'transform="rotate(-90 {x} {cy:.1f})">{esc(text)}</text>')


def chart_collapse(old, new):
    """The reported collapse, against the corrected invocation."""
    W, H = 720, 300
    L, R, T, B = 52, 16, 18, 44
    qs = list(range(1, 11))
    ymax = 100
    x = lambda q: L + (q - 1) * (W - L - R) / 9
    y = lambda v: T + (1 - min(v, ymax) / ymax) * (H - T - B)
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Reported throughput '
         f'collapse versus the corrected invocation">']
    for v in (0, 25, 50, 75, 100):
        p.append(f'<line x1="{L}" y1="{y(v):.1f}" x2="{W-R}" y2="{y(v):.1f}" '
                 f'stroke="var(--rule)" stroke-width="1"/>')
        p.append(f'<text x="{L-9}" y="{y(v)+4:.1f}" text-anchor="end" class="tick">{v}</text>')
    for q in qs:
        p.append(f'<text x="{x(q):.1f}" y="{H-B+20}" text-anchor="middle" class="tick">{q}</text>')
    for mode, col in (("dramblast", "var(--a)"), ("maglev", "var(--b)")):
        pts = [(q, old[mode][str(q)]["avg"]) for q in qs if str(q) in old[mode]]
        d = " ".join(f"{'M' if i==0 else 'L'}{x(q):.1f},{y(v):.1f}" for i, (q, v) in enumerate(pts))
        p.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="1.6" '
                 f'stroke-dasharray="4 3" opacity="0.85"/>')
        for q, v in pts:
            p.append(f'<circle cx="{x(q):.1f}" cy="{y(v):.1f}" r="2.6" fill="{col}" opacity="0.85"/>')
        rec = new.get(mode, {})
        pts2 = [(q, rec[str(q)]["steady_mpps"]) for q in qs if str(q) in rec]
        d2 = " ".join(f"{'M' if i==0 else 'L'}{x(q):.1f},{y(v):.1f}" for i, (q, v) in enumerate(pts2))
        p.append(f'<path d="{d2}" fill="none" stroke="{col}" stroke-width="2.4"/>')
        for q, v in pts2:
            p.append(f'<circle cx="{x(q):.1f}" cy="{y(v):.1f}" r="3.4" fill="{col}" '
                     f'stroke="var(--ground)" stroke-width="1.6"/>')
    p.append(f'<text x="{L}" y="{H-6}" class="axis">RX/TX queue pairs</text>')
    p.append(ylabel(16, T, H - B, "Mpps"))
    p.append("</svg>")
    return "".join(p)


def chart_burst(fits, pts_by):
    """Core cycles per packet against 1/burst, with the fitted lines."""
    W, H = 720, 330
    L, R, T, B = 56, 16, 18, 46
    xmax = 0.27
    ymax = 300
    X = lambda v: L + v / xmax * (W - L - R)
    Y = lambda v: T + (1 - min(v, ymax) / ymax) * (H - T - B)
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Core cycles per packet '
         f'against one over burst size">']
    for v in (0, 100, 200, 300):
        p.append(f'<line x1="{L}" y1="{Y(v):.1f}" x2="{W-R}" y2="{Y(v):.1f}" '
                 f'stroke="var(--rule)" stroke-width="1"/>')
        p.append(f'<text x="{L-9}" y="{Y(v)+4:.1f}" text-anchor="end" class="tick">{v}</text>')
    for b, lab in ((64, "64"), (16, "16"), (8, "8"), (4, "4")):
        p.append(f'<text x="{X(1/b):.1f}" y="{H-B+20}" text-anchor="middle" class="tick">{lab}</text>')
    for (mode, arm), (P, C, r2) in fits.items():
        col = "var(--a)" if mode == "dramblast" else "var(--b)"
        dash = '' if arm == "pinned" else ' stroke-dasharray="5 4"'
        p.append(f'<line x1="{X(0):.1f}" y1="{Y(P):.1f}" x2="{X(xmax):.1f}" '
                 f'y2="{Y(P + C*xmax):.1f}" stroke="{col}" stroke-width="1.8"{dash} opacity="0.9"/>')
        for xx, yy, q, b, _ in pts_by[(mode, arm)]:
            p.append(f'<circle cx="{X(xx):.1f}" cy="{Y(yy):.1f}" r="3.4" fill="{col}" '
                     f'stroke="var(--ground)" stroke-width="1.5"/>')
    p.append(f'<text x="{L}" y="{H-6}" class="axis">RX burst size (packets, reciprocal scale)</text>')
    p.append(ylabel(16, T, H - B, "core cycles / packet"))
    p.append("</svg>")
    return "".join(p)


def chart_split(rows):
    """Work against exposed stall, each bar normalised to its own total.

    A shared absolute scale would be misleading here rather than merely ugly:
    the per-burst cost is about seven times the per-packet one, so on one scale
    the two per-packet bars collapse to slivers and the comparison the figure
    exists to make -- what FRACTION of each cost is waiting -- becomes
    unreadable. Each bar is therefore its own 100%, with the absolute cycle
    counts written on it."""
    W = 720
    L, R, T = 168, 92, 26
    bh, gap = 40, 30
    H = T + len(rows) * (bh + gap) + 6
    span = W - L - R
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Share of each cost '
         f'that is CPU work versus exposed memory stall">']
    for i, (label, work, stall) in enumerate(rows):
        total = work + stall
        yy = T + i * (bh + gap)
        wpx = span * work / total
        p.append(f'<text x="{L-14}" y="{yy+bh/2+5:.0f}" text-anchor="end" '
                 f'class="barlab">{esc(label)}</text>')
        p.append(f'<rect x="{L}" y="{yy}" width="{wpx:.1f}" height="{bh}" '
                 f'fill="var(--a)"/>')
        p.append(f'<rect x="{L+wpx:.1f}" y="{yy}" width="{span-wpx:.1f}" '
                 f'height="{bh}" fill="var(--b)"/>')
        # Work label inside its own block; stall label inside if it fits, else
        # outside to the right, so a 14% sliver never gets text written over it.
        p.append(f'<text x="{L+10}" y="{yy+bh/2+5:.0f}" class="inbar">'
                 f'{work:.0f} cyc work</text>')
        spx = span - wpx
        if spx > 96:
            p.append(f'<text x="{L+wpx+10:.1f}" y="{yy+bh/2+5:.0f}" class="inbar">'
                     f'{stall:.0f} cyc stall</text>')
        else:
            p.append(f'<text x="{W-R+8}" y="{yy+bh/2+5:.0f}" class="tick">'
                     f'{stall:.0f} stall</text>')
        p.append(f'<text x="{L+span/2:.0f}" y="{yy+bh+17:.0f}" text-anchor="middle" '
                 f'class="tick">{stall/total*100:.0f}% of {total:.0f} cycles is '
                 f'waiting on memory</text>')
    p.append("</svg>")
    return "".join(p)


PERF_WINDOW = 8.0  # seconds, matches sweep.sh


def per_pkt(rec, key, pkts):
    """A counter per packet, or None if that counter is not in the record.

    NOT `(rec.get(key) or 0) / pkts`, which is what this used to be everywhere.
    An absent counter is not a measured zero, and the difference matters on
    this page: the no-table arm legitimately reads 0.000 L3 misses per packet,
    so a rendered 0 cannot be distinguished from a counter that was never
    recorded. The multiplexing guard makes that more likely rather than less --
    a reading it refuses leaves the key absent, so a rejected counter would come
    back as a confident zero, which is the exact failure the guard exists to
    prevent, one layer further out. A peer session hit the same shape from the
    other side: a correct rejection reported with a false diagnosis.
    """
    v = rec.get(key)
    return None if v is None or not pkts else v / pkts


def tlb_cycles_per_pkt(rec):
    """Cycles this run spent walking page tables, per forwarded packet.

    dtlb_walk_active counts cycles, not walks, so it is directly comparable to
    the per-packet cost without assuming a latency per walk. perf counted it
    across the worker cores for PERF_WINDOW seconds while the port forwarded
    steady_mpps; both are totals over the same window and the same set of
    cores, so the ratio is per-packet without needing the core count.
    """
    w = rec.get("pmu_dtlb_walk_active")
    m = rec.get("steady_mpps")
    if w is None or not m:
        return None
    return w / (m * 1e6 * PERF_WINDOW)


def chart_crossover(rows):
    """P per packet for each mode on each page backing, with the page-walk share.

    rows: [(mode, backing label, P cycles, tlb cycles or None)]
    """
    W = 720
    # L holds row labels up to "dramblast - 4 KiB pages"; R holds the value plus
    # its "(nnn in page walks)" annotation, which is the longest text in the
    # figure. Both were sized for shorter strings and clipped at the edges.
    L, R, T = 186, 176, 24
    bh, gap, grp = 26, 9, 20
    n = len(rows)
    H = T + n * (bh + gap) + grp + 10
    xmax = max(r[2] for r in rows) * 1.18
    X = lambda v: L + v / xmax * (W - L - R)
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Per-packet cost of '
         f'each mode on each page backing">']
    y = T
    prev = None
    for mode, lab, P, tlb in rows:
        if prev is not None and mode != prev:
            y += grp
        prev = mode
        col = "var(--a)" if mode == "dramblast" else "var(--b)"
        p.append(f'<text x="{L-12}" y="{y+bh/2+4:.0f}" text-anchor="end" '
                 f'class="barlab">{esc(mode)} &#183; {esc(lab)}</text>')
        p.append(f'<rect x="{L}" y="{y}" width="{X(P)-L:.1f}" height="{bh}" fill="{col}"/>')
        if tlb and tlb > 0.5:
            p.append(f'<rect x="{L}" y="{y}" width="{X(tlb)-L:.1f}" height="{bh}" '
                     f'fill="var(--ink)" opacity="0.45"/>')
        note = f"{P:.0f}" + (f"   ({tlb:.0f} in page walks)" if tlb and tlb > 0.5 else "")
        p.append(f'<text x="{X(P)+9:.1f}" y="{y+bh/2+4:.0f}" class="tick">{note}</text>')
        y += bh + gap
    p.append("</svg>")
    return "".join(p)


def crossover_rows(allc):
    """[(mode, label, P, tlb)] for whichever crossover conditions exist."""
    spec = [("dramblast", "1 GiB pages", "pinned2_asshipped"),
            ("dramblast", "2 MiB THP", "xover_dram_thp2m"),
            ("dramblast", "4 KiB pages", "xover_dram_4k"),
            ("maglev", "1 GiB pages", "xover_mag_1g"),
            ("maglev", "2 MiB THP", "pinned2_asshipped"),
            ("maglev", "4 KiB pages", "xover_mag_4k")]
    out = []
    for mode, lab, cond in spec:
        d = allc.get(cond, {}).get(mode, {})
        r = d.get("1")
        if not r or not r.get("freq_mhz"):
            continue
        # q=1: one worker, a full 64-packet burst. Deliberately NOT the fitted
        # intercept. On 4 KiB pages the cost rises with queue count at constant
        # burst size -- ten cores each walking a two-million-entry page table
        # put the page table's own working set into cache contention -- so the
        # two-parameter fit acquires a third term it cannot express and its
        # intercept stops meaning "per-packet cost". q=1 carries neither the
        # per-burst term nor the contention term.
        out.append((mode, lab, r["cycles_per_pkt"] * r["freq_mhz"] / TSC_MHZ,
                    tlb_cycles_per_pkt(r)))
    return out


def at_q1(allc, cond, mode):
    """Core cycles per packet at q=1: one worker, a full 64-packet burst."""
    r = allc.get(cond, {}).get(mode, {}).get("1")
    if not r or "cycles_per_pkt" not in r or not r.get("freq_mhz"):
        return None
    return r["cycles_per_pkt"] * r["freq_mhz"] / TSC_MHZ


def alloc_rows(allc):
    """[(pairs, cycles/burst above the hoisted arm, standard error)].

    Matched burst AND matched queue count. Adding allocator pairs slows the
    forwarder, which keeps it oversubscribed further up the sweep and changes
    the burst sizes it reaches, so the arms are only comparable where both sit
    at a full 64-packet burst. At a matched burst the per-packet difference
    times 64 is the per-burst difference, with no model in between.

    The queue count is matched too, and this is not a detail. At a fixed burst
    the cost still varies slightly with queue count, reproducibly, so the
    difference is taken at each queue count and averaged. An earlier version
    read it off q=1 alone, where "Cycle per fwd packet" -- an integer -- makes
    one printed tick worth 64 cycles per burst; the result that came out of that
    was a rounding artefact with a mechanism attached to it.
    """
    hoist = {int(q): (r["cycles_per_pkt"], r.get("rx_batch"), r.get("freq_mhz"))
             for q, r in allc.get("alloc_hoisted", {}).get("dramblast", {}).items()}
    out = [(0, 0.0, 0.0)]
    for pairs, cond in ((1, "pinned2_asshipped"), (3, "alloc_x2"),
                        (5, "alloc_x4"), (9, "alloc_x8")):
        t = {int(q): (r["cycles_per_pkt"], r.get("rx_batch"), r.get("freq_mhz"))
             for q, r in allc.get(cond, {}).get("dramblast", {}).items()}
        qs = [q for q in sorted(set(t) & set(hoist))
              if t[q][1] == 64 and hoist[q][1] == 64]
        if len(qs) < 2:
            continue
        diffs = [t[q][0] - hoist[q][0] for q in qs]
        fq = [t[q][2] / 2100.0 for q in qs if t[q][2]]
        scale = 64.0 * (sum(fq) / len(fq) if fq else 1.0)
        mean = sum(diffs) / len(diffs)
        var = sum((v - mean) ** 2 for v in diffs) / (len(diffs) - 1)
        out.append((pairs, mean * scale, (var / len(diffs)) ** 0.5 * scale))
    return out if len(out) >= 3 else []


def chart_alloc(rows):
    """Per-burst cost above the hoisted arm, against allocator round trips."""
    W, H = 720, 300
    L, R, T, B = 66, 24, 20, 46
    xs = [r[0] for r in rows]
    ys = [r[1] for r in rows]
    es = [r[2] if len(r) > 2 else 0.0 for r in rows]
    xmax, ymax = max(xs) * 1.12 + 0.4, max(ys) * 1.15
    X = lambda v: L + v / xmax * (W - L - R)
    Y = lambda v: T + (1 - v / ymax) * (H - T - B)
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Per-burst cost '
         f'against number of allocator round trips">']
    step = 400 if ymax > 1200 else 200
    v = 0
    while v <= ymax:
        p.append(f'<line x1="{L}" y1="{Y(v):.1f}" x2="{W-R}" y2="{Y(v):.1f}" '
                 f'stroke="var(--rule)" stroke-width="1"/>')
        p.append(f'<text x="{L-9}" y="{Y(v)+4:.1f}" text-anchor="end" class="tick">{v}</text>')
        v += step
    for x in sorted(set(xs)):
        p.append(f'<text x="{X(x):.1f}" y="{H-B+20}" text-anchor="middle" class="tick">{x}</text>')
    # least squares through the points, drawn across the whole range
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    den = n * sxx - sx * sx
    if den:
        b = (n * sxy - sx * sy) / den
        a = (sy - b * sx) / n
        p.append(f'<line x1="{X(0):.1f}" y1="{Y(a):.1f}" x2="{X(xmax):.1f}" '
                 f'y2="{Y(a + b * xmax):.1f}" stroke="var(--a)" stroke-width="1.6" '
                 f'opacity="0.55"/>')
    for x, y, e in zip(xs, ys, es):
        if e:
            p.append(f'<line x1="{X(x):.1f}" y1="{Y(y-e):.1f}" x2="{X(x):.1f}" '
                     f'y2="{Y(y+e):.1f}" stroke="var(--a)" stroke-width="1.6"/>')
        p.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="4.5" fill="var(--a)" '
                 f'stroke="var(--ground)" stroke-width="1.8"/>')
        p.append(f'<text x="{X(x):.1f}" y="{Y(y)-14:.1f}" text-anchor="middle" '
                 f'class="tick">{y:.0f}</text>')
    p.append(f'<text x="{L}" y="{H-6}" class="axis">aligned_alloc / free round trips per burst</text>')
    p.append(ylabel(16, T, H - B, "extra cycles per burst"))
    p.append("</svg>")
    return "".join(p), (a, b) if den else (None, None)



def depth_at64(allc):
    """Depth arms compared at a matched 64-packet burst: {Q: (cyc, insns, sem, n)}.

    Both counters, because the whole point of the depth arm is to separate
    doing more work from waiting longer, and a cycle count alone cannot.
    """
    PERF_WINDOW = 8.0
    out = {}
    for Q, cond in ((8, "depth_8"), (16, "depth_16"), (32, "depth_32"),
                    (64, "pinned2_asshipped")):
        runs = [r for r in allc.get(cond, {}).get("dramblast", {}).values()
                if r.get("rx_batch") == 64 and r.get("insns") and r.get("steady_mpps")]
        if not runs:
            continue
        cyc = sum(r["cycles_per_pkt"] for r in runs) / len(runs)
        ipp = sum(r["insns"] / (r["steady_mpps"] * 1e6 * PERF_WINDOW)
                  for r in runs) / len(runs)
        var = sum((r["cycles_per_pkt"] - cyc) ** 2 for r in runs)
        sem = (var / (len(runs) * (len(runs) - 1))) ** 0.5 if len(runs) > 1 else None
        out[Q] = (cyc, ipp, sem, len(runs))
    return out


def chart_depth(at64):
    """Cycles and instructions per packet against pipeline depth, matched burst."""
    import math
    W, H = 720, 320
    L, R, T, B = 62, 66, 26, 50
    ds = sorted(at64)
    cyc = [at64[d][0] for d in ds]
    ins = [at64[d][1] for d in ds]
    span = 0.26
    clo, chi = min(cyc) * (1 - span / 6), min(cyc) * (1 + span)
    ilo, ihi = min(ins) * (1 - span / 6), min(ins) * (1 + span)
    lo, hi = math.log2(min(ds)), math.log2(max(ds))
    X = lambda d: L + (math.log2(d) - lo) / (hi - lo) * (W - L - R)
    YC = lambda v: T + (1 - (v - clo) / (chi - clo)) * (H - T - B)
    YI = lambda v: T + (1 - (v - ilo) / (ihi - ilo)) * (H - T - B)
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Cycles and '
         f'instructions per packet against prefetch pipeline depth">']
    for i in range(5):
        cv = clo + (chi - clo) * i / 4
        iv = ilo + (ihi - ilo) * i / 4
        y = YC(cv)
        p.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W-R}" y2="{y:.1f}" '
                 f'stroke="var(--rule)" stroke-width="1"/>')
        p.append(f'<text x="{L-9}" y="{y+4:.1f}" text-anchor="end" class="tick" '
                 f'fill="var(--a)">{cv:.0f}</text>')
        p.append(f'<text x="{W-R+9}" y="{y+4:.1f}" class="tick" '
                 f'fill="var(--b)">{iv:.0f}</text>')
    for vals, Yf, col, dash in ((ins, YI, "var(--b)", ' stroke-dasharray="5 4"'),
                                (cyc, YC, "var(--a)", "")):
        pts = " ".join(f"{X(d):.1f},{Yf(v):.1f}" for d, v in zip(ds, vals))
        p.append(f'<polyline points="{pts}" fill="none" stroke="{col}" '
                 f'stroke-width="2.2"{dash}/>')
        for d, v in zip(ds, vals):
            p.append(f'<circle cx="{X(d):.1f}" cy="{Yf(v):.1f}" r="4.5" fill="{col}" '
                     f'stroke="var(--ground)" stroke-width="1.8"/>')
    for d in ds:
        c, i_, sem, _ = at64[d]
        if sem:
            p.append(f'<line x1="{X(d):.1f}" y1="{YC(c-sem):.1f}" x2="{X(d):.1f}" '
                     f'y2="{YC(c+sem):.1f}" stroke="var(--a)" stroke-width="1.6"/>')
        p.append(f'<text x="{X(d):.1f}" y="{YC(c)+20:.1f}" text-anchor="middle" '
                 f'class="tick">IPC {i_/c:.2f}</text>')
        p.append(f'<text x="{X(d):.1f}" y="{H-B+20}" text-anchor="middle" '
                 f'class="tick">{d}</text>')
    p.append(f'<text x="{L}" y="{H-6}" class="axis">prefetch pipeline depth '
             f'(burst held at 64)</text>')
    p.append(f'<text x="{L}" y="{T-8}" class="tick" fill="var(--a)">cycles / packet</text>')
    p.append(f'<text x="{W-R}" y="{T-8}" text-anchor="end" class="tick" '
             f'fill="var(--b)">instructions / packet</text>')
    p.append("</svg>")
    return "".join(p)



def repeat_rows(allc):
    """(mode, [(q, burst-matched?, run1, run2)], burst-64 rms) for the repeat arm."""
    out = []
    for mode in ("dramblast", "maglev"):
        a = allc.get("pinned2_asshipped", {}).get(mode, {})
        b = allc.get("pinned3_repeat", {}).get(mode, {})
        if not a or not b:
            continue
        d64 = [b[q]["cycles_per_pkt"] - a[q]["cycles_per_pkt"]
               for q in set(a) & set(b)
               if a[q].get("rx_batch") == 64 and b[q].get("rx_batch") == 64]
        dsm = [b[q]["cycles_per_pkt"] - a[q]["cycles_per_pkt"]
               for q in set(a) & set(b)
               if a[q].get("rx_batch") == b[q].get("rx_batch") != 64]
        rms = lambda v: (sum(x * x for x in v) / len(v)) ** 0.5 if v else None
        out.append((mode, len(d64), rms(d64), len(dsm), rms(dsm)))
    return out



def depth_pairs(allc):
    """Depth-32 minus depth-64, paired queue count by queue count at burst 64.

    Not arm mean against arm mean: the repeats do not all reach burst 64 at the
    same queue counts, so that would compare different queue sets, and the
    queue count moves the cost a little.
    """
    out = []
    for i in (1, 2, 3):
        def t(cond):
            return {int(q): (r["cycles_per_pkt"], r.get("rx_batch"))
                    for q, r in allc.get(cond, {}).get("dramblast", {}).items()}
        A, B = t(f"depth_32_r{i}"), t(f"depth_64_r{i}")
        qs = [q for q in sorted(set(A) & set(B)) if A[q][1] == 64 == B[q][1]]
        if len(qs) >= 2:
            diffs = [A[q][0] - B[q][0] for q in qs]
            out.append((i, qs, diffs, sum(diffs) / len(diffs)))
    return out

def walk_rows(allc):
    """Page-walk occupancy against queue count, at a matched 64-packet burst.

    Returns {label: {q: (cycles/pkt, walk cycles/pkt, walks/pkt, insns/pkt)}}.
    The counters were recorded alongside every run, so the 4 KiB core-count
    effect can be taken apart without measuring anything new.
    """
    PW = 8.0
    out = {}
    for cond, mode, lab in (("xover_dram_4k", "dramblast", "dram4k"),
                            ("xover_mag_4k", "maglev", "mag4k"),
                            ("pinned2_asshipped", "dramblast", "dram1g")):
        t = {}
        for q, r in allc.get(cond, {}).get(mode, {}).items():
            if r.get("rx_batch") != 64 or not r.get("steady_mpps"):
                continue
            pkts = r["steady_mpps"] * 1e6 * PW
            # A run missing any of these is dropped from the section rather
            # than contributing a zero to it; walk_rows feeds the core-count
            # argument, where a spurious zero would read as "no page walks".
            vals = [per_pkt(r, k, pkts) for k in
                    ("pmu_dtlb_walk_active", "pmu_dtlb_walk_completed", "insns")]
            if any(v is None for v in vals):
                continue
            t[int(q)] = (r["cycles_per_pkt"], *vals)
        if t:
            out[lab] = t
    return out



# --------------------------------------------------------------- source quotes
SRC = pathlib.Path(__file__).resolve().parent


def snip(relpath, start, end=None, nlines=None, note="", before=0):
    """Quote real lines out of the working tree, with their real line numbers.

    Anchors are exact substrings, not line numbers, and a miss is fatal rather
    than silent. The alternative -- pasting code into the generator -- lets the
    page keep asserting something about a function that has since been edited,
    which is the one failure mode a quotation is supposed to prevent. If this
    raises, the code moved and the argument around it needs re-reading, not the
    anchor needs nudging.
    """
    path = SRC / relpath
    src = path.read_text().splitlines()
    hits = [i for i, ln in enumerate(src) if start in ln]
    if len(hits) != 1:
        raise SystemExit(f"snip: {relpath}: {len(hits)} matches for {start!r}, "
                         f"expected exactly 1")
    a = max(0, hits[0] - before)
    if end is not None:
        b = next((j for j in range(hits[0], len(src)) if end in src[j]), None)
        if b is None:
            raise SystemExit(f"snip: {relpath}: no end anchor {end!r} after line {a+1}")
    else:
        b = hits[0] + (nlines or 1) - 1
    width = len(str(b + 1))
    body = "\n".join(f"{i+1:>{width}}  {esc(src[i])}" for i in range(a, b + 1))
    where = f"l2fwd/{relpath}:{a+1}" + (f"&ndash;{b+1}" if b > a else "")
    cap = f"<figcaption><span class=\"mono\">{where}</span>"
    cap += (" &mdash; " + note if note else "") + "</figcaption>"
    return f'<figure class="code"><pre><code>{body}</code></pre>{cap}</figure>'


# ------------------------------------------------------- the three-engine floor
def trio_rows(allc):
    """{mode: {q: (mpps, cycles/pkt, burst, insns/pkt)}} for the trio arm.

    All three modes were swept under one tag, back to back, so this comparison
    does not span the hours that separate the historical arms. `none` is the
    forwarding loop's third branch: same MAC write, no lookup.
    """
    PW = 8.0
    out = {}
    for mode in ("none", "dramblast", "maglev"):
        t = {}
        for q, r in allc.get("engine_trio", {}).get(mode, {}).items():
            if "cycles_per_pkt" not in r or not r.get("steady_mpps"):
                continue
            pkts = r["steady_mpps"] * 1e6 * PW
            t[int(q)] = (r["steady_mpps"], r["cycles_per_pkt"], r.get("rx_batch"),
                         (r.get("insns") or 0) / pkts if pkts else None)
        if t:
            out[mode] = t
    return out


TRIO_COL = {"dramblast": "var(--a)", "maglev": "var(--b)", "none": "var(--ink-2)"}
TRIO_LAB = {"dramblast": "dramblast", "maglev": "maglev", "none": "no hash table"}
LINE_RATE = 93.28


def trio_legend():
    """The figure's key, as page markup rather than as text inside the SVG.

    It lives outside the drawing on purpose: the three curves meet at the
    offered load, so nothing drawn at the end of a line can identify it.
    """
    keys = "".join(
        f'<span class="key"><span class="sw" style="background:{TRIO_COL[m]}">'
        f'</span>{TRIO_LAB[m]}</span>'
        for m in ("dramblast", "maglev", "none"))
    return ('<div class="legend">' + keys
            + '<span class="key">dashed: offered load, 93.28 Mpps</span></div>')


def chart_trio(rows):
    """Two stacked panels: delivered throughput, then per-packet cost."""
    W = 720
    PH = 258                      # panel height
    L, R = 58, 26
    TOP, gapY = 30, 58            # first panel's top edge, and the gap between
    # Height must clear the SECOND panel's tick row, which sits 18px below its
    # plot area, plus the shared x-axis label below that. Sizing it as
    # 2*PH + gap alone put both outside the viewBox and silently clipped them.
    H = TOP + PH * 2 + gapY + 18 + 26
    qs = sorted({q for t in rows.values() for q in t})
    x = lambda q: L + (q - min(qs)) * (W - L - R) / max(1, (max(qs) - min(qs)))
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Throughput and '
         f'per-packet cost of dramblast, maglev and no hash table against '
         f'queue-pair count">']

    def panel(top, ymax, ticks, pick, title, unit):
        Y = lambda v: top + (1 - min(v, ymax) / ymax) * PH
        for v in ticks:
            p.append(f'<line x1="{L}" y1="{Y(v):.1f}" x2="{W-R}" y2="{Y(v):.1f}" '
                     f'stroke="var(--rule)" stroke-width="1"/>')
            p.append(f'<text x="{L-9}" y="{Y(v)+4:.1f}" text-anchor="end" '
                     f'class="tick">{v:g}</text>')
        p.append(f'<text x="{L}" y="{top-10:.0f}" class="axis">{title}</text>')
        for mode in ("maglev", "dramblast", "none"):
            t = rows.get(mode)
            if not t:
                continue
            pts = [(q, pick(t[q])) for q in sorted(t) if pick(t[q]) is not None]
            d = " ".join(f"{'M' if i==0 else 'L'}{x(q):.1f},{Y(v):.1f}"
                         for i, (q, v) in enumerate(pts))
            p.append(f'<path d="{d}" fill="none" stroke="{TRIO_COL[mode]}" '
                     f'stroke-width="2.2"/>')
            for q, v in pts:
                p.append(f'<circle cx="{x(q):.1f}" cy="{Y(v):.1f}" r="3.6" '
                         f'fill="{TRIO_COL[mode]}" stroke="var(--ground)" '
                         f'stroke-width="1.6"/>')
        for q in qs:
            p.append(f'<text x="{x(q):.1f}" y="{top+PH+18:.0f}" '
                     f'text-anchor="middle" class="tick">{q}</text>')
        return Y

    # panel 1: delivered throughput, with the generator's line rate drawn in
    Y1 = panel(TOP, 100, (0, 25, 50, 75, 100), lambda r: r[0],
               "delivered throughput, Mpps", "Mpps")
    yr = TOP + (1 - LINE_RATE / 100) * PH
    p.append(f'<line x1="{L}" y1="{yr:.1f}" x2="{W-R}" y2="{yr:.1f}" '
             f'stroke="var(--ink)" stroke-width="1" stroke-dasharray="3 4" '
             f'opacity="0.55"/>')
    p.append(f'<text x="{W-R-4}" y="{yr-6:.1f}" text-anchor="end" class="tick">'
             f'offered load {LINE_RATE} Mpps</text>')

    # panel 2: cost per packet inside the timed region
    top2 = TOP + PH + gapY
    cmax = max(r[1] for t in rows.values() for r in t.values()) * 1.12
    step = 50 if cmax > 160 else 20
    ticks = [v for v in range(0, int(cmax) + step, step)]
    panel(top2, cmax, ticks, lambda r: r[1], "cycles per forwarded packet", "cycles")
    p.append(f'<text x="{L}" y="{H-6}" class="axis">RX/TX queue pairs</text>')
    p.append("</svg>")
    return "".join(p)


CSS = """
:root{
  --ground:#f5f7f7; --panel:#ffffff; --ink:#10181a; --ink-2:#55635f;
  --rule:#dde4e3; --a:#12707f; --b:#bb551c; --bad:#9b2c2c; --good:#2f6b46;
  --shadow:0 1px 2px rgba(16,24,26,.06);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ground:#0d1416; --panel:#131d20; --ink:#e7eeed; --ink-2:#94a5a2;
    --rule:#24343a; --a:#4bb6c8; --b:#e8894a; --bad:#e08585; --good:#7ec79b;
    --shadow:0 1px 2px rgba(0,0,0,.4);
  }
}
:root[data-theme="dark"]{
  --ground:#0d1416; --panel:#131d20; --ink:#e7eeed; --ink-2:#94a5a2;
  --rule:#24343a; --a:#4bb6c8; --b:#e8894a; --bad:#e08585; --good:#7ec79b;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}
*{box-sizing:border-box}
body{
  background:var(--ground); color:var(--ink);
  font-family:Spectral,Georgia,"Times New Roman",serif;
  font-size:17px; line-height:1.62; margin:0;
  padding-block:0 72px; padding-left:20px; padding-right:20px;
}
.wrap{max-width:760px;margin:0 auto}
.wide{max-width:940px;margin:0 auto}
h1,h2,h3,.eyebrow,.tick,.axis,.barlab,.mono,.stat b,code,th{
  font-family:Archivo,"Helvetica Neue",Arial,sans-serif;
}
h1{font-size:clamp(30px,5.2vw,46px);line-height:1.06;letter-spacing:-.022em;
   font-weight:700;margin:0 0 18px;text-wrap:balance}
h2{font-size:25px;letter-spacing:-.014em;font-weight:700;margin:56px 0 14px;
   text-wrap:balance;padding-top:20px;border-top:1px solid var(--rule)}
h3{font-size:18px;letter-spacing:-.008em;font-weight:600;margin:32px 0 8px}
p{margin:0 0 16px}
/* The page has never styled its own links, so every cross-reference rendered in
   the browser's default blue on a teal-and-rust palette, and turned purple once
   followed. Both are tokens, so they follow the theme. */
a{color:var(--a);text-decoration:underline;text-decoration-thickness:1px;
  text-underline-offset:2px;text-decoration-color:var(--rule)}
a:visited{color:var(--a)}
a:hover{text-decoration-color:var(--a)}
a:focus-visible{outline:2px solid var(--a);outline-offset:2px;border-radius:2px}
/* The arrows on the summary cards are navigation, not prose: no rule under
   them, and a little weight so they read as a target. */
.finding a{text-decoration:none;font-weight:600;padding-left:2px}
.finding a:hover{text-decoration:underline;text-decoration-color:var(--a)}
.eyebrow{font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink-2);font-weight:600;margin:0 0 14px}
.lede{font-size:20px;line-height:1.5;color:var(--ink-2);margin:0 0 34px}
header{padding-block:64px 8px}
.mono,code{font-family:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
  font-size:.86em;font-variant-numeric:tabular-nums}
code{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:1px 5px}
.findings{display:grid;gap:14px;margin:34px 0 10px}
.finding{display:grid;grid-template-columns:auto 1fr;gap:16px;align-items:start;
  padding:16px 18px;background:var(--panel);border:1px solid var(--rule);
  border-radius:4px;box-shadow:var(--shadow)}
.finding .n{font-family:"IBM Plex Mono",monospace;font-size:12px;font-weight:600;
  color:var(--a);padding-top:4px;letter-spacing:.06em}
.finding p{margin:0;font-size:16px;line-height:1.5}
.finding b{font-weight:600}
figure{margin:30px 0 26px}
figure.code{margin:22px 0 24px}
figure.code pre{
  margin:0;background:var(--panel);border:1px solid var(--rule);
  border-left:3px solid var(--a);border-radius:3px;
  padding:14px 16px;overflow-x:auto;
}
figure.code code{
  background:none;border:0;padding:0;display:block;white-space:pre;
  font-family:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
  font-size:12.5px;line-height:1.55;color:var(--ink);
}
figure.code figcaption{margin-top:9px;font-size:13px}
figure.code figcaption code{
  background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:1px 5px;display:inline;white-space:normal;font-size:12px;
}
figure svg{width:100%;height:auto;display:block}
figcaption{font-size:14px;line-height:1.5;color:var(--ink-2);margin-top:12px;
  font-family:Archivo,sans-serif}
.tick{font-size:11px;fill:var(--ink-2)}
.axis{font-size:12px;fill:var(--ink-2);font-weight:600}
.barlab{font-size:13px;fill:var(--ink);font-weight:600}
.inbar{font-size:12.5px;fill:#fff;font-weight:600;font-family:"IBM Plex Mono",monospace}
.legend{display:flex;flex-wrap:wrap;gap:8px 22px;font-family:Archivo,sans-serif;
  font-size:13px;color:var(--ink-2);margin:6px 0 0}
.key{display:inline-flex;align-items:center;gap:7px}
.sw{width:13px;height:13px;border-radius:2px;display:inline-block}
.tablewrap{overflow-x:auto;margin:22px 0}
table{border-collapse:collapse;width:100%;font-size:15px}
th,td{text-align:left;padding:9px 14px 9px 0;border-bottom:1px solid var(--rule);
  vertical-align:baseline}
th{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);
  font-weight:600}
td.num,th.num{text-align:right;font-family:"IBM Plex Mono",monospace;
  font-variant-numeric:tabular-nums;font-size:14px}
blockquote{margin:26px 0;padding:2px 0 2px 22px;border-left:3px solid var(--a);
  font-size:19px;line-height:1.45;color:var(--ink)}
.note{background:var(--panel);border:1px solid var(--rule);border-left:3px solid var(--bad);
  border-radius:3px;padding:16px 18px;margin:26px 0;font-size:15.5px;line-height:1.55}
.note b{font-weight:600}
footer{margin-top:64px;padding-top:22px;border-top:1px solid var(--rule);
  font-size:14px;color:var(--ink-2);font-family:Archivo,sans-serif}
@media (max-width:560px){
  body{font-size:16px}
  .finding{grid-template-columns:1fr;gap:6px}
}
"""


def main():
    old, allc = load()
    P = allc.get("pinned_2100mhz", {})
    T = allc.get("turbo_instr", {})

    fits, pts_by = {}, {}
    for mode in ("dramblast", "maglev"):
        for arm, cond in (("pinned", P), ("turbo", T)):
            pts = series(cond, mode)
            f = lsq(pts)
            if f:
                fits[(mode, arm)] = f
                pts_by[(mode, arm)] = pts

    fp = armfreq(P, "dramblast") or 2094.0
    ft = armfreq(T, "dramblast") or 2993.0

    split = []
    for mode in ("dramblast", "maglev"):
        if (mode, "pinned") in fits and (mode, "turbo") in fits:
            w, t_ns, frac = decompose(fits[(mode, "pinned")][0],
                                      fits[(mode, "turbo")][0], fp, ft)
            split.append((f"{mode} / packet", w, t_ns * fp / 1000.0))
    if ("dramblast", "pinned") in fits and ("dramblast", "turbo") in fits:
        w, t_ns, _ = decompose(fits[("dramblast", "pinned")][1],
                               fits[("dramblast", "turbo")][1], fp, ft)
        split.append(("dramblast / burst", w, t_ns * fp / 1000.0))

    # Standard errors on the two fits of the same coefficient, for the
    # bookkeeping note in section 4. Quoted from the shared estimator so the
    # page and the analysis cannot disagree about them.
    f_old = fit_of(allc, "pinned_2100mhz", "dramblast") or {}
    f_new = fit_of(allc, "pinned2_asshipped", "dramblast") or {}
    se_old, se_new = f_old.get("se") or 0.0, f_new.get("se") or 0.0

    dP, dC, dR = fits[("dramblast", "pinned")]
    mP, mC, mR = fits[("maglev", "pinned")]
    crossover = dC / (mP - dP)

    dwork, dns, dfrac = decompose(dP, fits[("dramblast", "turbo")][0], fp, ft)
    mwork, mns, mfrac = decompose(mP, fits[("maglev", "turbo")][0], fp, ft)

    # The crossover section appears only once its conditions have been measured.
    # An unmeasured section is omitted rather than stubbed: a page that shows a
    # placeholder where a result belongs invites the reader to supply their own.
    # Independent check on the memory share: the PMU counts cycles stalled with
    # an L3 miss outstanding, which is the same quantity the clock arms infer.
    check = []
    N = allc.get("pinned2_asshipped", {})
    for mode in ("dramblast", "maglev"):
        pr, tr = P.get(mode, {}).get("1"), T.get(mode, {}).get("1")
        nr = N.get(mode, {}).get("1")
        if not (pr and tr and nr and nr.get("pmu_stalls_l3_miss")):
            continue
        cp = pr["cycles_per_pkt"] * pr["freq_mhz"] / TSC_MHZ
        ct = tr["cycles_per_pkt"] * tr["freq_mhz"] / TSC_MHZ
        t_ns = (ct - cp) / ((tr["freq_mhz"] - pr["freq_mhz"]) / 1000.0)
        clock_share = t_ns * pr["freq_mhz"] / 1000.0 / cp * 100
        pk = nr["steady_mpps"] * 1e6 * PERF_WINDOW
        pmu_share = nr["pmu_stalls_l3_miss"] / pk / nr["cycles_per_pkt"] * 100
        ipc = nr["insns"] / (nr["freq_mhz"] * 1e6 * PERF_WINDOW)
        check.append((mode, pr["cycles_per_pkt"], ipc, clock_share, pmu_share))

    check_html = ""
    if len(check) == 2:
        rows_html = "".join(
            f"<tr><td>{esc(m)}</td><td class='num'>{tk}</td><td class='num'>{ipc:.2f}</td>"
            f"<td class='num'>{cs:.1f}%</td><td class='num'>{ps:.1f}%</td></tr>"
            for m, tk, ipc, cs, ps in check)
        check_html = f"""
<h3>The same split, arrived at twice</h3>
<p>The split above is inferred from how the cost responds to the clock. The
processor will also tell you directly: it counts the cycles in which nothing
executes because an L3 miss is outstanding. Two methods, no shared
assumptions.</p>
<div class="tablewrap"><table>
<thead><tr><th>engine</th><th class="num">cycles/pkt</th><th class="num">IPC</th>
<th class="num">memory share, two clocks</th>
<th class="num">memory share, stall counter</th></tr></thead>
<tbody>{rows_html}</tbody>
</table></div>
<p>For maglev they agree. That is the strongest corroboration here: the
clock-arm method's entire premise is that a cost which does not scale with core
frequency is memory, and a hardware counter that knows nothing about that
premise says the same thing.</p>
<p>For dramblast they disagree twentyfold — and <em>that</em> is the result. The
stall counter measures cycles thrown away waiting, and dramblast throws away
almost none: it runs at an IPC of 3.03 because the prefetch pipeline has always
queued other work. The clock method measures everything whose duration is fixed
in nanoseconds rather than cycles, which includes waiting <em>and</em> any
throughput limit in the memory hierarchy. So the prefetch pipeline does not
remove dramblast's memory traffic. It converts that traffic from latency into
throughput — and the two measurements diverging is how you tell those two
regimes apart.</p>
"""

    # The allocator section replaces the "not yet established" note once its
    # conditions exist.
    arows = alloc_rows(allc)
    alloc_html = retraction_alloc = ""
    if len(arows) >= 3:
        asvg, (a0, per_pair) = chart_alloc(arows)
        byn = {r[0]: r[1] for r in arows}
        bye = {r[0]: r[2] for r in arows}
        shipped_pair, shipped_se = byn.get(1), bye.get(1)
        multi = sorted((r[0], r[1], r[2]) for r in arows if r[0] >= 3)
        slopes = [(multi[i + 1][1] - multi[i][1]) / (multi[i + 1][0] - multi[i][0])
                  for i in range(len(multi) - 1)]
        # Weighted line through the multi-pair arms, extrapolated to one pair.
        m_slope = m_int = pred1 = z1 = None
        if len(multi) >= 2:
            w = [1.0 / max(e, 1.0) ** 2 for _, _, e in multi]
            sw = sum(w)
            sx = sum(wi * x for wi, (x, _, _) in zip(w, multi))
            sy = sum(wi * y for wi, (_, y, _) in zip(w, multi))
            sxx = sum(wi * x * x for wi, (x, _, _) in zip(w, multi))
            sxy = sum(wi * x * y for wi, (x, y, _) in zip(w, multi))
            den = sw * sxx - sx * sx
            if den:
                m_slope = (sw * sxy - sx * sy) / den
                m_int = (sy - m_slope * sx) / sw
                pred1 = m_int + m_slope
                if shipped_pair and shipped_se:
                    z1 = abs(shipped_pair - pred1) / shipped_se
        # The non-allocator remainder is the hoisted arm's own per-burst cost,
        # measured rather than extrapolated from an intercept.
        hoist_fit = lsq(series({"dramblast": allc.get("alloc_hoisted", {}).get("dramblast", {})},
                               "dramblast"))
        remainder = hoist_fit[1] if hoist_fit else None
        shipped_fit = lsq(series({"dramblast": allc.get("pinned2_asshipped", {}).get("dramblast", {})},
                                 "dramblast"))
        shipped_C = shipped_fit[1] if shipped_fit else None
        # Second estimator: differencing the two fitted per-burst coefficients.
        removal = (shipped_C - remainder) if (shipped_C and remainder) else None
        lo_est = min(x for x in (shipped_pair, removal) if x)
        hi_est = max(x for x in (shipped_pair, removal) if x)
        slope_txt = ", ".join(
            f"{multi[i][0]}&nbsp;&rarr;&nbsp;{multi[i+1][0]} gives {sl:.0f}"
            for i, sl in enumerate(slopes))
        onetick = 64.0 * 2095.0 / 2100.0
        retraction_alloc = f"""<h3>The allocator round trip, quoted to the cycle</h3>
<p>One printed tick is {onetick:.0f} cycles per burst, as
<a href="#rig">section 1</a> sets out. That is not a pedantic caveat; it is the
correction to a result this page carried for six hours. Read at a single queue count, the shipped pair came out
at 447 cycles and an incremental one at 511, the two consecutive slopes agreed
to 0.00 cycles and the line's intercept was 0.0. That was written up as a
structural check — <i>k</i> pairs costing exactly <i>k</i> times one — and then,
because the shipped pair sat 64 cycles below the line, as an allocator load
effect: a lone round trip being cheaper than one of several in flight. A
reviewer improved the framing and it still stood on nothing. The gap was 64
cycles, which is one tick; the perfect slope agreement followed arithmetically
from three integers where one difference was exactly twice another; and 447 came
from the one queue count where the difference happens to be smallest.</p>
<p>Measured at every matched queue count instead, the incremental pair is
{m_slope:.0f} cycles (consecutive slopes: {slope_txt}) and extrapolating that
line to a single pair predicts {pred1:.0f} against {shipped_pair:.0f} measured —
{z1:.1f}σ apart, <b>not resolved</b>. There is no first-pair effect and no load
effect. The failure worth naming is precision claimed past the instrument's
resolution, where the excess precision then generates a mechanism and everything
downstream stays internally consistent while describing rounding.</p>

"""
        alloc_html = f"""
<section class="wrap">
<h3>Most of it is one call to <span class="mono">aligned_alloc</span></h3>
<p>Two candidates survived: the <span class="mono">aligned_alloc</span> /
<span class="mono">free</span> round trip the batched path performs once per
burst, and the batching machinery itself. They can be separated because only one
of them responds to being multiplied. The code now takes a count of allocator
round trips per burst — minus one meaning none at all, with the buffer allocated
once per core at start-up — so the per-burst cost becomes a straight line whose
slope is what a round trip costs <em>on this machine</em>, rather than what the
literature says one costs somewhere else.</p>
{snip("libsashstore/dramblast.c", "if (dramblast_alloc_pairs < 0) {",
      "results = aligned_alloc(64, sizeof(dramblast_result_t) * args_len);",
      note="the whole of the shipped allocation: one call per burst, for a "
           "buffer whose maximum size is known at compile time and whose "
           "elements are 16 bytes wide.")}
</section>

<div class="wide">
<figure>
  {asvg}
  <figcaption>Each point is the difference from the arm with no allocation in
  the burst path, taken at every queue count where both sit at a full 64-packet
  burst and averaged. Bars are the standard error of that mean.</figcaption>
</figure>
</div>

<section class="wrap">
<p>The one round trip the code actually performs costs
<b>{lo_est:.0f}&ndash;{hi_est:.0f} cycles</b> per burst — about
{(lo_est+hi_est)/2/2.1:.0f}&nbsp;nanoseconds. Two estimators that share no
algebra: {shipped_pair:.0f}&nbsp;&plusmn;&nbsp;{shipped_se:.0f} at a matched
burst and queue count, and {removal:.0f} from differencing the two fitted
per-burst coefficients. Against a total per-burst cost of {shipped_C:.0f}
cycles, that single allocation is <b>{100*lo_est/shipped_C:.0f}&ndash;{100*hi_est/shipped_C:.0f}%</b>
of it. What remains when it is removed, the batching machinery itself, is
{remainder:.0f} cycles, measured directly by the leftmost point rather than
extrapolated from the line through the others.</p>
<p>It is quoted as a range, and not more tightly, because the counter is an
integer: one printed tick is {onetick:.0f} cycles per burst. An earlier draft of
this page put the allocator at <em>at most 11%</em>, and a later one quoted the
round trip to the cycle. Both were wrong, in opposite directions, and both
failures are set out in <a href="#corrections">what these numbers are
worth</a>.</p>

<p>This reverses an earlier conclusion in the investigation log, and the way it
was wrong is worth more than the correction. The allocator had been dismissed by
comparing the measured per-burst cost against a published figure of 20-40&nbsp;ns
for a hot allocator round trip. But that figure describes the fast path, and
this call is not on it. The binary links glibc 2.33, where
<span class="mono">aligned_alloc</span> is a nine-byte jump into
<span class="mono">_mid_memalign</span>, which relays to plain
<span class="mono">malloc</span> only when the requested alignment is at most
16 bytes. This call asks for 64. So it takes
<span class="mono">_int_memalign</span> instead: 453 bytes of code that allocates
oversized, computes the aligned address, splits the chunk and frees the leader,
with the arena lock held throughout.</p>
<p>The 64-byte alignment buys nothing — the array holds 16-byte elements written
in order. The earlier finding that the per-burst cost is overwhelmingly executed
instructions rather than waiting should have pointed here immediately; several
hundred instructions of chunk-splitting is exactly what that looks like. It was
read as evidence against the allocator instead of for it, because a constant
taken from a paper had quietly replaced a measurement.</p>
</section>
"""

    # The depth-32 point is the one that tests the ramp model, and one sweep
    # each could not resolve it against the run-to-run floor. These are the
    # repeats that did.
    at64 = depth_at64(allc)
    dprs = depth_pairs(allc)
    pairs_html = ""
    pair_mean = pair_lo_b = pair_hi_b = pair_lo_p = pair_hi_p = None
    ramp_cycles = ramp_pred = None
    if len(dprs) >= 3:
        FQ = 2095.0 / 2100.0
        ms = [m for _, _, _, m in dprs]
        nn = len(ms)
        mu = sum(ms) / nn
        sd = (sum((v - mu) ** 2 for v in ms) / (nn - 1)) ** 0.5
        sem = sd / nn ** 0.5
        t2 = {2: 4.303, 3: 3.182, 4: 2.776}.get(nn - 1, 2.0)
        flat = [v for _, _, ds, _ in dprs for v in ds]
        mu2 = sum(flat) / len(flat)
        sd2 = (sum((v - mu2) ** 2 for v in flat) / (len(flat) - 1)) ** 0.5
        sem2 = sd2 / len(flat) ** 0.5
        tf = 2.16 if len(flat) >= 13 else 2.45
        pair_mean = mu * FQ
        pair_lo_b, pair_hi_b = (mu - t2 * sem) * FQ, (mu + t2 * sem) * FQ
        pair_lo_p, pair_hi_p = (mu2 - tf * sem2) * FQ, (mu2 + tf * sem2) * FQ
        if at64 and 64 in at64 and 8 in at64 and 16 in at64:
            xs = [(1.0 / q - 1.0 / 64.0) for q in (8, 16)]
            ys = [at64[q][0] - at64[64][0] for q in (8, 16)]
            ramp_cycles = sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs)
            ramp_pred = ramp_cycles * (1.0 / 32 - 1.0 / 64)
        rowsh = "".join(
            f'<tr><td>{i}</td><td class="num">{", ".join(map(str, qs))}</td>'
            f'<td class="num">{", ".join(map(str, ds))}</td>'
            f'<td class="num">{m:.2f}</td></tr>' for i, qs, ds, m in dprs)
        edge = "" if (ramp_pred and pair_lo_b <= ramp_pred <= pair_hi_b) else f"""
<p>Its <em>point</em> prediction is another matter. {ramp_pred:.2f} sits inside
the conservative interval and just outside the tighter one, and the measured
excess is {100*(1-pair_mean/ramp_pred):.0f}% below it. The ramp model has the
right sign and roughly the right size here; calling this a clean confirmation
would be overreading it.</p>"""
        pairs_html = f"""
<h3>Resolving the depth-32 point</h3>
<p>That two-cycle step is the one measurement that <em>tests</em> the model of
the pipeline ramp, and against the run-to-run floor a single sweep of each arm
could not separate the model's prediction from no effect at all. So both arms
were run three more times, alternating rather than one block after the other, so
that any drift over the half hour would land on both instead of entirely on the
second. The comparison is paired queue count by queue count, because the repeats
do not all hold a full burst at the same queue counts.</p>
<div class="tablewrap"><table>
<thead><tr><th>repeat</th><th class="num">matched queues</th>
<th class="num">differences, ticks</th><th class="num">mean</th></tr></thead>
<tbody>{rowsh}</tbody>
</table></div>
<p>The excess is <b>{pair_mean:+.2f} cycles per packet</b>, with two error bars
that answer different questions: the spread of the three repeat means gives
[{pair_lo_b:+.2f}, {pair_hi_b:+.2f}], and every one of the {len(flat)} matched
differences gives the more conservative [{pair_lo_p:+.2f}, {pair_hi_p:+.2f}].
<b>Both exclude no-effect</b>, so depth 32 really does cost more than depth 64 at
a matched burst — which is the part the pipeline story needs, and it was
established against a prediction made from the depth-8 and depth-16 arms alone.</p>
{edge}
<p>An earlier version of this page reported this comparison as a 3.1σ
confirmation on the strength of one sweep each, using an error bar taken from
within a single sweep — which cannot see drift between sweeps. Once the repeat
arm measured that drift, the same data said nothing at all.</p>
"""

    # The depth section exists only once the depth arms have run.
    depth_html = ""
    if len(at64) >= 3:
        d_lo, d_hi = min(at64), max(at64)
        c_lo, i_lo = at64[d_lo][0], at64[d_lo][1]
        c_hi, i_hi = at64[d_hi][0], at64[d_hi][1]
        depth_html = f"""
<section class="wrap">
<h3>The rest of it is the prefetch pipeline's ramp</h3>
<p>The remaining candidate for the per-burst cost was the prefetch pipeline's
own fill and drain. Each iteration issues a prefetch and queues an item, then
pops one and processes it, so the queue keeps many cache lines in flight — but a
burst of B packets can only ever fill <span class="mono">min(B, depth)</span>
slots, and a short burst runs a pipeline that never reaches steady state.</p>
{snip("libsashstore/dramblast.c", "// push as many as possible without stalling on LFB.",
      "dramblast_push_queue(ht, idx, arg->k, 0, arg->id, id);",
      note="the fill. <code>find_queue_size</code> is what <code>-Q</code> "
           "sets, so shortening it shortens the ramp and nothing else.")}
<p>At a matched 64-packet burst the four depths separate cleanly.</p>
</section>

<div class="wide">
<figure>
  {chart_depth(at64)}
  <figcaption>Each point averages the five queue counts whose burst stayed at
  64, so every depth is read at the same burst size and nothing is fitted.
  Error bars are the standard error of that mean.</figcaption>
</figure>
</div>

<section class="wrap">
<p>Shortening the pipeline from {d_hi} to {d_lo} raises instructions per packet
by <b>{100*(i_lo-i_hi)/i_hi:.1f}%</b> and cycles per packet by
<b>{100*(c_lo-c_hi)/c_hi:.1f}%</b>. IPC falls from {i_hi/c_hi:.2f} to
{i_lo/c_lo:.2f}. The forwarder is not doing meaningfully more work with a
shallow pipeline; it is waiting. That is the whole claim, and it needs two
counters rather than a model — a cycle count on its own cannot tell those two
apart, which is why the instruction counter has been read alongside it
throughout.</p>
<p>It also prices the design decision. Halving the shipped depth {d_hi} to 32
costs about two cycles per packet — small enough that a single sweep of each
could not resolve it, which is why it was run six times; see below. Going all
the way down to {d_lo} costs {c_lo-c_hi:.1f}. The returns are nearly exhausted
before the shipped depth is reached, so the last doubling buys very
little.</p>
{pairs_html}
<p>Fitting the per-burst model separately to each depth produces something
impossible — at depth&nbsp;8 the per-burst term comes out
<em>below</em> the cost of the single <span class="mono">aligned_alloc</span>
pair that every burst pays. The model is the wrong shape there, not the
measurement: the pipeline fills <span class="mono">ceil(B/Q)</span> times per
burst, so a straight line in <span class="mono">1/B</span> is mis-specified
wherever the burst exceeds the queue depth. At the shipped depth the burst never
does, which is why every earlier result on this page is unaffected.</p>
</section>
"""

    # The repeat arm is the error bar everything else is measured against, so
    # it gets a section rather than a footnote.
    rrows = repeat_rows(allc)
    repeat_html = ""
    if rrows:
        tr = "".join(
            f"<tr><td>{m}</td><td>{n64}</td>"
            f"<td>{('%.2f' % r64) if r64 else '—'}</td>"
            f"<td>{nsm}</td><td>{('%.2f' % rsm) if rsm else '—'}</td></tr>"
            for m, n64, r64, nsm, rsm in rrows)
        all64 = [r for _, n, r, _, _ in rrows if r for _ in range(n)]
        pooled = (sum(r * r for r in all64) / len(all64)) ** 0.5 if all64 else None
        dram64 = next((r for m, _, r, _, _ in rrows if m == "dramblast"), None)
        repeat_html = f"""
<h3>The error bar everything else is measured against</h3>
<p>The shipped condition was run again at the end of the matrix — same binary,
same core assignment, same invocation, hours later with every other experiment
in between. Until that ran, every error bar on this page came from <em>inside</em>
a single sweep: the scatter of ten one-second samples in a twelve-second window,
which by construction cannot see anything that drifts between sweeps.</p>
<div class="tablewrap"><table>
<thead><tr><th>engine</th><th>points at burst 64</th><th>rms</th>
<th>points at smaller bursts</th><th>rms</th></tr></thead>
<tbody>{tr}</tbody>
</table></div>
<p>The floor is not one number. At a 64-packet burst the forwarder is
oversubscribed and the operating point is pinned by the offered load, so the run
reproduces to about {dram64:.2f} cycles per packet for dramblast. At the small
bursts a saturated link produces, the burst size is an <em>outcome</em> rather
than a setting and it wanders between runs, costing several times that. Every
matched-burst comparison on this page is made at burst 64, which is the stable
end.</p>
<p>Folding this in withdrew a verdict. The depth-32 point — the one that tests
the pipeline-ramp model — is an effect of about two cycles per packet, roughly
twice this floor. Quoted against within-sweep scatter alone it had looked like a
3.1σ confirmation; against the measured run-to-run spread the same data said
nothing. It was settled by running both arms three more times, which is the only
thing that could have settled it. The larger results were never close to this
line — the allocator pair is six times the floor and the eightfold pipeline
shortening sixteen times — but every σ quoted before this arm existed was
optimistic by a factor nobody could have known.</p>
"""

    # The open-question note stands only until the allocator sweep answers it.
    note_html = "" if alloc_html else f"""<div class="note">
<b>What this page does not yet establish.</b> The composition of the
{dC:.0f}-cycle per-burst cost is still open. Two candidates remain — the
<span class="mono">aligned_alloc</span>/<span class="mono">free</span> round
trip the batched path performs once per burst, and the batching machinery
itself. Run-time knobs now exist for both, and they cannot mimic each other:
sweeping the number of allocator round trips makes the per-burst cost a straight
line whose slope is what a round trip costs on this machine, while changing the
prefetch pipeline depth moves a pipeline cost and cannot move an allocator one.
</div>"""

    xrows = crossover_rows(allc)
    crossover_html = ""
    if len(xrows) >= 6:
        by = {(m, l): (P, t) for m, l, P, t in xrows}
        d1 = by.get(("dramblast", "1 GiB pages"), (None,))[0]
        d4 = by.get(("dramblast", "4 KiB pages"), (None,))[0]
        m2 = by.get(("maglev", "2 MiB THP"), (None,))[0]
        m1 = by.get(("maglev", "1 GiB pages"), (None,))[0]
        d2 = by.get(("dramblast", "2 MiB THP"), (None,))[0]
        m4 = by.get(("maglev", "4 KiB pages"), (None,))[0]
        gap = m2 - d1 if (m2 and d1) else None
        moved = (m2 - m1) if (m2 and m1) else None
        share = f"{moved/gap*100:.0f}%" if (moved and gap) else "?"
        crossover_html = f"""
<section class="wrap" id="pages">
<h2>3 &middot; Was it ever about page size?</h2>
<p>The two engines do not only differ in how they look a key up. They differ in
how their table is mapped: dramblast takes 8 GiB of 1 GiB hugepages, eight TLB
entries; maglev's ordinary allocation is promoted by transparent hugepages to
2 MiB pages, four thousand and ninety-six of them against a translation buffer
that holds about two thousand. Every comparison between the two was therefore a
comparison of algorithm <em>and</em> address translation at once. So each engine
was run on the other's page size, and on 4 KiB pages, which neither ships
with.</p>
{snip("libsashstore/dramblast.c", "/* As shipped this was an unconditional MAP_HUGETLB",
      "return backing_alloc(bytes,",
      note="dramblast asks for 1 GiB pages by policy: 8 GiB of table is "
           "8 TLB entries.")}
{snip("libsashstore/maglev.c", "/* aligned_alloc(4096, 8 GiB) as shipped",
      "maglev_conntrack.pairs = backing_alloc(size, BACKING_THP2M);",
      note="maglev asks for nothing in particular, and transparent hugepages "
           "give it 2 MiB pages: 4096 entries against a translation buffer "
           "that holds about 2048.")}
<p>The <span class="mono">-B</span> flag exists to break that tie: it overrides
each mode's default so the two can be compared at equal address translation, and
on 4 KiB pages, which neither ships with.</p>
</section>

<div class="wide">
<figure>
  {chart_crossover(xrows)}
  <div class="legend">
    <span class="key"><span class="sw" style="background:var(--a)"></span>dramblast</span>
    <span class="key"><span class="sw" style="background:var(--b)"></span>maglev</span>
    <span class="key"><span class="sw" style="background:var(--ink);opacity:.45"></span>cycles spent walking page tables</span>
  </div>
  <figcaption>Cost at a single queue with a full 64-packet burst, so neither
  the per-burst term nor cross-core page-table contention is in the number.
  Page-walk cycles, shown inside each bar, are counted directly
  (<span class="mono">dtlb_walk_active</span>) rather than inferred from a miss
  rate times an assumed latency — and they are consistently larger than the cost
  the page change actually adds, because a good deal of walking happens
  underneath other work.</figcaption>
</figure>
</div>

<section class="wrap">
<p>Giving maglev dramblast's 1 GiB pages moves its per-packet cost by
{abs(moved):.0f} cycles — {share} of the {abs(gap):.0f}-cycle gap between the two
engines. So the confound was real, and it was worth finding, and it is a fifth
of the story. Address translation is not the explanation.</p>
<p>The sharper result is in how differently the two engines react to the same
change. Dropping from 1 GiB pages to 4 KiB costs dramblast {d4-d1:.0f} cycles
and maglev {m4-m1:.0f} — close to three times as much. The prefetch pipeline is
not only hiding the data access; it is hiding the <em>page walk</em>, which the
prefetch itself triggers, early, so it completes underneath later work. That is
why the gap between the engines widens as pages shrink, from {m1-d1:.0f} cycles
at 1 GiB to {m4-d4:.0f} at 4 KiB. A configuration change that hurts both engines
hurts the unprefetched one three times harder.</p>
<p>And the counters make a trap visible. maglev on 2 MiB pages spends 25.6
cycles per packet with a page walk in flight, but removing the walks entirely
saves only {m2-m1:.0f} — so even the engine with no prefetching at all overlaps
about 45% of its walking under other work. dramblast on 2 MiB pages spends 27%
of every core cycle with a walk outstanding and pays {d2-d1:.0f} cycles for it.
Walk occupancy is not walk cost, and reading it as cost would have overstated
this whole section sixfold.</p>

<h3>The one thing on this page the burst model cannot express</h3>
<p>On 4 KiB pages the cost rises with the <em>number of queues</em> while the
burst stays pinned at 64 — and the model has no term for how many cores are
running. The counters already recorded answer what it is. Across every queue
count, in both engines, the number of page walks per packet is flat at 0.99 and
instructions per packet are flat too. What rises is the time each walk takes:
walk occupancy per packet climbs 19% over six cores for dramblast and 28% over
ten for maglev. The same binary on 1 GiB pages takes no walks at all and shows
no core-count effect whatsoever, which is the control.</p>
<p>So the extra cost is <b>contention for shared page-table structures</b>,
measured in the duration of a walk rather than in how many happen. At a matched
six queues, dramblast's walk occupancy rises 20.3 cycles per packet and 12 of
them reach the cost; maglev's rises 11.8 and none of them do. That is the
opposite direction from the prefetch result earlier on this page, for a reason
that is consistent with it: a pipeline hides latency it issued early, not
latency added underneath it, and an engine already waiting has slack to absorb
more waiting while one retiring at three instructions per cycle does not.</p>
</section>
"""

    # ---------------------------------------------------------------- the floor
    trio = trio_rows(allc)
    trio_html = ""
    if len(trio) == 3:
        nfit = lsq(series(allc.get("engine_trio", {}), "none"))
        n_P, n_C = (nfit[0], nfit[1]) if nfit else (None, None)
        n1 = trio["none"][1]
        d1 = trio["dramblast"][1]
        m1 = trio["maglev"][1]
        # first queue count at which each arm reaches the offered load
        sat = {}
        for mode, t in trio.items():
            hit = [q for q in sorted(t) if t[q][0] >= LINE_RATE - 0.1]
            sat[mode] = hit[0] if hit else None
        # What the timed region excludes, three ways. MHz / Mpps is cycles
        # per packet of a core that is busy-polling at 100%, which every DPDK
        # worker is, so no task-clock correction is needed.
        outs, orows = [], []
        for mode in ("none", "dramblast", "maglev"):
            r = allc["engine_trio"][mode].get("1", {})
            if not (r.get("steady_mpps") and r.get("freq_mhz")):
                continue
            tot = r["freq_mhz"] / r["steady_mpps"]
            outs.append(tot - r["cycles_per_pkt"])
            orows.append(f"<tr><td>{TRIO_LAB[mode]}</td>"
                         f"<td class='num'>{tot:.1f}</td>"
                         f"<td class='num'>{r['cycles_per_pkt']}</td>"
                         f"<td class='num'>{tot - r['cycles_per_pkt']:.1f}</td></tr>")
        outside_rows = "".join(orows)
        outside_mu = sum(outs) / len(outs) if outs else 0.0
        outside_spread = (max(outs) - min(outs)) if outs else 0.0
        # What the lookup actually does to the memory system, at q=1 where
        # all three arms sit at a full burst. Both counters are per packet.
        mrows, miss = [], {}
        for mode in ("none", "dramblast", "maglev"):
            r = allc["engine_trio"][mode].get("1", {})
            if not r.get("steady_mpps"):
                continue
            pk = r["steady_mpps"] * 1e6 * PERF_WINDOW
            ll = per_pkt(r, "pmu_LLC-load-misses", pk)
            st = per_pkt(r, "pmu_stalls_l3_miss", pk)
            miss[mode] = (ll, st, r["cycles_per_pkt"])
            # An em dash where a counter is absent, never a zero: this table is
            # the one place on the page where 0.000 is also a real measurement.
            fmt = lambda v, d: "&mdash;" if v is None else f"{v:.{d}f}"
            mrows.append(f"<tr><td>{TRIO_LAB[mode]}</td>"
                         f"<td class='num'>{fmt(ll, 3)}</td>"
                         f"<td class='num'>{fmt(st, 1)}</td>"
                         f"<td class='num'>{r['cycles_per_pkt']}</td></tr>")
        miss_rows = "".join(mrows)

        # The paragraph under the miss table argues FROM those three numbers,
        # so if any is absent the argument cannot be made. The page then says
        # which counter is missing rather than crashing on a None or quietly
        # asserting a zero. Injecting an absent counter is how this was found:
        # the table itself already declined correctly, and the prose beneath it
        # still died with "unsupported format string passed to NoneType" --
        # a message naming a formatting fault rather than a missing
        # measurement, which is the false-diagnosis shape a peer session named.
        gaps = [f"{TRIO_LAB[m]} ({'L3 misses' if i == 0 else 'stall cycles'})"
                for m, v in miss.items() for i in (0, 1) if v[i] is None]
        if gaps:
            miss_prose = (
                '<div class="note"><b>This paragraph is withheld.</b> It argues '
                'from all three arms&rsquo; miss counters, and these were not '
                'recorded: ' + ", ".join(gaps) + '. An absent counter is not a '
                'measured zero, so the table above shows an em dash and the '
                'argument is not made.</div>')
        else:
            miss_prose = f"""<p>maglev takes {miss["maglev"][0]:.2f} last-level misses per packet and spends
{miss["maglev"][1]:.0f} of its {miss["maglev"][2]} cycles stalled on them: one
DRAM round trip per packet, and half the cost is waiting for it. dramblast
reports {miss["dramblast"][0]:.3f} and {miss["dramblast"][1]:.1f}, which cannot
be a real hit rate &mdash; the same table and the same flows would need to hit
99.3% of the time in a cache a fifth the size of the live set. What differs is
attribution, not traffic, and the code says exactly how.</p>
{snip("libsashstore/dramblast.c", "inline void dramblast_prefetch",
      "LX_PREFETCH(&ht->table[idx], PREFETCH_T1);",
      note="the prefetch is <code>prefetcht1</code>, which fills L2 and not "
           "L1. Note that the comment above it describes "
           "<code>PREFETCH_T0</code>, which is not what the line does.")}
<p>So the line arrives in two steps and <em>neither</em> is a demand load that
misses the last-level cache. The DRAM fill is performed by the prefetch, which
is not a load at all; the L2&nbsp;&rarr;&nbsp;L1 move is performed by the
gather that consumes it, which is a load but hits in L2. The traffic is
identical to maglev's. Only its visibility &mdash; to this counter, and to the
core &mdash; is different. <span class="mono">LLC-load-misses</span> and
<span class="mono">stalls_l3_miss</span> both measure <em>exposure</em> on a
prefetched path, never volume, and nothing on this page should be read as
claiming otherwise.</p>"""

        nf = fit_of(allc, "engine_trio", "none") or {}
        n_Cse, n_r2 = nf.get("se") or 0.0, nf.get("r2") or 0.0
        shipC = f_new.get("C") or dC
        trio_html = f"""
<section class="wrap" id="floor">
<h2>2 &middot; What the table is, and what forwarding costs without it</h2>
<p>Both engines do the same job, and it is worth being exact about what that is.
A packet's flow key is hashed; the hash is looked up in a table of 2<sup>29</sup>
sixteen-byte entries &mdash; 8&nbsp;GiB &mdash; and the value found is the
destination MAC the packet is rewritten with. A miss consults a static backend
table and inserts the result. It is a connection tracker: flow to backend, one
lookup per packet.</p>
<p>The <em>size</em> of that table is the workload. Against the generator's 16.8
million flows it is 3% occupied, so the live set is about 268&nbsp;MB while this
part has 52.5&nbsp;MiB of last-level cache, and the index is a hash, so there is
no locality to exploit. Every packet should therefore be one random DRAM read.
The counters agree, at one queue and a full burst:</p>
<div class="tablewrap"><table>
<thead><tr><th>arm</th><th class="num">L3 load misses / packet</th>
<th class="num">cycles stalled on an L3 miss</th>
<th class="num">cycles / packet</th></tr></thead>
<tbody>{miss_rows}</tbody>
</table></div>
{miss_prose}
<p>So the hash table is not incidental to this experiment; it <em>is</em> the
experiment. Everything below is a consequence of servicing one random DRAM
access per packet at 93 million packets per second: the gap between the engines
is two strategies for the same access, the page-size section exists because the
thing being translated is 8&nbsp;GiB, the per-burst cost exists because issuing
the access early requires batching, and the burst-size crossover is exactly
where the price of batching passes the latency batching hides.</p>
<p>Which leaves the denominator. Every per-packet number on this page is read
out of one timed region, and that region contains more than the lookup. The
forwarding loop already has a third branch for pricing it:
<span class="mono">-m none</span> writes the destination MAC exactly as the
other two do, and skips only the lookup that produced the address.</p>
{snip("main.c", "uint64_t mac = 0xff;",
      "port_statistics[portid][lcore_id].fwded += nb_rx;", before=2,
      note="the forwarding loop's third branch. Same header write as the "
           "other two engines; no table, no key.")}
<p>The timed region is the same in all three cases &mdash; one
<span class="mono">rte_rdtsc</span> before the branch, one after &mdash; so
subtracting this arm is a subtraction of identical code, not of an estimate.</p>
{snip("main.c", "uint64_t start = rte_rdtsc();",
      note="the region every 'cycles per packet' on this page is measured over.")}
</section>

<div class="wide">
<figure>
  {chart_trio(trio)}
  {trio_legend()}
  <figcaption>All three engines swept back to back under one tag, so the
  comparison does not span the hours that separate the other arms. Top: what the
  forwarder actually delivers against a generator offering
  {LINE_RATE}&nbsp;Mpps. Bottom: cycles inside the timed region. Both engines
  buy their way to line rate with cores; the no-table arm is there at two.
  </figcaption>
</figure>
</div>

<section class="wrap">
<p>With no hash table the forwarder reaches the offered load at
<b>two queue pairs</b> and stays there; dramblast needs
{sat["dramblast"]}, maglev {sat["maglev"]}. One worker alone carries
{trio["none"][1][0]:.1f}&nbsp;Mpps of the {LINE_RATE} offered, against
{trio["dramblast"][1][0]:.1f} and {trio["maglev"][1][0]:.1f}. Whatever else is
true of this system, the packet path is not what limits it. The lookup is, and
the rest of this page is about the lookup.</p>
<p>Inside the timed region, at a full 64-packet burst, the floor is
<b>{n1[1]:.0f} cycles per packet</b> against {d1[1]:.0f} for dramblast and
{m1[1]:.0f} for maglev at the same burst and the same queue count. So
{100*(1-n1[1]/d1[1]):.0f}% of dramblast's per-packet cost and
{100*(1-n1[1]/m1[1]):.0f}% of maglev's is the lookup itself. That is the licence
the rest of this page needs: a difference between two engines is a difference
between two lookups, not between two harnesses.</p>

<h3>What the timed region does not contain</h3>
<p>The subtraction also runs the other way, and gives something the timed region
cannot report at all. <span class="mono">rte_eth_rx_burst</span>, the TX buffer
and the driver are all <em>outside</em> the timestamp pair, so no cycles-per-packet
figure on this page includes them. At one queue the single worker busy-polls at
100%, so the cycles it has per packet is simply its delivered clock over its
delivered rate &mdash; and the gap between that and the timed region is the part
of the packet path this instrument never sees.</p>
<div class="tablewrap"><table>
<thead><tr><th>arm</th><th class="num">cycles/packet, total</th>
<th class="num">inside the timed region</th><th class="num">outside it</th></tr></thead>
<tbody>{outside_rows}</tbody>
</table></div>
<p>Three arms whose delivered rates differ sixfold agree on
<b>{outside_mu:.0f}&nbsp;&plusmn;&nbsp;{outside_spread:.0f} cycles per
packet</b> of RX, TX and driver. They did not have to: if the subtraction were
an artefact of the method it would scale with the thing being subtracted, and it
does not. This is also the number that reconciles the two panels above &mdash;
why a forwarder costing five cycles per packet inside the region still needs two
cores to hold line rate.</p>

<h3>The floor is not quite flat</h3>
<p>Fitted the same way as everything else, the no-table arm has a small
per-burst term of its own: <span class="mono">C = {n_C:.0f} &plusmn;
{n_Cse:.0f}</span> cycles per burst, which is the two timestamp reads and the
loop entry, charged to every burst in every arm. Against dramblast's
{shipC:.0f} that is <b>{100*n_C/shipC:.0f}%</b> &mdash; small, but it is the
first thing that would have to be subtracted if the per-burst cost were ever
quoted as an absolute. The fit itself is poor (R&sup2; {n_r2:.2f}), and for a
reason worth stating: this arm is fast enough to run down to two-packet bursts,
where the printed integer is 5 to 24 and rounding is a large fraction of the
value. Treat {n_C:.0f} as a bound, not a measurement. Differences taken between
two arms &mdash; which is every comparison that follows &mdash; are untouched
either way, because the instrument cancels.</p>
</section>
"""

    snip_ticks = snip(
        "main.c", 'printf("\\nFull-loop cyc per fwd packet: %lu"', nlines=2,
        note="an integer quotient of two running totals. At a 64-packet burst "
             "one printed tick is 64 cycles per burst.")

    html = f"""<title>The Burst-Size Crossover</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700&family=IBM+Plex+Mono:wght@400;600&family=Spectral:ital,wght@0,400;0,600;1,400&display=swap">
<style>{CSS}</style>

<header class="wrap">
  <p class="eyebrow">NetBlast &middot; l2fwd on Intel E810-C, 100 GbE</p>
  <h1>Where a forwarded packet's cycles go</h1>
  <p class="lede">A reported throughput collapse turned out to be a harness bug.
  Underneath it is a real and much smaller queue-count effect, and taking that
  effect apart prices every layer between the wire and the answer &mdash; the
  forwarding loop, the page tables, the lookup algorithm, and one line of
  allocation nobody meant to pay for.</p>
</header>

<main>
<section class="wrap">
<p>The argument runs in six steps, each one the precondition for the next.</p>
<div class="findings">
  <div class="finding"><span class="n">01</span><p><b>Fix the instrument.</b>
  <code>run.sh</code> gave N lcores for N queues, but serving N queues needs
  N+1. Above a threshold the run died in
  <code>rte_exit("Not enough cores")</code> and the reported figure was a floor,
  not a measurement. <a href="#rig">&rarr;</a></p></div>

  <div class="finding"><span class="n">02</span><p><b>Price the floor.</b> With
  the lookup removed and nothing else changed, the forwarder holds line rate at
  <b>two</b> queue pairs, where dramblast needs seven and maglev nine, and costs
  {trio["none"][1][1] if len(trio)==3 else 5:.0f} cycles per packet inside the
  timed region. The lookup is 95&ndash;97% of the per-packet cost. The same
  subtraction run backwards prices what the timed region <em>omits</em>: about
  {outside_mu if len(trio)==3 else 29:.0f} cycles per packet of RX, TX and
  driver. <a href="#floor">&rarr;</a></p></div>

  <div class="finding"><span class="n">03</span><p><b>Make the two engines
  comparable.</b> As shipped they differ in address translation as well as
  algorithm: 8 TLB entries against 4096. Swapping each onto the other's page
  size shows translation is about a fifth of the gap, not the explanation.
  <a href="#pages">&rarr;</a></p></div>

  <div class="finding"><span class="n">04</span><p><b>Find what depends on
  queue count.</b> A cost paid once per RX <em>burst</em>, not per packet.
  Fitting <span class="mono">cycles/packet = P + C/B</span> gives dramblast
  <span class="mono">C = {dC:.0f}</span> cycles per burst (R&sup2; {dR:.3f});
  maglev's slope is indistinguishable from zero. So dramblast is the cheaper
  engine only while the burst holds more than {crossover:.1f} packets.
  <a href="#burst">&rarr;</a></p></div>

  <div class="finding"><span class="n">05</span><p><b>Take that cost apart.</b>
  It is executed work, not waiting. Most of it is one call to
  <code>aligned_alloc</code> &mdash;
  <b>{lo_est:.0f}&ndash;{hi_est:.0f} cycles</b>, against an earlier estimate of
  <em>at most 11%</em> taken from a published figure rather than measured. The
  remainder is the prefetch pipeline's ramp, worth about 165 cycles each time it
  fills. <a href="#composition">&rarr;</a></p></div>

  <div class="finding"><span class="n">06</span><p><b>Say what it is worth.</b>
  The counter is an integer, the identical condition run twice differs by
  {abs(next((r for m, _, r, _, _ in repeat_rows(allc) if m == "dramblast"), 0.45)):.2f}
  cycles per packet, and three claims on this page have been retracted by later
  measurement. All three are kept, with the reasoning that produced them.
  <a href="#corrections">&rarr;</a></p></div>
</div>
</section>

<section class="wrap" id="rig">
<h2>1 &middot; The collapse was the instrument</h2>
<p>The committed sweep showed both engines falling roughly seventyfold at
specific queue-pair counts and never recovering. The shape is wrong for a
performance phenomenon &mdash; it is quantised, and it never comes back &mdash;
and the cause is in the core assignment. One lcore is claimed per queue, and the
main lcore is skipped, so serving N queues needs N+1 lcores. Given N, the loop
runs off the end of the enabled set.</p>
</section>

<div class="wide">
<figure>
  {chart_collapse(old, P)}
  <div class="legend">
    <span class="key"><span class="sw" style="background:var(--a)"></span>dramblast</span>
    <span class="key"><span class="sw" style="background:var(--b)"></span>maglev</span>
    <span class="key">dashed: as reported &middot; solid: corrected invocation</span>
  </div>
  <figcaption>That floor is <span class="mono">rte_exit</span>, not throughput.
  Re-run with one lcore per queue plus the main lcore, both engines scale
  linearly until they meet the 93.28&nbsp;Mpps line rate of the
  generator.</figcaption>
</figure>
</div>

<section class="wrap">
<p>The sweep driver in this repository carries the correction, and says so where
it makes it, because the difference between the two invocations is one arithmetic
expression and it silently converts a benchmark into a crash report.</p>
{snip("sweep.sh", "#   run.sh:   MAX_CORE", "#   here:     MAX_CORE",
      note="the whole of the fix, and the reason the original numbers looked "
           "the way they did.")}
<h3>What the instrument can resolve</h3>
<p>The second property of the rig matters just as much and is easier to miss:
cycles per packet are reported as an <b>integer</b>, the quotient of two running
totals.</p>
{snip_ticks}
<p>Every number on this page is a mean of small integers, and nothing is
meaningful below one tick. Counted that way the claims here are many ticks
wide &mdash; the engine gap 64, the page-size effects 19 to 44, the allocator
round trip 8 &mdash; with three exceptions that are marked as approximate where
they appear. Re-running the identical condition lands <em>below</em> one tick,
which is both the right answer and the scale for reading everything else. Two
results on this page were retracted for being quoted past this line;
<a href="#corrections">they are set out at the end</a>.</p>
</section>

{trio_html}

{crossover_html}

<section class="wrap" id="burst">
<h2>4 &middot; What is actually queue-count dependent</h2>
<p>Offered load is held at line rate while the queue count rises, so the same
packet stream is divided over more queues and the average RX burst shrinks &mdash;
from 64 packets down to 4 &mdash; with nothing else about the workload changing.
That makes the sweep an instrument for separating a per-packet cost from a
per-burst one, because only the second depends on burst size.</p>
</section>

<div class="wide">
<figure>
  {chart_burst(fits, pts_by)}
  <div class="legend">
    <span class="key"><span class="sw" style="background:var(--a)"></span>dramblast</span>
    <span class="key"><span class="sw" style="background:var(--b)"></span>maglev</span>
    <span class="key">solid line: 2.100 GHz arm &middot; dashed: 2.993 GHz arm</span>
  </div>
  <figcaption>Each point is one queue-pair count. A straight line here means the
  model holds: the intercept is the per-packet cost, the slope is the cost paid
  once per burst. dramblast climbs; maglev is flat, which is the same statement
  as &ldquo;maglev has no per-burst cost&rdquo;.</figcaption>
</figure>
</div>

<section class="wrap">
<p>The crossover follows from the two fits without any further measurement.
dramblast is cheaper than maglev exactly while</p>
<blockquote>the RX burst holds more than {crossover:.1f} packets.</blockquote>
<p>Above that, dramblast wins by up to 40%. Below it, it loses. That single
inequality is the whole shape of the queue-count dependence &mdash; and it is why
the engine that looks faster in a microbenchmark can be the slower one in a
deployment that spreads traffic across many queues.</p>
<p>One bookkeeping note, because two numbers for the same quantity appear on this
page. The fit above is taken on the original binary, because the clock-arm
decomposition in the next section needs a matched turbo run and only that binary
has one: <span class="mono">C = {dC:.0f} &plusmn; {se_old:.0f}</span> cycles per
burst. Everything that compares one arm against another is read instead against
the control arm of the refactored build, where the same fit gives
<span class="mono">{shipped_C:.0f} &plusmn; {se_new:.0f}</span>. The two differ
by {abs(shipped_C-dC)/((se_old**2+se_new**2)**0.5):.1f}&sigma; of their combined
error, which is to say they are the same measurement; but a difference taken
across the two would not be.</p>
</section>

<section class="wrap" id="composition">
<h2>5 &middot; What the per-burst cost is made of</h2>
<p>A cost measured in core cycles at two different clock speeds separates
computation from memory access, because the two scale differently: instructions
retire in a fixed number of <em>cycles</em>, while a DRAM access takes a fixed
number of <em>nanoseconds</em> and therefore costs more cycles on a faster core.
Running the identical binary at {fp/1000:.3f} GHz and {ft/1000:.3f} GHz gives two
equations and two unknowns.</p>
</section>

<div class="wide">
<figure>
  {chart_split(split)}
  <div class="legend">
    <span class="key"><span class="sw" style="background:var(--a)"></span>CPU work</span>
    <span class="key"><span class="sw" style="background:var(--b)"></span>exposed memory stall</span>
    <span class="key">cycles at 2.100 GHz</span>
  </div>
  <figcaption>The per-packet bars are nearly the same length in their work
  component and very different in their stall component. The per-burst bar is
  the surprise: the price dramblast pays for hiding latency is overwhelmingly
  executed instructions, not waiting.</figcaption>
</figure>
</div>

<section class="wrap">
<p>That last row is what narrows the search. If the per-burst cost were the
prefetch pipeline failing to fill on a short burst, it would show up as
<em>stall</em>. It does not: it is
{100*(1-decompose(dC, fits[("dramblast","turbo")][1], fp, ft)[1]*fp/1000.0/dC):.0f}%
executed work. Whatever dramblast is doing once per burst, it is doing it, not
waiting for it &mdash; so the thing to look for is several hundred instructions,
somewhere on the per-burst path.</p>
{check_html}
{note_html}
</section>

{alloc_html}
{depth_html}

<section class="wrap" id="corrections">
<h2>6 &middot; What these numbers are worth</h2>
<p>Three results on this page were retracted by later measurement, and a fourth
was withdrawn and then re-established. Two of the four are recorded where the
number they changed appears &mdash; the allocator's <em>at most 11%</em> in
section 5, and the depth-32 point's 3.1&sigma; just above. The other two are
here, because they are properties of the rig rather than of either engine. In
every case the mistake had the same shape: precision claimed past what the
instrument can resolve, which then grew a mechanism to explain itself and stayed
internally consistent while describing rounding.</p>
{repeat_html}
{retraction_alloc}
</section>

<section class="wrap">
<h2>The machine this was measured on</h2>
<p>Two properties of the host turned out to matter more than expected, and both
are recorded as conditions of the experiment rather than corrected away.</p>
<p>Idle states are disabled on all 56 cores, so every core spins unhalted and the
package never goes quiet. The all-core turbo ceiling is therefore pinned near
2.99&nbsp;GHz regardless of load &mdash; measured at {ft:.0f} MHz on every one of
ten runs spanning one to ten busy cores, with 1 MHz of spread. Nothing here
should be described as running at the 3.7&nbsp;GHz nominal.</p>
<p>And the two engines ship on different page sizes, which is the confound
<a href="#pages">section 3</a> exists to remove. It was found by looking for it,
not by it causing trouble &mdash; which is the only reason it did not quietly
become the result.</p>
</section>

<section class="wrap">
<footer>
Measured on a single-socket Intel Xeon Gold 5512U with an E810-C 100 GbE NIC,
DPDK 21.11, against a hardware generator holding 93.28 Mpps of 110-byte frames
across 16.8M flows. Every figure and every quoted source line on this page is
generated from the working tree by
<span class="mono">l2fwd/make_report.py</span>; the full reasoning, including
the mistakes, is in <span class="mono">docs/INVESTIGATION.md</span>.
</footer>
</section>
</main>
"""
    out = DOCS / "report.html"
    out.write_text(html)
    print(f"wrote {out}  ({len(html)} bytes)")


if __name__ == "__main__":
    main()
