#!/usr/bin/env python3
"""Is the rig's 5 cycles per packet a real cost, or an artefact of the instrument?

Reads the CSV that bench_timed_region.c writes and draws cycles per packet
against burst size for the `-m none` loop reproduced at several memory
footprints, all read through the same unfenced rdtsc pair the rig uses.

The figure answers the question a single number cannot. Three curves and one
point:

  * the instrument floor, 29.95 ticks/burst measured on an EMPTY region, drawn
    as floor/b. Where a footprint curve lies on top of it, the reading is the
    instrument and not the loop.
  * one curve per footprint. The same eleven instructions cost 3.5 cycles per
    packet with the packets in L1/L2 and 25 when every line misses to DRAM, so
    any reading in between is a statement about where the packets are, not
    about how much work the loop does.
  * the rig's own (64, 5), drawn as a point. It has to fall inside the bracket
    for the reported number to mean anything, and where inside it falls says
    which memory the NIC left the packets in.

Hand-written SVG with no dependencies, matching plot_dram_ceiling.py:
matplotlib lives in the nix dev shell and this has to run from a plain shell.

    python3 l2fwd/plot_timed_region.py <timed_region.csv>  -> docs/timed_region.svg
"""
import csv
import sys

from plotlib import DOCS, check_extents, esc, wrap

SURFACE, INK, INK_2, GRID = "#fbfaf7", "#1a1a1a", "#5a5a5a", "#e0ddd6"
MUTED, RIG = "#9a9a9a", "#b3123c"
# Cool -> warm as the footprint grows, so the ordering is readable without the
# legend as well as with it.
RAMP = ["#12707f", "#2f8f6f", "#6f9a3a", "#a8901c", "#bb551c", "#8c2d18"]

# The rig, from docs/results_reproduced.json, engine_trio/none/q=1: a full
# 64-packet burst, one busy worker, pinned arm so ticks are cycles.
RIG_BURST, RIG_CYC = 64, 5
RIG_LABEL = "the rig: -m none, q=1, burst 64"

W, H = 900, 500
L, R, T, B = 80, 274, 62, 74      # right margin holds the legend


def footprint_mib(stride, pool):
    return stride * 64 * pool / (1024.0 * 1024.0)


# This part: 48 KiB L1d, 2 MiB L2, 52.5 MiB L3, and an L2 TLB of ~2048 entries
# = 8 MiB of 4 KiB pages. Capacity and translation run out at different sizes,
# and the 28 MiB arm is the one that shows it: still inside L3, already past
# the TLB, and reading like DRAM.
def where(mib):
    """Name what the packet lines have run out of, for the legend."""
    if mib < 1.0:
        return "L1/L2 resident"
    if mib < 8.0:
        return "past L2, inside L3 and TLB"
    if mib < 52.5:
        return "inside L3, past the L2 TLB"
    return "past L3 - DRAM and page walks"


