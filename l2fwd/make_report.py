"""Generate docs/report.html — the findings page — from the measured results.

Everything on the page is read from docs/results_reproduced.json and
docs/results.json at build time, so the report cannot drift from the data. If a
condition has not been measured, its section is omitted rather than stubbed.

Run:  nix develop .. -c python3 make_report.py
"""

import json
import pathlib

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
    p.append(f'<text x="14" y="{T+4}" class="axis" transform="rotate(-90 14 {T+4})">Mpps</text>')
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
    p.append(f'<text x="14" y="{T+4}" class="axis" transform="rotate(-90 14 {T+4})">core cycles / packet</text>')
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
    L, R, T = 150, 74, 24
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
    """[(absolute pairs, per-burst cycles above the hoisted arm)].

    Read at a matched 64-packet burst rather than from each arm's own fit.
    Adding allocator pairs slows the forwarder, which keeps it oversubscribed
    further up the sweep, which changes the burst sizes it reaches -- so the
    arms are only comparable at q=1, where all of them have a full burst. There
    the per-packet difference times 64 is the per-burst difference, with no
    model in between.
    """
    spec = [(0, "alloc_hoisted"), (1, "pinned2_asshipped"), (3, "alloc_x2"),
            (5, "alloc_x4"), (9, "alloc_x8")]
    base, out = None, []
    for n, cond in spec:
        v = at_q1(allc, cond, "dramblast")
        if v is None:
            continue
        if base is None:
            base = v
        out.append((n, (v - base) * 64.0))
    return out


def chart_alloc(rows):
    """Per-burst cost above the hoisted arm, against allocator round trips."""
    W, H = 720, 300
    L, R, T, B = 66, 24, 20, 46
    xs = [n for n, _ in rows]
    ys = [c for _, c in rows]
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
    for x, y in zip(xs, ys):
        p.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="4.5" fill="var(--a)" '
                 f'stroke="var(--ground)" stroke-width="1.8"/>')
        p.append(f'<text x="{X(x):.1f}" y="{Y(y)-12:.1f}" text-anchor="middle" '
                 f'class="tick">{y:.0f}</text>')
    p.append(f'<text x="{L}" y="{H-6}" class="axis">aligned_alloc / free round trips per burst</text>')
    p.append(f'<text x="14" y="{T+4}" class="axis" transform="rotate(-90 14 {T+4})">extra cycles per burst</text>')
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
<section class="wrap">
<h2>The same number, arrived at twice</h2>
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
</section>
"""

    # The allocator section replaces the "not yet established" note once its
    # conditions exist.
    arows = alloc_rows(allc)
    alloc_html = ""
    if len(arows) >= 3:
        asvg, (a0, per_pair) = chart_alloc(arows)
        byn = dict(arows)                       # absolute pairs -> extra cycles/burst
        shipped_pair = byn.get(1)               # cost of the one pair the code performs
        # Consecutive slopes between the multi-pair arms. Each uses a disjoint
        # pair of measurements, unlike total/pairs, which is an average and so
        # converges on the slope whatever the low-count behaviour is.
        multi = sorted((n, c) for n, c in arows if n > 1)
        slopes = [(multi[i + 1][1] - multi[i][1]) / (multi[i + 1][0] - multi[i][0])
                  for i in range(len(multi) - 1)]
        incr_lo = min(slopes) if slopes else None
        incr_hi = max(slopes) if slopes else None
        # Line through the multi-pair arms; its intercept is the structural test.
        if len(multi) >= 2:
            n_ = len(multi)
            sx = sum(n for n, _ in multi); sy = sum(c for _, c in multi)
            sxx = sum(n * n for n, _ in multi); sxy = sum(n * c for n, c in multi)
            m_slope = (n_ * sxy - sx * sy) / (n_ * sxx - sx * sx)
            m_int = (sy - m_slope * sx) / n_
        else:
            m_slope = m_int = None
        # The position model: first pair cheap, the rest at the asymptotic slope.
        pos_miss = [(n, c - (shipped_pair + (n - 1) * m_slope)) for n, c in multi] \
            if (m_slope is not None and shipped_pair is not None) else []
        miss_rows = "".join(
            f"<tr><td>{n}</td><td>{shipped_pair + (n - 1) * m_slope:.0f}</td>"
            f"<td>{c:.0f}</td><td>{d:+.0f}</td></tr>"
            for (n, c), (_, d) in zip(multi, pos_miss))
        # The non-allocator remainder is the hoisted arm's own per-burst cost,
        # measured rather than extrapolated from an intercept.
        hoist_fit = lsq(series({"dramblast": allc.get("alloc_hoisted", {}).get("dramblast", {})},
                               "dramblast"))
        remainder = hoist_fit[1] if hoist_fit else None
        shipped_fit = lsq(series({"dramblast": allc.get("pinned2_asshipped", {}).get("dramblast", {})},
                                 "dramblast"))
        shipped_C = shipped_fit[1] if shipped_fit else None
        slope_txt = ", ".join(
            f"{multi[i][0]}&nbsp;&rarr;&nbsp;{multi[i+1][0]} pairs gives "
            f"{sl:.2f} cycles each" for i, sl in enumerate(slopes))
        gap = m_slope - shipped_pair
        gappct = 100.0 * gap / m_slope
        overpct = 100.0 * gap / shipped_pair
        alloc_html = f"""
