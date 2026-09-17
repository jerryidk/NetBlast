#!/usr/bin/env python3
"""What each candidate change to dramblast's lookup path is worth.

Reads the TSV that check_dramblast_arms.sh writes (arm, repeat, TSC ticks per
packet) and draws one horizontal bar per arm, with the repeats overplotted as
dots so that the scatter is visible rather than hidden behind an error bar.
Arms that do not beat the baseline are drawn in the same ink as the ones that
do -- the point of the figure is that three of the six candidates did nothing,
and colouring only the winners would bury that.

Hand-written SVG with no dependencies, matching plot_matrix.py: matplotlib
lives in the nix dev shell and this has to run from a plain shell.

    python3 l2fwd/plot_dramblast_arms.py <arms.tsv>  -> docs/dramblast_arms.svg
"""
import pathlib
import statistics as st
import sys

from plotlib import DOCS, esc, wrap_caption

SURFACE, INK, INK_2, GRID = "#fbfaf7", "#1a1a1a", "#5a5a5a", "#e0ddd6"
BLUE, ORANGE, GREEN = "#12707f", "#bb551c", "#4a7a3a"

# Order is the order of the argument, not of the result: shipped first, then
# each candidate as a delta against the arm it modifies.
ORDER = [
    ("shipped", "as shipped", ORANGE),
    ("maskfix", "+ find-mask fix", BLUE),
    ("maskfix+alloc", "+ hoisted result buffer", BLUE),
    ("prefetchT1", "+ prefetcht1 (vs t2)", GREEN),
    ("prefetchT0", "+ prefetcht0", GREEN),
    ("hoistloop", "+ loop state in locals", GREEN),
    ("hoistloop+alloc", "+ that, and hoisted buffer", GREEN),
    ("vecspill", "+ vector spill on hit", GREEN),
]

CAPTION = ("The find-mask fix is now upstream, so the tree is the fixed code "
           "and `shipped` is synthesised by reverting the mask. Every other arm "
           "is the tree plus one further change.")


def main(path):
    rows = {}
    for line in pathlib.Path(path).read_text().splitlines():
        if not line.strip():
            continue
        arm, _run, val = line.split("\t")
        rows.setdefault(arm, []).append(float(val))

    arms = [(k, lab, col, rows[k]) for k, lab, col in ORDER if k in rows]
    if not arms:
        sys.exit("no known arms in %s" % path)

    # Geometry. The label column is sized from the longest label at the font
    # size actually used, rather than guessed -- three captions in this repo's
    # figures have already been clipped by a guessed margin (INVESTIGATION.md
    # section 5.22).
    FS = 13
    CHAR_W = FS * 0.55           # conservative for this sans stack
    LABEL_W = int(max(len(lab) for _, lab, _, _ in arms) * CHAR_W) + 16
    ROW_H, BAR_H = 34, 16
    PAD_L, PAD_R, PAD_T = 18, 26, 54
    PLOT_W = 430
    LEGEND_H = 34
    CAP_H = 34
    W = PAD_L + LABEL_W + PLOT_W + PAD_R
    H = PAD_T + ROW_H * len(arms) + LEGEND_H + CAP_H

    lo = 0.0
    hi = max(max(v) for _, _, _, v in arms) * 1.08
    x0 = PAD_L + LABEL_W

    def sx(v):
        return x0 + (v - lo) / (hi - lo) * PLOT_W

    o = []
    a = o.append
    a('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
      'height="%d" font-family="system-ui,-apple-system,Segoe UI,sans-serif">'
      % (W, H, W, H))
    a('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    a('<text x="%d" y="24" font-size="15" font-weight="600" fill="%s">'
      'dramblast lookup path: what each change is worth</text>' % (PAD_L, INK))
    a('<text x="%d" y="42" font-size="12" fill="%s">TSC ticks per packet, '
      'lower is better; %d repeats, interleaved</text>'
      % (PAD_L, INK_2, len(arms[0][3])))

    # gridlines
    step = 10
    t = 0
    while t <= hi:
        a('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" '
          'stroke-width="1"/>' % (sx(t), PAD_T - 8, sx(t), PAD_T + ROW_H * len(arms) - 10, GRID))
        a('<text x="%.1f" y="%d" font-size="11" fill="%s" text-anchor="middle">'
          '%d</text>' % (sx(t), PAD_T - 14, INK_2, t))
        t += step

    for i, (_k, lab, col, vals) in enumerate(arms):
        y = PAD_T + i * ROW_H
        m = st.median(vals)   # median, not mean: these were taken on the
                              # shared housekeeping cpuset, where one daemon
                              # wake-up during a twelve-second run moves a mean
                              # and does not move a median.
        a('<text x="%d" y="%.1f" font-size="%d" fill="%s" text-anchor="end">'
          '%s</text>' % (x0 - 10, y + BAR_H * 0.78, FS, INK, esc(lab)))
        a('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" fill="%s" '
          'fill-opacity="0.85"/>' % (x0, y, sx(m) - x0, BAR_H, col))
        for v in vals:
            a('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s" fill-opacity="0.9"/>'
              % (sx(v), y + BAR_H / 2, INK))
        a('<text x="%.1f" y="%.1f" font-size="11" fill="%s">%.1f</text>'
          % (sx(m) + 7, y + BAR_H * 0.78, INK_2, m))

    # Legend, separate from the bars rather than labels on them.
    ly = PAD_T + ROW_H * len(arms) + 4
    for j, (col, txt) in enumerate([(ORANGE, "as shipped"),
                                    (BLUE, "confirmed win"),
                                    (GREEN, "candidate, not confirmed"),
                                    (INK, "individual repeats")]):
        lx = PAD_L + j * (W - 2 * PAD_L) / 4.0
        if txt == "individual repeats":
            a('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/>' % (lx + 5, ly + 5, col))
        else:
            a('<rect x="%.1f" y="%.1f" width="10" height="10" fill="%s"/>'
              % (lx, ly, col))
        a('<text x="%.1f" y="%.1f" font-size="11" fill="%s">%s</text>'
          % (lx + 15, ly + 9, INK_2, esc(txt)))

    # Caption, wrapped so it cannot run off the right edge. The 0.6*font-size
    # per character estimate and the reason for it live in plotlib.wrap_caption.
    lines = wrap_caption(CAPTION, W - 2 * PAD_L, 11)
    for j, ln in enumerate(lines):
        a('<text x="%d" y="%.1f" font-size="11" fill="%s">%s</text>'
          % (PAD_L, ly + 26 + j * 13, INK_2, esc(ln)))

    a("</svg>")
    out = DOCS / "dramblast_arms.svg"
    out.write_text("\n".join(o))
    print("wrote %s" % out)

    # Print the table too; the figure is for looking at, the table for quoting.
    base = st.median(rows["shipped"])
    print("\n%-28s %8s %8s %8s %11s" % ("arm", "median", "mean", "sd",
                                          "vs shipped"))
    for _k, lab, _c, vals in arms:
        sd = st.stdev(vals) if len(vals) > 1 else 0.0
        print("%-28s %8.2f %8.2f %8.2f %+11.2f"
              % (lab, st.median(vals), st.mean(vals), sd,
                 st.median(vals) - base))


if __name__ == "__main__":
    main(sys.argv[1])
