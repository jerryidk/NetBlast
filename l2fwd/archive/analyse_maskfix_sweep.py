#!/usr/bin/env python3
"""Pair the find-mask arms by queue count and draw the result.

Reads the summary.txt that run_maskfix_sweep.sh appends to, which interleaves
the two arms within each repeat. The statistic is the PAIRED difference at each
queue count within each repeat, so drift between repeats cancels and the spread
of the pairs is a measured error bar rather than an assumed one -- the design
docs/INVESTIGATION.md section 5.14 had to adopt after comparing arm means.

Comparisons are made only where BOTH arms reached the same RX burst size.
`Cycle per fwd packet` is an integer, so at a 64-packet burst one printed tick
is 64 cycles per burst (section 5.19); a difference of one or two ticks is at
the instrument's resolution and is reported as such rather than quoted.

    python3 l2fwd/analyse_maskfix_sweep.py <outdir>  -> docs/maskfix_sweep.svg
"""
import pathlib
import re
import statistics as st
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # archive/ -> l2fwd/
from analysis import DOCS, esc, wrap_caption

SURFACE, INK, INK_2, GRID = "#fbfaf7", "#1a1a1a", "#5a5a5a", "#e0ddd6"
BLUE, ORANGE = "#12707f", "#bb551c"

TAG = re.compile(r"tag=(r(\d+)_(shipped|maskfix))")
ROW = re.compile(
    r"q=(\d+)\s+.*?avg=([\d.]+)\s+cyc=(\d+|NA)\s+batch=(\d+|NA)\s+missed=(\d+|NA)")


def parse(path):
    """-> {(repeat, arm, q): {...}}"""
    out, cur = {}, None
    for line in pathlib.Path(path).read_text().splitlines():
        m = TAG.search(line)
        if m:
            cur = (int(m.group(2)), m.group(3))
            continue
        m = ROW.search(line)
        if m and cur:
            q = int(m.group(1))
            if m.group(3) == "NA" or m.group(4) == "NA":
                continue
            out[(cur[0], cur[1], q)] = dict(
                mpps=float(m.group(2)), cyc=int(m.group(3)),
                batch=int(m.group(4)), missed=m.group(5))
    return out


