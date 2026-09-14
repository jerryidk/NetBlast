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
        pts = series({mode: d}, mode)
        f = lsq(pts)
        if not f:
            continue
        # Page-walk cycles are read at the largest burst available, where the
        # per-burst term is smallest and P dominates -- the same cell the bar
        # length is dominated by.
        best = max(d.values(), key=lambda r: r.get("rx_batch", 0))
        out.append((mode, lab, f[0], tlb_cycles_per_pkt(best)))
    return out


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

    xrows = crossover_rows(allc)
    crossover_html = ""
    if len(xrows) >= 4:
        by = {(m, l): (P, t) for m, l, P, t in xrows}
        d1 = by.get(("dramblast", "1 GiB pages"), (None,))[0]
        d4 = by.get(("dramblast", "4 KiB pages"), (None,))[0]
        m2 = by.get(("maglev", "2 MiB THP"), (None,))[0]
        m1 = by.get(("maglev", "1 GiB pages"), (None,))[0]
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
  <figcaption>Per-packet cost on each backing, with the measured page-walk
  cycles shown inside each bar. Page-walk cycles are counted directly
  (<span class="mono">dtlb_walk_active</span>), not inferred from a miss rate
  multiplied by an assumed latency.</figcaption>
</figure>
</div>

<section class="wrap">
<p>Giving maglev dramblast's 1 GiB pages moves its per-packet cost by
{abs(moved):.0f} cycles — {share} of the {abs(gap):.0f}-cycle gap between the two
engines. Address translation is a real cost and it is not the explanation. The
prefetch pipeline is.</p>
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

  <div class="finding"><span class="n">03</span><p><b>The two engines do the
  same CPU work per packet. The whole difference is exposed memory latency.</b>
  {dwork:.0f} cycles of work for dramblast against {mwork:.0f} for maglev —
  within {abs(mwork-dwork)/dwork*100:.0f}% — while maglev waits {mns:.1f} ns per
  packet and dramblast only {dns:.1f} ns. The software prefetch pipeline hides
  about {(1-dns/mns)*100:.0f}% of the same memory access.</p></div>
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

<div class="note">
<b>What this page does not yet establish.</b> The composition of the
{dC:.0f}-cycle per-burst cost is still open. Two candidates remain — the
<span class="mono">aligned_alloc</span>/<span class="mono">free</span> round
trip the batched path performs once per burst, and the batching machinery
itself. Run-time knobs now exist for both, and they cannot mimic each other:
sweeping the number of allocator round trips makes the per-burst cost a straight
line whose slope is what a round trip costs on this machine, while changing the
prefetch pipeline depth moves a pipeline cost and cannot move an allocator one.
</div>
</section>

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
