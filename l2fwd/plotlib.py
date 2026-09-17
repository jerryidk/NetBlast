"""Drawing helpers shared by the figure scripts in this directory.

Two groups live here, both of them "how a figure is drawn" rather than what it
measures, so no script's data handling is affected by anything in this file:

  * Hand-written SVG -- `esc`, `wrap`, `wrap_caption`, `check_extents` -- used
    by plot_matrix.py, plot_timed_region.py, plot_dramblast_arms.py and
    analyse_maskfix_sweep.py. Those plotters exist precisely so that a figure
    can be regenerated from a plain shell, outside the nix dev shell where
    matplotlib lives, so NOTHING in this module may import matplotlib at module
    scope. Nothing does: the matplotlib helpers below only call methods on an
    Axes the caller hands them, and the rcParams live here as a plain dict that
    the caller passes to `plt.rcParams.update`.

  * matplotlib styling -- the palette, `RC`, `style` and `line` -- shared by
    plot_sweep.py and plot_clock_arms.py, the two scripts that draw PNGs.

The two palettes are deliberately NOT unified. The SVG figures are drawn on
#fbfaf7 with #12707f/#bb551c series, the matplotlib ones on #fcfcfb with
#2a78d6/#eb6834, and several SVG plotters carry a third palette of their own
(plot_timed_region.py's footprint ramp). Only the matplotlib tokens, which are
identical across both of their users, are shared here; each SVG plotter keeps
its own constants where they can be read next to the figure they colour.
"""
import pathlib
import re

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"


# --- hand-written SVG --------------------------------------------------------

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def wrap_caption(text, avail_px, font_size=11):
    """Wrap a caption to fit `avail_px` of horizontal room, as a list of lines.

    0.6 * font-size per character is the generous width estimate the callers
    have always used for captions. An earlier 0.52 let a caption pass the wrap
    test and still overrun the canvas by 56 px: the wrap and the extent check
    must agree, or the check is testing a different string than the one drawn.

    Kept separate from `wrap` above rather than folded into it. This variant
    breaks before a word even when the current line is empty, and appends the
    last line unconditionally, so a caption that starts with a word longer than
    the line gets a leading empty line where `wrap` would not. That difference
    never fires on the captions in this repo, but it is a difference, and the
    two call sites this replaces both had the behaviour spelled out below.
    """
    n = int(avail_px / (font_size * 0.6))
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    return lines


def check_extents(svg, W, H):
    """Approximate each <text>'s box and report any that leaves the canvas."""
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


# --- matplotlib figures ------------------------------------------------------
# Categorical slots 1 and 2 of the reference palette, used unmodified.
# Validated light-mode: CVD dE 24.7 (protan), normal-vision dE 33.6, contrast >= 3:1.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
INK_MUTED = "#8a8983"
GRID = "#e4e3de"
BLUE = "#2a78d6"
ORANGE = "#eb6834"

RC = {
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "DejaVu Sans",
    "text.color": INK,
    "axes.labelcolor": INK_2,
    "xtick.color": INK_2,
    "ytick.color": INK_2,
    "axes.edgecolor": GRID,
    "axes.linewidth": 1.0,
    "xtick.major.size": 0,
    "ytick.major.size": 0,
}


def style(ax, title, xlabel, ylabel, xticks=None):
    """Titles, labels, grid and spines. `xticks` is applied only when given:
    the queue-pair panels fix them at 1..10, the burst-size panel must not."""
    ax.set_title(title, fontsize=12, color=INK, pad=12, loc="left", fontweight="medium")
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.grid(True, color=GRID, linewidth=0.8, alpha=0.9)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    if xticks is not None:
        ax.set_xticks(xticks)
    ax.tick_params(labelsize=9)


def line(ax, xs, ys, color, label, dashed=False):
    """2px line, >=8px markers with a 2px surface ring; dashed arms get squares."""
    ax.plot(xs, ys, color=color, linewidth=2.0,
            linestyle=(0, (4, 3)) if dashed else "-",
            marker="s" if dashed else "o", markersize=6,
            markerfacecolor=color, markeredgecolor=SURFACE, markeredgewidth=2.0,
            label=label, zorder=3, clip_on=False)
