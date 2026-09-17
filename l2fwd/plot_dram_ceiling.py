#!/usr/bin/env python3
"""Does the DRAM ceiling plateau before the thread count l2fwd actually runs at?

Reads the CSV that dram_ceiling.sh writes and draws achieved bandwidth against
thread count for the read-only and mixed arms, with the nominal 8-channel peak
drawn as a reference line.

The figure exists to answer one question that a single number cannot: whether
the value written into .dram_ceiling as DRAM_ACHIEVED_GBS is a ceiling or a
lower bound. A curve still climbing at the right-hand edge is a lower bound, and
every utilisation divided by it is overstated. The withdrawn 360.0 GB/s had
exactly that defect and nothing on the page showed it.

Hand-written SVG with no dependencies, matching plot_dramblast_arms.py:
matplotlib lives in the nix dev shell and this has to run from a plain shell.

    python3 l2fwd/plot_dram_ceiling.py <dram_ceiling.csv>  -> docs/dram_ceiling.svg
"""
import csv
import pathlib
import sys

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"

SURFACE, INK, INK_2, GRID = "#fbfaf7", "#1a1a1a", "#5a5a5a", "#e0ddd6"
BLUE, ORANGE, MUTED = "#12707f", "#bb551c", "#9a9a9a"

PEAK = 307.2          # dmidecode: 8 channels x DDR5-4800 x 8 B
PEAK_LABEL = "nominal 8-channel peak, 307.2 GB/s"