def main(path):
    rows = []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            rows.append((int(r["stride"]), int(r["pool"]), int(r["burst"]),
                         float(r["bare_per_pkt"]), float(r["floor_bare"])))
    if not rows:
        sys.exit("no rows in %s" % path)

    floor = sum(r[4] for r in rows) / len(rows)
    series = {}
    for stride, pool, burst, per_pkt, _f in rows:
        series.setdefault((stride, pool), {})[burst] = per_pkt
    # Only burst sizes every arm reached, so no curve ends in mid-air.
    common = sorted(set.intersection(*(set(v) for v in series.values())),
                    reverse=True)
    if RIG_BURST not in common:
        sys.exit("no arm was measured at burst %d" % RIG_BURST)

    keys = sorted(series, key=lambda k: footprint_mib(*k))
    ymax = max(max(v[b] for b in common) for v in series.values()) * 1.12
    pw, ph = W - L - R, H - T - B

    # Reciprocal x, as the report's burst-model figure uses: a per-burst cost
    # is a straight line on 1/b, so the instrument floor is visibly a line.
    xs = [1.0 / b for b in common]
    xlo, xhi = min(xs), max(xs)

    def sx(b):
        return L + pw * (1.0 / b - xlo) / (xhi - xlo)

    def sy(v):
        return T + ph - ph * v / ymax

    out = []
    a = out.append
    a('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
      'height="%d" font-family="Helvetica,Arial,sans-serif">' % (W, H, W, H))
    a('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SURFACE))
    a('<text x="%d" y="28" font-size="16" font-weight="600" fill="%s">'
      'Where the rig\'s 5 cycles per packet sits</text>' % (L, INK))
    a('<text x="%d" y="47" font-size="11.5" fill="%s">the same eleven '
      'instructions, same unfenced rdtsc pair, at four memory footprints'
      '</text>' % (L, INK_2))

    step = 5 if ymax <= 40 else 10
    v = 0
    while v <= ymax:
        y = sy(v)
        a('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s"/>'
          % (L, y, L + pw, y, GRID))
        a('<text x="%d" y="%.1f" font-size="10.5" text-anchor="end" fill="%s">'
          '%d</text>' % (L - 8, y + 3.5, INK_2, v))
        v += step

    for b in common:
        a('<text x="%.1f" y="%d" font-size="10.5" text-anchor="middle" '
          'fill="%s">%d</text>' % (sx(b), T + ph + 18, INK_2, b))
    a('<text x="%.1f" y="%d" font-size="11.5" text-anchor="middle" fill="%s">'
      'RX burst size (packets, reciprocal scale)</text>'
      % (L + pw / 2, T + ph + 40, INK))
    a('<text transform="translate(22,%.1f) rotate(-90)" font-size="11.5" '
      'text-anchor="middle" fill="%s">cycles per packet, as the rig reads it'
      '</text>' % (T + ph / 2, INK))

    # The instrument's own floor, floor/b: everything at or below this line is
    # the timestamp pair rather than the loop.
    d = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L", sx(b),
                                  sy(min(floor / b, ymax)))
                 for i, b in enumerate(common))
    a('<path d="%s" fill="none" stroke="%s" stroke-width="1.6" '
      'stroke-dasharray="5,4"/>' % (d, MUTED))

    for i, k in enumerate(keys):
        col = RAMP[i % len(RAMP)]
        pts = series[k]
        d = " ".join("%s%.1f,%.1f" % ("M" if j == 0 else "L", sx(b),
                                      sy(min(pts[b], ymax)))
                     for j, b in enumerate(common))
        a('<path d="%s" fill="none" stroke="%s" stroke-width="2.2"/>' % (d, col))
        for b in common:
            a('<circle cx="%.1f" cy="%.1f" r="3.4" fill="%s"/>'
              % (sx(b), sy(min(pts[b], ymax)), col))

    # The rig itself. Ringed rather than filled so it reads as a different kind
    # of thing from the reproduction's points.
    a('<circle cx="%.1f" cy="%.1f" r="7" fill="none" stroke="%s" '
      'stroke-width="2.6"/>' % (sx(RIG_BURST), sy(RIG_CYC), RIG))
    a('<circle cx="%.1f" cy="%.1f" r="2.6" fill="%s"/>'
      % (sx(RIG_BURST), sy(RIG_CYC), RIG))

    # Separate legend: five curves converge at the left edge, where an inline
    # label could not be attached to any of them.
    lx, ly = L + pw + 24, T + 6
    a('<text x="%d" y="%d" font-size="11" font-weight="600" fill="%s">'
      'Packet footprint</text>' % (lx, ly, INK))
    ly += 20
    for i, k in enumerate(keys):
        col = RAMP[i % len(RAMP)]
        mib = footprint_mib(*k)
        lab = "%.1f MiB - %s" % (mib, where(mib))
        a('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" '
          'stroke-width="2.2"/>' % (lx, ly - 4, lx + 22, ly - 4, col))
        a('<circle cx="%d" cy="%d" r="3.4" fill="%s"/>' % (lx + 11, ly - 4, col))
        parts = wrap(lab, 24)
        for j, part in enumerate(parts):
            a('<text x="%d" y="%d" font-size="10.5" fill="%s">%s</text>'
              % (lx + 30, ly + j * 13, INK_2, esc(part)))
        ly += 13 * len(parts) + 8
    ly += 4
    a('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1.6" '
      'stroke-dasharray="5,4"/>' % (lx, ly - 4, lx + 22, ly - 4, MUTED))
    for j, part in enumerate(wrap("instrument floor alone, %.1f ticks/burst"
                                  % floor, 24)):
        a('<text x="%d" y="%d" font-size="10.5" fill="%s">%s</text>'
          % (lx + 30, ly + j * 13, INK_2, esc(part)))
    ly += 13 * len(wrap("instrument floor alone, %.1f ticks/burst" % floor, 24)) + 12
    a('<circle cx="%d" cy="%d" r="6" fill="none" stroke="%s" '
      'stroke-width="2.4"/>' % (lx + 11, ly - 4, RIG))
    for j, part in enumerate(wrap(RIG_LABEL, 24)):
        a('<text x="%d" y="%d" font-size="10.5" fill="%s">%s</text>'
          % (lx + 30, ly + j * 13, INK, esc(part)))
    ly += 13 * len(wrap(RIG_LABEL, 24)) + 16

    hot = series[keys[0]][RIG_BURST]
    cold = series[keys[-1]][RIG_BURST]
    verdict = ("At burst 64 the bracket is %.1f to %.1f. The rig's %d is "
               "inside it, above the issue-limited floor and far below a "
               "cache miss." % (hot, cold, RIG_CYC))
    for j, part in enumerate(wrap(verdict, 27)):
        a('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>'
          % (lx, ly + j * 12.5, INK, esc(part)))

    a('</svg>')
    svg = "\n".join(out)

    bad = check_extents(svg, W, H)
    if bad:
        sys.exit("labels outside the canvas: %s" % "; ".join(bad))

    DOCS.mkdir(exist_ok=True)
    (DOCS / "timed_region.svg").write_text(svg)
    print("wrote docs/timed_region.svg  (floor %.2f, bracket %.1f-%.1f at b=64)"
          % (floor, hot, cold))


if __name__ == "__main__":
    main(sys.argv[1])