def main(outdir):
    rows = parse(pathlib.Path(outdir) / "summary.txt")
    if not rows:
        sys.exit("no parsable rows in %s/summary.txt" % outdir)

    repeats = sorted({k[0] for k in rows})
    qs = sorted({k[2] for k in rows})

    pairs = []          # (repeat, q, shipped, maskfix)
    skipped = []
    for r in repeats:
        for q in qs:
            a, b = rows.get((r, "shipped", q)), rows.get((r, "maskfix", q))
            if not a or not b:
                continue
            if a["batch"] != b["batch"]:
                skipped.append((r, q, a["batch"], b["batch"]))
                continue
            pairs.append((r, q, a, b))

    if not pairs:
        sys.exit("no matched-burst pairs")

    d_cyc = [b["cyc"] - a["cyc"] for _r, _q, a, b in pairs]
    d_mpps = [b["mpps"] - a["mpps"] for _r, _q, a, b in pairs]

    def stat(v):
        m = st.mean(v)
        s = st.stdev(v) / len(v) ** 0.5 if len(v) > 1 else 0.0
        return m, s

    mc, sc = stat(d_cyc)
    mm, sm = stat(d_mpps)

    print("matched-burst pairs: %d (%d repeats x q=%s)"
          % (len(pairs), len(repeats), ",".join(map(str, qs))))
    if skipped:
        print("skipped for unequal burst size: %s" % skipped)
    print()
    print("%-8s %-3s %10s %10s %8s   %10s %10s %8s"
          % ("repeat", "q", "cyc ship", "cyc fix", "d cyc",
             "Mpps ship", "Mpps fix", "d Mpps"))
    for r, q, a, b in pairs:
        print("%-8d %-3d %10d %10d %+8d   %10.2f %10.2f %+8.2f"
              % (r, q, a["cyc"], b["cyc"], b["cyc"] - a["cyc"],
                 a["mpps"], b["mpps"], b["mpps"] - a["mpps"]))
    print()
    print("ticks/packet  %+.2f +/- %.2f   (negative = the fix is cheaper)" % (mc, sc))
    print("Mpps          %+.2f +/- %.2f   (positive = the fix is faster)" % (mm, sm))

    # ---- figure: per-queue-count paired points, both metrics ----
    PW, PH, PAD_L, PAD_T, PAD_B = 300, 240, 62, 58, 92
    GAP = 46
    W, H = PAD_L * 2 + PW * 2 + GAP, PAD_T + PH + PAD_B
    o = []
    a_ = o.append
    a_('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
       'height="%d" font-family="system-ui,-apple-system,Segoe UI,sans-serif">'
       % (W, H, W, H))
    a_('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    a_('<text x="%d" y="24" font-size="15" font-weight="600" fill="%s">'
       'The find-mask fix on the rig</text>' % (PAD_L, INK))
    a_('<text x="%d" y="42" font-size="12" fill="%s">dramblast, 100 Gbps '
       'offered, matched RX burst; %d repeats x %d queue counts</text>'
       % (PAD_L, INK_2, len(repeats), len(qs)))

    panels = [
        ("TSC ticks / packet", lambda d: d["cyc"], 0),
        ("Mpps forwarded", lambda d: d["mpps"], 1),
    ]
    for title, get, pi in panels:
        x0 = PAD_L + pi * (PW + GAP)
        vals = [get(d) for _r, _q, a, b in pairs for d in (a, b)]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        lo, hi = lo - span * 0.18, hi + span * 0.18

        def sy(v, lo=lo, hi=hi):
            return PAD_T + PH - (v - lo) / (hi - lo) * PH

        def sx(q, x0=x0):
            return x0 + (q - qs[0] + 0.5) / len(qs) * PW

        a_('<text x="%d" y="%d" font-size="12" font-weight="600" fill="%s">%s'
           '</text>' % (x0, PAD_T - 10, INK, esc(title)))
        a_('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s"/>'
           % (x0, PAD_T + PH, x0 + PW, PAD_T + PH, GRID))
        for frac in (0, 0.5, 1.0):
            v = lo + frac * (hi - lo)
            a_('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
               % (x0, sy(v), x0 + PW, sy(v), GRID))
            a_('<text x="%d" y="%.1f" font-size="10" fill="%s" '
               'text-anchor="end">%.0f</text>' % (x0 - 6, sy(v) + 3, INK_2, v))
        for q in qs:
            a_('<text x="%.1f" y="%d" font-size="10" fill="%s" '
               'text-anchor="middle">%d</text>'
               % (sx(q), PAD_T + PH + 15, INK_2, q))
        a_('<text x="%.1f" y="%d" font-size="11" fill="%s" text-anchor="middle">'
           'queue pairs</text>' % (x0 + PW / 2, PAD_T + PH + 32, INK_2))

        for _r, q, aa, bb in pairs:
            a_('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
               'stroke-width="1" stroke-opacity="0.45"/>'
               % (sx(q), sy(get(aa)), sx(q), sy(get(bb)), INK_2))
            a_('<circle cx="%.1f" cy="%.1f" r="3.2" fill="%s"/>'
               % (sx(q), sy(get(aa)), ORANGE))
            a_('<circle cx="%.1f" cy="%.1f" r="3.2" fill="%s"/>'
               % (sx(q), sy(get(bb)), BLUE))

    ly = PAD_T + PH + 48
    for j, (col, txt) in enumerate([(ORANGE, "as shipped"),
                                    (BLUE, "find-mask fixed")]):
        lx = PAD_L + j * 190
        a_('<circle cx="%.1f" cy="%.1f" r="3.2" fill="%s"/>' % (lx + 4, ly + 4, col))
        a_('<text x="%.1f" y="%.1f" font-size="11" fill="%s">%s</text>'
           % (lx + 14, ly + 8, INK_2, esc(txt)))

    cap = ("Paired within each repeat at equal RX burst size: %+.2f ticks/packet "
           "and %+.2f Mpps." % (mc, mm))
    lines = wrap_caption(cap, W - 2 * PAD_L, 11)
    for j, ln in enumerate(lines):
        a_('<text x="%d" y="%.1f" font-size="11" fill="%s">%s</text>'
           % (PAD_L, ly + 26 + j * 13, INK_2, esc(ln)))
    a_("</svg>")
    out = DOCS / "maskfix_sweep.svg"
    out.write_text("\n".join(o))
    print("\nwrote %s" % out)


if __name__ == "__main__":
    main(sys.argv[1])