<section class="wrap">
<h2>What the per-burst cost is made of</h2>
<p>Two candidates survived: the <span class="mono">aligned_alloc</span> /
<span class="mono">free</span> round trip the batched path performs once per
burst, and the batching machinery itself. They can be separated because only one
of them responds to being multiplied. The code now takes a count of allocator
round trips per burst — minus one meaning none at all, with the buffer allocated
once per core at start-up — so the per-burst cost becomes a straight line whose
slope is what a round trip costs <em>on this machine</em>, rather than what the
literature says one costs somewhere else.</p>
</section>

<div class="wide">
<figure>
  {asvg}
  <figcaption>Each point is a full ten-run queue sweep refitted. The leftmost
  point has no allocation in the burst path at all.</figcaption>
</figure>
</div>

<section class="wrap">
<p>The one round trip the code actually performs is worth
<b>{shipped_pair:.0f} cycles</b> per burst — about 215&nbsp;nanoseconds.
Against a total per-burst cost of {shipped_C:.0f} cycles, that single allocation
is roughly <b>60%</b> of it. What remains when it is removed, the batching
machinery itself, is {remainder:.0f} cycles, measured directly by the leftmost
point rather than extrapolated from the line through the others.</p>
<p>An earlier draft of this investigation put the allocator at <em>at most
11%</em>. That was wrong, and how it was wrong is the more useful finding.</p>