W, H = 860, 470
L, R, T, B = 78, 250, 58, 68      # right margin holds the legend


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main(path):
    series = {}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            series.setdefault(row["mode"], []).append(
                (int(row["nthreads"]), float(row["total_gbs"])))
    for k in series:
        series[k].sort()
    if not series:
        sys.exit("no rows in %s" % path)

    threads = sorted({t for pts in series.values() for t, _ in pts})
    ymax = max(PEAK, max(v for pts in series.values() for _, v in pts)) * 1.08

    pw, ph = W - L - R, H - T - B

    def sx(i):
        return L + (pw * i / max(1, len(threads) - 1))

    def sy(v):
        return T + ph - (ph * v / ymax)

    out = []
    a = out.append
    a('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
      'height="%d" font-family="Helvetica,Arial,sans-serif">' % (W, H, W, H))
    a('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    a('<text x="%d" y="26" font-size="16" font-weight="600" fill="%s">'
      'Is the DRAM ceiling a ceiling, or still climbing?</text>' % (L, INK))
    a('<text x="%d" y="45" font-size="11.5" fill="%s">achieved bandwidth vs '
      'thread count, one thread per physical core</text>' % (L, INK_2))

    # y grid and axis
    step = 50
    v = 0
    while v <= ymax:
        y = sy(v)
        a('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s"/>'
          % (L, y, L + pw, y, GRID))
        a('<text x="%d" y="%.1f" font-size="10.5" text-anchor="end" fill="%s">'
          '%d</text>' % (L - 8, y + 3.5, INK_2, v))
        v += step

    # the nominal peak, drawn dashed because it is not achievable
    a('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
      'stroke-dasharray="5,4"/>' % (L, sy(PEAK), L + pw, sy(PEAK), MUTED))

    # x axis
    for i, t in enumerate(threads):
        a('<text x="%.1f" y="%d" font-size="10.5" text-anchor="middle" '
          'fill="%s">%d</text>' % (sx(i), T + ph + 18, INK_2, t))
    a('<text x="%.1f" y="%d" font-size="11.5" text-anchor="middle" fill="%s">'
      'threads (= physical cores)</text>' % (L + pw / 2, T + ph + 40, INK))

    # rotated y caption, CENTRED on the plot area: rotate(-90) runs text upward
    # from its anchor, and three captions in this repo have already been clipped
    # by anchoring one at the top of the plot instead (INVESTIGATION.md 5.22).
    a('<text transform="translate(20,%.1f) rotate(-90)" font-size="11.5" '
      'text-anchor="middle" fill="%s">GB/s</text>' % (T + ph / 2, INK))

    order = [("mixed", "mixed read+write (the denominator)", BLUE),
             ("read", "read-only", ORANGE)]
    for key, _lab, col in order:
        pts = series.get(key)
        if not pts:
            continue
        d = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L",
                                      sx(threads.index(t)), sy(v))
                     for i, (t, v) in enumerate(pts))
        a('<path d="%s" fill="none" stroke="%s" stroke-width="2.2"/>' % (d, col))
        for t, val in pts:
            a('<circle cx="%.1f" cy="%.1f" r="3.4" fill="%s"/>'
              % (sx(threads.index(t)), sy(val), col))

    # separate legend, never end-of-line labels: the two arms converge at low
    # thread counts, where an inline label cannot be attached to either.
    lx, ly = L + pw + 22, T + 8
    a('<text x="%d" y="%d" font-size="11" font-weight="600" fill="%s">'
      'Arms</text>' % (lx, ly, INK))
    ly += 20
    for key, lab, col in order:
        if key not in series:
            continue
        a('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="2.2"/>'
          % (lx, ly - 4, lx + 22, ly - 4, col))
        a('<circle cx="%d" cy="%d" r="3.4" fill="%s"/>' % (lx + 11, ly - 4, col))
        for j, part in enumerate(wrap(lab, 24)):
            a('<text x="%d" y="%d" font-size="10.5" fill="%s">%s</text>'
              % (lx + 30, ly + j * 13, INK_2, esc(part)))
        ly += 13 * max(1, len(wrap(lab, 24))) + 10
    a('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1.6" '
      'stroke-dasharray="5,4"/>' % (lx, ly - 4, lx + 22, ly - 4, MUTED))
    for j, part in enumerate(wrap(PEAK_LABEL, 24)):
        a('<text x="%d" y="%d" font-size="10.5" fill="%s">%s</text>'
          % (lx + 30, ly + j * 13, INK_2, esc(part)))
    ly += 13 * len(wrap(PEAK_LABEL, 24)) + 14

    # the verdict, in the legend column so it cannot be missed
    mixed = series.get("mixed", [])
    if len(mixed) >= 2:
        growth = (mixed[-1][1] / mixed[-2][1] - 1) * 100 if mixed[-2][1] else 0
        verdict = ("plateaued (+%.1f%% over the last step): a ceiling"
                   % growth) if growth < 5 else \
                  ("still climbing (+%.1f%% over the last step): a LOWER "
                   "BOUND, so utilisations divided by it are overstated"
                   % growth)
        for j, part in enumerate(wrap(verdict, 26)):
            a('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>'
              % (lx, ly + j * 12.5, INK, esc(part)))

    a('</svg>')
    svg = "\n".join(out)

    # Every <text> must fit the viewBox. This repo has shipped four clipped
    # captions found only by this check, never by looking at the page -- there
    # is no rasteriser on this host.
    bad = check_extents(svg)
    if bad:
        sys.exit("labels outside the canvas: %s" % "; ".join(bad))

    DOCS.mkdir(exist_ok=True)
    (DOCS / "dram_ceiling.svg").write_text(svg)
    print("wrote docs/dram_ceiling.svg")


def wrap(text, n):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def check_extents(svg):
    """Approximate each <text>'s box and report any that leaves the canvas."""
    import re
    bad = []
    for m in re.finditer(r'<text ([^>]*)>([^<]*)</text>', svg):
        attrs, body = m.group(1), m.group(2)
        if "rotate(-90)" in attrs:
            continue                      # centred by construction above
        fs = float(re.search(r'font-size="([\d.]+)"', attrs).group(1))
        x = float(re.search(r'x="([-\d.]+)"', attrs).group(1))
        y = float(re.search(r'y="([-\d.]+)"', attrs).group(1))
        w = len(body) * fs * 0.55
        anchor = re.search(r'text-anchor="(\w+)"', attrs)
        anchor = anchor.group(1) if anchor else "start"
        x0 = x - w if anchor == "end" else x - w / 2 if anchor == "middle" else x
        if x0 < 0 or x0 + w > W or y - fs < 0 or y > H:
            bad.append("%r at (%.0f,%.0f)" % (body[:28], x, y))
    return bad


if __name__ == "__main__":
    main(sys.argv[1])
