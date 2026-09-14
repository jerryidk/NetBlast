#!/usr/bin/env python3
"""Three-panel summary figure for the experiment matrix, as dependency-free SVG.

Why SVG by hand: this runs with nothing but the standard library, so it works
from a plain shell. The project's toolchain -- meson, ninja, matplotlib -- lives
in the nix dev shell (`nix develop`), not on the system Python, and running
outside it makes them all look uninstalled. That misread cost time here and
produced a commit message claiming they had been removed from the node; they had
not. The lesson is cheap to state and was not: on this repo, check whether you
are inside the dev shell before concluding a tool is missing.

The script is kept because the report draws all of its charts as hand-written
SVG anyway, an SVG scales better in the page than a rasterised PNG, and a figure
that needs no environment at all is one less thing to be wrong about.
analyse_matrix.py --plot uses matplotlib when it is importable and falls back
here when it is not.

    python3 l2fwd/plot_matrix.py          -> docs/matrix.svg
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fit_burst_model import DOCS, lsq, points          # noqa: E402
from analyse_matrix import at_q1                        # noqa: E402

# Same palette as the report, so the figure and the page read as one thing.
SURFACE, INK, INK_2, GRID = "#fbfaf7", "#1a1a1a", "#5a5a5a", "#e0ddd6"
BLUE, ORANGE = "#12707f", "#bb551c"
PERF_WINDOW = 8.0

PW, PH = 500, 430          # one panel
PAD = 26


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def frame(x0, title, sub=""):
    """Panel chrome: title, subtitle, plot rectangle. Returns the plot box."""
    L, R, T, B = x0 + 76, x0 + PW - 46, 74, PH - 62
    out = [f'<text x="{x0 + 14}" y="30" class="ttl">{esc(title)}</text>']
    if sub:
        out.append(f'<text x="{x0 + 14}" y="50" class="sub">{esc(sub)}</text>')
    return out, (L, R, T, B)


def ygrid(g, L, R, T, B, lo, hi, fmt="{:.0f}", ticks=5, colour=INK_2, side="left"):
    """Horizontal rules with labels, and the value->y mapping that goes with them."""
    def y(v):
        return B - (v - lo) / (hi - lo) * (B - T)
    for i in range(ticks):
        v = lo + (hi - lo) * i / (ticks - 1)
        yy = y(v)
        if side == "left":
            g.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{R}" y2="{yy:.1f}" '
                     f'class="grid"/>')
            g.append(f'<text x="{L - 10}" y="{yy + 4:.1f}" class="tick" '
                     f'text-anchor="end" fill="{colour}">{fmt.format(v)}</text>')
        else:
            g.append(f'<text x="{R + 10}" y="{yy + 4:.1f}" class="tick" '
                     f'text-anchor="start" fill="{colour}">{fmt.format(v)}</text>')
    return y


def panel_backing(x0, allc, base_cond):
    """Per-packet cost of each mode on each page size, at q=1 and burst 64.

    q=1 rather than a fitted intercept: on 4 KiB pages the fit absorbs a
    core-count term it cannot represent, and maglev on 4 KiB never saturates the
    link so it has no fit at all. q=1 exists in every arm and means the same
    thing in each.
    """
    g, (L, R, T, B) = frame(x0, "1. Page backing moves the per-packet cost",
                            "core cycles per packet, one queue, 64-packet burst")
    bars = []
    for lab, mode, cond, col in (("dram\n1 GiB", "dramblast", base_cond, BLUE),
                                 ("dram\n2 MiB", "dramblast", "xover_dram_thp2m", BLUE),
                                 ("dram\n4 KiB", "dramblast", "xover_dram_4k", BLUE),
                                 ("mag\n1 GiB", "maglev", "xover_mag_1g", ORANGE),
                                 ("mag\n2 MiB", "maglev", base_cond, ORANGE),
                                 ("mag\n4 KiB", "maglev", "xover_mag_4k", ORANGE)):
        v = at_q1(allc, cond, mode)
        if v:
            bars.append((lab, v, col))
    if not bars:
        return g
    hi = max(v for _, v, _ in bars) * 1.18
    y = ygrid(g, L, R, T, B, 0, hi)
    step = (R - L) / len(bars)
    for i, (lab, v, col) in enumerate(bars):
        w = step * 0.56
        cx = L + step * (i + 0.5)
        g.append(f'<rect x="{cx - w/2:.1f}" y="{y(v):.1f}" width="{w:.1f}" '
                 f'height="{B - y(v):.1f}" fill="{col}" rx="2"/>')
        g.append(f'<text x="{cx:.1f}" y="{y(v) - 8:.1f}" class="val" '
                 f'text-anchor="middle">{v:.0f}</text>')
        for j, line in enumerate(lab.split("\n")):
            g.append(f'<text x="{cx:.1f}" y="{B + 20 + j*14:.1f}" class="tick" '
                     f'text-anchor="middle">{esc(line)}</text>')
    g.append(f'<line x1="{L}" y1="{B}" x2="{R}" y2="{B}" class="axis"/>')
    return g


def panel_alloc(x0, rows, shipped_pair):
    """Per-burst cost against the number of alloc/free round trips in the burst."""
    g, (L, R, T, B) = frame(x0, "2. The allocator's share of the per-burst cost",
                            "cycles per burst above the arm with no allocation")
    if len(rows) < 2:
        return g
    xs = [n for n, _ in rows]
    ys = [c for _, c in rows]
    xhi, yhi = max(xs) * 1.12, max(ys) * 1.14
    y = ygrid(g, L, R, T, B, 0, yhi)

    def x(v):
        return L + v / xhi * (R - L)
    # The line through the arms with three or more pairs, extended back to the
    # origin, is the claim: k pairs cost k times one pair with nothing left over.
    multi = [(n, c) for n, c in rows if n >= 3]
    if len(multi) >= 2:
        nn = len(multi)
        sx = sum(n for n, _ in multi); sy = sum(c for _, c in multi)
        sxx = sum(n * n for n, _ in multi); sxy = sum(n * c for n, c in multi)
        den = nn * sxx - sx * sx
        if den:
            b = (nn * sxy - sx * sy) / den
            a = (sy - b * sx) / nn
            g.append(f'<line x1="{x(0):.1f}" y1="{y(a):.1f}" x2="{x(xhi):.1f}" '
                     f'y2="{y(a + b*xhi):.1f}" stroke="{INK_2}" '
                     f'stroke-width="1.4" stroke-dasharray="5 4"/>')
            g.append(f'<text x="{x(xhi)-6:.1f}" y="{y(a + b*xhi) - 10:.1f}" '
                     f'class="note" text-anchor="end">'
                     f'{b:.0f} cycles per pair, intercept {a:+.0f}</text>')
    pts = " ".join(f"{x(n):.1f},{y(c):.1f}" for n, c in rows)
    g.append(f'<polyline points="{pts}" fill="none" stroke="{BLUE}" '
             f'stroke-width="2.4"/>')
    for n, c in rows:
        g.append(f'<circle cx="{x(n):.1f}" cy="{y(c):.1f}" r="5" fill="{BLUE}" '
                 f'stroke="{SURFACE}" stroke-width="2"/>')
    if shipped_pair:
        g.append(f'<text x="{x(1)+10:.1f}" y="{y(shipped_pair)+4:.1f}" '
                 f'class="val">{shipped_pair:.0f}  (as shipped)</text>')
    for v in (0, 3, 5, 9):
        if v <= xhi:
            g.append(f'<text x="{x(v):.1f}" y="{B + 20:.1f}" class="tick" '
                     f'text-anchor="middle">{v}</text>')
    g.append(f'<line x1="{L}" y1="{B}" x2="{R}" y2="{B}" class="axis"/>')
    g.append(f'<text x="{(L+R)/2:.1f}" y="{B + 44:.1f}" class="tick" '
             f'text-anchor="middle">aligned_alloc / free round trips per burst</text>')
    return g


def panel_depth(x0, at64):
    """Cycles and instructions per packet against pipeline depth, matched burst.

    Deliberately not P and C against depth. Below Q = B the number of pipeline
    fills is ceil(B/Q), a step, so a line in 1/B is the wrong shape and the
    shallow arms' P/C split is an artefact of fitting it anyway. The matched
    burst comparison is real, and it needs both counters: a cycle curve on its
    own cannot separate more work from more waiting.
    """
    g, (L, R, T, B) = frame(x0, "3. The pipeline hides latency, it does not remove work",
                            "at a matched 64-packet burst")
    if len(at64) < 2:
        return g
    ds = sorted(at64)
    cyc = [at64[d][0] for d in ds]
    ins = [at64[d][1] for d in ds]
    span = 0.26
    clo, chi = min(cyc) * (1 - span / 6), min(cyc) * (1 + span)
    ilo, ihi = min(ins) * (1 - span / 6), min(ins) * (1 + span)
    yc = ygrid(g, L, R, T, B, clo, chi, colour=BLUE)
    yi = ygrid(g, L, R, T, B, ilo, ihi, colour=ORANGE, side="right")
    import math
    lo, hi = math.log2(min(ds)), math.log2(max(ds))

    def x(d):
        return L + (math.log2(d) - lo) / (hi - lo) * (R - L)
    for vals, yf, col, dash in ((ins, yi, ORANGE, "5 4"), (cyc, yc, BLUE, "")):
        pts = " ".join(f"{x(d):.1f},{yf(v):.1f}" for d, v in zip(ds, vals))
        da = f' stroke-dasharray="{dash}"' if dash else ""
        g.append(f'<polyline points="{pts}" fill="none" stroke="{col}" '
                 f'stroke-width="2.4"{da}/>')
        for d, v in zip(ds, vals):
            g.append(f'<circle cx="{x(d):.1f}" cy="{yf(v):.1f}" r="5" '
                     f'fill="{col}" stroke="{SURFACE}" stroke-width="2"/>')
    for d in ds:
        cy, ip, sem, _ = at64[d]
        if sem:
            g.append(f'<line x1="{x(d):.1f}" y1="{yc(cy - sem):.1f}" '
                     f'x2="{x(d):.1f}" y2="{yc(cy + sem):.1f}" '
                     f'stroke="{BLUE}" stroke-width="1.6"/>')
        g.append(f'<text x="{x(d):.1f}" y="{yc(cy) + 22:.1f}" class="note" '
                 f'text-anchor="middle">IPC {ip/cy:.2f}</text>')
        g.append(f'<text x="{x(d):.1f}" y="{B + 20:.1f}" class="tick" '
                 f'text-anchor="middle">{d}</text>')
    g.append(f'<line x1="{L}" y1="{B}" x2="{R}" y2="{B}" class="axis"/>')
    g.append(f'<text x="{(L+R)/2:.1f}" y="{B + 44:.1f}" class="tick" '
             f'text-anchor="middle">prefetch pipeline depth</text>')
    g.append(f'<text x="{L - 58}" y="{T - 12}" class="note" fill="{BLUE}">'
             f'cycles / packet</text>')
    g.append(f'<text x="{R + 8}" y="{T - 12}" class="note" fill="{ORANGE}">'
             f'instructions / packet</text>')
    return g


def main():
    allc = json.loads((DOCS / "results_reproduced.json").read_text())
    base_cond = "pinned2_asshipped"

    # Allocator arms, at a matched burst of 64 -- the same estimator §5.13 uses.
    def q1_extra(cond):
        v = at_q1(allc, cond, "dramblast")
        return v
    hoist = q1_extra("alloc_hoisted")
    arows = []
    if hoist:
        for pairs, cond in ((1, base_cond), (3, "alloc_x2"), (5, "alloc_x4"),
                            (9, "alloc_x8")):
            v = q1_extra(cond)
            if v:
                arows.append((pairs, (v - hoist) * 64))
        arows = [(0, 0.0)] + arows
    shipped_pair = dict(arows).get(1)

    # Depth arms at a matched burst of 64.
    at64 = {}
    for Q, cond in ((8, "depth_8"), (16, "depth_16"), (32, "depth_32"),
                    (64, base_cond)):
        runs = [r for r in allc.get(cond, {}).get("dramblast", {}).values()
                if r.get("rx_batch") == 64 and r.get("insns") and r.get("steady_mpps")]
        if not runs:
            continue
        cyc = sum(r["cycles_per_pkt"] for r in runs) / len(runs)
        ipp = sum(r["insns"] / (r["steady_mpps"] * 1e6 * PERF_WINDOW)
                  for r in runs) / len(runs)
        var = sum((r["cycles_per_pkt"] - cyc) ** 2 for r in runs)
        sem = (var / (len(runs) * (len(runs) - 1))) ** 0.5 if len(runs) > 1 else None
        at64[Q] = (cyc, ipp, sem, len(runs))

    W, H = PW * 3, PH
    body = []
    body += panel_backing(0, allc, base_cond)
    body += panel_alloc(PW, arows, shipped_pair)
    body += panel_depth(PW * 2, at64)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'width="{W}" height="{H}" role="img" '
           f'aria-label="Three panels: page backing, allocator share, pipeline depth">'
           f'<style>'
           f'.ttl{{font:600 15px Archivo,system-ui,sans-serif;fill:{INK}}}'
           f'.sub{{font:13px Spectral,Georgia,serif;fill:{INK_2}}}'
           f'.tick{{font:11.5px "IBM Plex Mono",monospace;fill:{INK_2}}}'
           f'.val{{font:600 12px "IBM Plex Mono",monospace;fill:{INK}}}'
           f'.note{{font:11px "IBM Plex Mono",monospace;fill:{INK_2}}}'
           f'.grid{{stroke:{GRID};stroke-width:1}}'
           f'.axis{{stroke:{GRID};stroke-width:1.4}}'
           f'</style>'
           f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>'
           + "".join(body) + "</svg>\n")
    out = DOCS / "matrix.svg"
    out.write_text(svg)
    print(f"wrote {out}  ({len(svg)} bytes)")


if __name__ == "__main__":
    main()