<h3>Reading the amplified arms honestly</h3>
<p>The arms with several pairs are very well behaved, but the tempting way to
say so — total cost divided by number of pairs, three numbers agreeing to the
cycle — does not survive scrutiny. That ratio is an average, so it converges on
the asymptotic slope as the count grows no matter how the first pair behaves;
three such numbers are not three independent confirmations of anything. The
<em>consecutive</em> slopes are independent, because each uses a disjoint pair of
measurements: {slope_txt}. The line through those arms is
<span class="mono">{m_int:+.1f} + {m_slope:.2f} × pairs</span>, and the zero
intercept is the real check — <i>k</i> pairs cost exactly <i>k</i> times one pair
with nothing left over, which a fixed setup overhead would violate.</p>
<p>Extrapolated down to a single pair that line predicts
{m_slope:.0f} cycles, against {shipped_pair:.0f} measured: the lone pair is
<b>{gap:.0f} cycles ({gappct:.0f}%) cheaper</b>. Two explanations for that were
wrong. The first said the shipped pair must cost <em>more</em>, its
<span class="mono">free</span> separated from its <span class="mono">alloc</span>
by a whole batch while the amplification pairs run back to back — that had the
sign backwards. The second said the first pair is intrinsically cheap and later
ones cost {m_slope:.0f}. The data rule that out: if the discount belonged to
pair&nbsp;#1 it would persist into every arm that contains one, and instead the
model misses each of them by the same constant.</p>
<div class="tablewrap"><table>
<thead><tr><th>pairs</th><th>cheap-first model</th><th>measured</th><th>miss</th></tr></thead>
<tbody>{miss_rows}</tbody>
</table></div>
<p>So in an arm with three or more pairs, <em>every</em> pair costs
{m_slope:.0f} — including the first. What survives is a statement about how
many, not which one: <b>a lone round trip costs {shipped_pair:.0f} cycles; with
three or more in flight each costs {m_slope:.0f}.</b> That is an allocator load
effect — more live chunks, more splitting, a larger free-list working set — and
its mechanism is not established here. It also settles which number describes
the shipped code, which performs exactly one: {shipped_pair:.0f}. Calibrating
from the amplification slope instead would overstate it by
{overpct:.0f}%.</p>
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

    # The depth section exists only once the depth arms have run.
    at64 = depth_at64(allc)
    depth_html = ""
    if len(at64) >= 3:
        d_lo, d_hi = min(at64), max(at64)
        c_lo, i_lo = at64[d_lo][0], at64[d_lo][1]
        c_hi, i_hi = at64[d_hi][0], at64[d_hi][1]
        depth_html = f"""
<section class="wrap">
<h2>What the prefetch pipeline actually buys</h2>
<p>The remaining candidate for the per-burst cost was the prefetch pipeline's
own fill and drain. It can be shortened — <span class="mono">-Q</span> sets the
find queue's depth — and at a matched 64-packet burst the four depths separate
cleanly.</p>
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
costs {at64[32][0]-c_hi:.1f} cycles per packet; going all the way down to
{d_lo} costs {c_lo-c_hi:.1f}. The returns are nearly exhausted before the
shipped depth is reached, so the last doubling buys very little.</p>
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
<section class="wrap">
<h2>Was it ever about page size?</h2>
<p>The two engines do not only differ in how they look a key up. They differ in
how their table is mapped: dramblast takes 8 GiB of 1 GiB hugepages, eight TLB
entries; maglev's ordinary allocation is promoted by transparent hugepages to
2 MiB pages, four thousand and ninety-six of them against a translation buffer
that holds about two thousand. Every comparison between the two was therefore a
comparison of algorithm <em>and</em> address translation at once. So each engine
was run on the other's page size, and on 4 KiB pages, which neither ships
with.</p>
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
</section>
"""

    html = f"""<title>The Burst-Size Crossover</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700&family=IBM+Plex+Mono:wght@400;600&family=Spectral:ital,wght@0,400;0,600;1,400&display=swap">
<style>{CSS}</style>

<header class="wrap">
  <p class="eyebrow">NetBlast &middot; l2fwd on Intel E810-C, 100 GbE</p>
  <h1>Why the forwarder looked like it fell off a cliff</h1>
  <p class="lede">The reported collapse at specific queue-pair counts was a
  harness bug. Underneath it there is a real, much smaller queue-count effect —
  and measuring it properly turns out to say something precise about what the
  two lookup engines are actually trading against each other.</p>
</header>

<main>
<section class="wrap">
<div class="findings">
  <div class="finding"><span class="n">01</span><p><b>The dips are not a
  performance phenomenon.</b> <code>run.sh</code> gave N lcores for N queues,
  but serving N queues needs N+1 — one worker each plus the main lcore. Above a
  threshold the run died in <code>rte_exit("Not enough cores")</code> and the
  reported figure was a floor, not a measurement. With the corrected invocation
  both engines scale cleanly to line rate.</p></div>

  <div class="finding"><span class="n">02</span><p><b>The real effect is a cost
  paid once per RX burst, not per packet.</b> Fitting
  <span class="mono">cycles/packet = P + C/B</span> across the sweep gives
  dramblast <span class="mono">C = {dC:.0f}</span> cycles per burst
  (R&sup2; {dR:.3f}); maglev's slope is indistinguishable from zero. As the
  queue count rises the same packet stream is split over more queues, bursts
  shrink, and dramblast's fixed cost is amortised over fewer packets.</p></div>

  <div class="finding"><span class="n">03</span><p><b>Both engines spend nearly
  the same number of cycles computing. The whole difference is time spent
  waiting.</b> {dwork:.0f} cycles of computation for dramblast against
  {mwork:.0f} for maglev — within {abs(mwork-dwork)/dwork*100:.0f}% — while
  maglev waits {mns:.1f} ns per packet and dramblast only {dns:.1f} ns. The
  software prefetch pipeline hides about {(1-dns/mns)*100:.0f}% of the same
  memory access. Note that equal <em>cycles</em> is not equal work: dramblast
  retires about 40% more instructions per packet, at more than twice the
  instructions-per-cycle. Doing more, faster, to wait less is the
  trade.</p></div>
</div>
</section>

<div class="wide">
<figure>
  {chart_collapse(old, P)}
  <div class="legend">
    <span class="key"><span class="sw" style="background:var(--a)"></span>dramblast</span>
    <span class="key"><span class="sw" style="background:var(--b)"></span>maglev</span>
    <span class="key">dashed: as reported &middot; solid: corrected invocation</span>
  </div>
  <figcaption>The committed data falls roughly seventyfold to a quantised floor
  and never recovers. That floor is <span class="mono">rte_exit</span>, not
  throughput. Re-run with one lcore per queue plus the main lcore, both engines
  scale linearly until they meet the 93.28&nbsp;Mpps line rate of the
  generator.</figcaption>
</figure>
</div>

<section class="wrap">
<h2>What is actually queue-count dependent</h2>
<p>Offered load is held at line rate while the queue count rises, so the same
packet stream is divided over more queues and the average RX burst shrinks —
from 64 packets down to 4 — with nothing else about the workload changing. That
makes the sweep an instrument for separating a per-packet cost from a per-burst
one, because only the second depends on burst size.</p>
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
inequality is the whole shape of the queue-count dependence — and it is why the
engine that looks faster in a microbenchmark can be the slower one in a
deployment that spreads traffic across many queues.</p>

<h2>Splitting the cost into work and waiting</h2>
<p>A cost measured in core cycles at two different clock speeds separates
computation from memory access, because the two scale differently: instructions
retire in a fixed number of <em>cycles</em>, while a DRAM access takes a fixed
number of <em>nanoseconds</em> and therefore costs more cycles on a faster core.
Running the identical binary at {fp/1000:.3f} GHz and {ft/1000:.3f} GHz gives
two equations and two unknowns.</p>
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
<p>That last row matters because it kills the obvious explanation. If the
per-burst cost were the prefetch pipeline failing to fill on a short burst, it
would show up as <em>stall</em>. It does not: it is
{100*(1-decompose(dC, fits[("dramblast","turbo")][1], fp, ft)[1]*fp/1000.0/dC):.0f}%
executed work. Whatever dramblast is doing once per burst, it is doing it, not
waiting for it.</p>

{note_html}
</section>

{alloc_html}
{depth_html}
{check_html}
{crossover_html}
<section class="wrap">
<h2>How much of this is the measurement rig</h2>
<p>Two properties of the machine turned out to matter more than expected, and
both are recorded as conditions of the experiment rather than corrected away.</p>
<p>Idle states are disabled on all 56 cores, so every core spins unhalted and the
package never goes quiet. The all-core turbo ceiling is therefore pinned near
2.99&nbsp;GHz regardless of load — measured at {ft:.0f} MHz on every one of ten
runs spanning one to ten busy cores, with 1 MHz of spread. Nothing here should
be described as running at the 3.7&nbsp;GHz nominal.</p>
<p>The two engines also ship on different page sizes: dramblast maps its 8 GiB
table on 1 GiB pages, while maglev's plain
<span class="mono">aligned_alloc</span> is silently promoted by transparent
hugepages to 2 MiB pages. That is 8 TLB entries against 4096. Every comparison
between them was therefore a comparison of algorithm <em>and</em> address
translation at once — a confound found by looking for one, not by it causing
trouble.</p>
</section>

<section class="wrap">
<footer>
Measured on a single-socket Intel Xeon Gold 5512U with an E810-C 100 GbE NIC,
DPDK 21.11, against a hardware generator holding 93.28 Mpps of 110-byte frames
across 16.8M flows. Every figure on this page is generated from the measured
data by <span class="mono">l2fwd/make_report.py</span>; the full reasoning,
including the mistakes, is in <span class="mono">docs/INVESTIGATION.md</span>.
</footer>
</section>
</main>
"""
    out = DOCS / "report.html"
    out.write_text(html)
    print(f"wrote {out}  ({len(html)} bytes)")


if __name__ == "__main__":
    main()
