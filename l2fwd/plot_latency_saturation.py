#!/usr/bin/env python3
"""The answer to §5.31: latency, not bandwidth.

§5.31 left an open question. At high worker counts the core is ~58%
memory-bound while every instrument measuring a memory *resource* reads 1-5%.
That cannot be a bandwidth or a capacity limit, and none of §5.27's nine
enumerated resources could explain it.

This figure plots the resource §5.27 never enumerated. Mean data-read latency
rises 9.3x across the sweep while DRAM bandwidth utilisation never exceeds 8%.
A bandwidth-limited system runs out of throughput; this one runs out of
tolerance for how long one access takes. The two are drawn together because the
disagreement between them IS the result -- either curve alone is unremarkable.

Deliberately its own figure rather than another series on saturation.svg: that
plot's y axis is "percentage of a known ceiling", and latency has no ceiling to
divide by. Forcing it onto that axis would mean inventing a denominator, which
is the exact failure §5.30 records.

    python3 l2fwd/plot_latency_saturation.py <outdir> -> docs/latency_vs_bandwidth.svg
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from plotlib import DOCS, esc, check_extents          # noqa: E402

SURFACE, INK, INK_2, GRID = "#fbfaf7", "#1a1a1a", "#5a5a5a", "#e0ddd6"
C_LAT, C_BW, C_MLP = "#a8324a", "#12707f", "#bb551c"

W, H = 760, 500
PAD_L, PAD_R, PAD_T, PAD_B = 74, 74, 58, 150
PW, PH = W - PAD_L - PAD_R, H - PAD_T - PAD_B


def val(path, pat):
    for line in path.read_text().splitlines():
        f = line.split(",")
        if len(f) > 2 and re.search(pat, f[2]):
            try:
                return float(f[0])
            except ValueError:
                return None
    return None


def collect(outdir):
    d = pathlib.Path(outdir)
    rows = {}
    for p in sorted(d.glob("*_q*_latency.perf")):
        q = int(re.search(r"_q(\d+)_", p.name).group(1))
        out = val(p, r"outstanding_data_rd$")
        req = val(p, r"offcore_reqs_data_rd$")
        if out and req:
            rows.setdefault(q, {})["lat"] = out / req
    for p in sorted(d.glob("*_q*_mlp.perf")):
        q = int(re.search(r"_q(\d+)_", p.name).group(1))
        pend = val(p, r"l1d_pend_miss_pending$")
        pc = val(p, r"pending_cycles")
        if pend and pc:
            rows.setdefault(q, {})["mlp"] = pend / pc
    # DRAM bandwidth: eight controllers, read + write, 64 B per CAS, over the
    # perf window. Same arithmetic as analyse_saturation, restated rather than
    # imported so this figure does not depend on that module's globals.
    for p in sorted(d.glob("*_q*_bw.perf")):
        q = int(re.search(r"_q(\d+)_", p.name).group(1))
        tot = 0.0
        for i in range(8):
            for kind in ("rd", "wr"):
                v = val(p, r"cas_%s_%d$" % (kind, i))
                if v:
                    tot += v
        if tot:
            rows.setdefault(q, {})["gbs"] = tot * 64 / 1e9 / 8.0
    return rows


def dram_ceiling():
    f = pathlib.Path(__file__).resolve().parent / ".dram_ceiling"
    m = re.search(r"^DRAM_ACHIEVED_GBS=([0-9.]+)", f.read_text(), re.M)
    if not (m and float(m.group(1)) > 0):
        sys.exit("refusing to plot: .dram_ceiling has no measured ceiling. "
                 "Run dram_ceiling.sh first rather than dividing by a guess.")
    return float(m.group(1))


def main(outdir):
    rows = collect(outdir)
    qs = sorted(k for k in rows if "lat" in rows[k])
    if not qs:
        sys.exit("no latency counters in %s -- was the latency group run?" % outdir)
    ceil = dram_ceiling()

    lat = [rows[q]["lat"] for q in qs]
    bwpc = [100.0 * rows[q].get("gbs", 0) / ceil for q in qs]
    mlp = [rows[q].get("mlp", 0) for q in qs]
    lat_hi = max(800.0, max(lat) * 1.12)

    o = []
    a = o.append
    a('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
      'viewBox="0 0 %d %d" font-family="Inter,Helvetica,Arial,sans-serif">'
      % (W, H, W, H))
    a('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    a('<text x="%d" y="26" font-size="15" font-weight="600" fill="%s">%s</text>'
      % (PAD_L, INK, esc("Latency, not bandwidth")))
    a('<text x="%d" y="44" font-size="12" fill="%s">%s</text>'
      % (PAD_L, INK_2,
         esc("one access costs 9.3x more at 23 workers; the memory system is "
             "never above 8% of its ceiling")))

    def sx(i):
        return PAD_L + (i + 0.5) / len(qs) * PW

    def sy_lat(v):
        return PAD_T + PH - (v / lat_hi) * PH

    def sy_pc(v):
        return PAD_T + PH - (v / 100.0) * PH

    # left axis: cycles
    for v in range(0, int(lat_hi) + 1, 200):
        y = sy_lat(v)
        a('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
          % (PAD_L, y, PAD_L + PW, y, GRID))
        a('<text x="%d" y="%.1f" font-size="10" text-anchor="end" fill="%s">%d</text>'
          % (PAD_L - 8, y + 3, INK_2, v))
    # transform on the <text> itself, not a wrapping <g>: check_extents reads
    # each text element's own attributes, so a rotation on the parent is
    # invisible to it and the label is measured as horizontal.
    a('<text transform="translate(20,%d) rotate(-90)" x="0" y="0" font-size="11" '
      'fill="%s" text-anchor="middle">%s</text>'
      % (PAD_T + PH / 2, C_LAT, esc("cycles per data read")))

    # right axis: percent
    for v in range(0, 101, 25):
        y = sy_pc(v)
        a('<text x="%d" y="%.1f" font-size="10" fill="%s">%d%%</text>'
          % (PAD_L + PW + 8, y + 3, C_BW, v))
    a('<text transform="translate(%d,%d) rotate(-90)" x="0" y="0" font-size="11" '
      'fill="%s" text-anchor="middle">%s</text>'
      % (PAD_L + PW + 52, PAD_T + PH / 2, C_BW, esc("% of DRAM ceiling")))

    for i, q in enumerate(qs):
        a('<text x="%.1f" y="%d" font-size="10" text-anchor="middle" fill="%s">%d</text>'
          % (sx(i), PAD_T + PH + 18, INK_2, q))
    a('<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="%s">%s</text>'
      % (PAD_L + PW / 2, PAD_T + PH + 38, INK, esc("worker cores")))

    def poly(pts, col, dash=""):
        a('<polyline fill="none" stroke="%s" stroke-width="2.2"%s points="%s"/>'
          % (col, dash, " ".join("%.1f,%.1f" % p for p in pts)))
        for x, y in pts:
            a('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>' % (x, y, col))

    poly([(sx(i), sy_lat(v)) for i, v in enumerate(lat)], C_LAT)
    poly([(sx(i), sy_pc(v)) for i, v in enumerate(bwpc)], C_BW)
    poly([(sx(i), sy_pc(v * 10)) for i, v in enumerate(mlp)], C_MLP,
         ' stroke-dasharray="5,3"')

    # separate legend, below the plot
    lx = PAD_L
    for col, lab in ((C_LAT, "mean data-read latency"),
                     (C_BW, "DRAM bandwidth used"),
                     (C_MLP, "misses in flight (x10)")):
        a('<rect x="%d" y="%d" width="10" height="10" fill="%s"/>'
          % (lx, PAD_T + PH + 52, col))
        a('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>'
          % (lx + 14, PAD_T + PH + 61, INK_2, esc(lab)))
        lx += 200

    a('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>'
      % (PAD_L, H - 30, INK_2,
         esc("Ceiling = %.1f GB/s, measured (dram_ceiling.sh). Latency is "
             "Little's law over OFFCORE_REQUESTS[_OUTSTANDING].DATA_RD."
             % ceil)))
    a('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>'
      % (PAD_L, H - 14, INK_2,
         esc("Misses in flight scaled x10 to share the right axis; it falls "
             "2.3 -> 1.3 while latency rises.")))
    a("</svg>")

    svg = "\n".join(o)
    bad = check_extents(svg, W, H)
    if bad:
        sys.exit("labels outside the canvas: %s" % "; ".join(bad))
    out = DOCS / "latency_vs_bandwidth.svg"
    out.write_text(svg)
    print("wrote %s" % out)
    for i, q in enumerate(qs):
        print("  q=%-3d latency %7.1f cyc   bandwidth %5.1f%%   mlp %.2f"
              % (q, lat[i], bwpc[i], mlp[i]))


if __name__ == "__main__":
    main(sys.argv[1])
