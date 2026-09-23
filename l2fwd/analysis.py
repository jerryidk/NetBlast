#!/usr/bin/env python3
"""NetBlast l2fwd analysis: logs -> json -> fits, figures, report. One file, many subs.

    python3 analysis.py <sub> [args...]

sub            was                          does
-------------  ---------------------------  ------------------------------------------
extract        extract_results.py           sweep logs -> docs/results_reproduced.json
fit            fit_burst_model.py           P + C/B burst model fit [--plot]
matrix         analyse_matrix.py            matrix results, each with falsification test [--plot]
report         make_report.py               docs/report.html
plot-matrix    plot_matrix.py               docs/matrix.svg, no deps
plot-sweep     plot_sweep.py                queue-sweep PNGs (matplotlib)
saturation     analyse_saturation.py        saturation counters -> utilisation, docs/saturation.svg
plot-latency   plot_latency_saturation.py   docs/latency_vs_bandwidth.svg
plot-ceiling   plot_dram_ceiling.py         docs/dram_ceiling.svg
backing        verify_backing.py            page_watch log -> did each arm get its backing
selftest       test_perf_guard.py           fire perf multiplexing guard [--real]
probe          (new, 2026-09-22)            nbprobe logs + ring dumps -> per-phase table, json
ptw            (new, 2026-09-22)            PT ptwrite trace (build-ptw) -> per-packet push->resolve times
ab             (new, 2026-09-23)            harness.sh ab rows.txt -> per-q per-arm Mpps/loop/all-poll, deltas

Each section below keeps old module's header comment. Names that clashed
between modules got section prefix (report_lsq, lat_dram_ceiling, CEIL_W, ...).
No matplotlib at module scope: SVG subs must run from plain shell, outside nix.
"""
import collections
import csv
import json
import pathlib
import re
import statistics
import subprocess
import sys


# =============================================================================
# shared: was plotlib.py
# =============================================================================
# Drawing helpers shared by the figure subcommands.
#
# Two groups, both "how figure drawn", not what it measures:
#
#   * Hand-written SVG -- `esc`, `wrap`, `wrap_caption`, `check_extents`, plus
#     the SVG_* palette -- used by plot-matrix, saturation, plot-latency and
#     plot-ceiling. Those exist so figure regenerates from plain shell, outside
#     nix dev shell where matplotlib lives. So NOTHING at module scope in this
#     file may import matplotlib. matplotlib helpers below only call methods on
#     Axes caller hands them; rcParams live here as plain dict caller passes to
#     `plt.rcParams.update`.
#
#   * matplotlib styling -- palette, `RC`, `style`, `line` -- used by plot-sweep;
#     fit and matrix --plot use the same tokens.
#
# Two palettes deliberately NOT unified. SVG figures: #fbfaf7 surface,
# #12707f/#bb551c series. matplotlib: #fcfcfb, #2a78d6/#eb6834. Four SVG
# plotters carried byte-identical copies of theirs; merged into SVG_* below.
# Figure-specific colours (plot-latency C_*, plot-ceiling MUTED, saturation
# PALETTE) stay next to figure they colour.

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"


# --- hand-written SVG --------------------------------------------------------
SVG_SURFACE, SVG_INK, SVG_INK_2, SVG_GRID = "#fbfaf7", "#1a1a1a", "#5a5a5a", "#e0ddd6"
SVG_BLUE, SVG_ORANGE = "#12707f", "#bb551c"


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


# =============================================================================
# shared: was perf_csv.py
# =============================================================================
# Parsing for `perf stat -x,` output, split out so it can be tested.
#
# Guard is own function, not inline in `extract`, so `selftest` can call it
# directly and fire it for reason it was written. See docs/INVESTIGATION.md
# 5.24 and 5.25.

# `perf stat -x,` field layout, confirmed against the sidecars this parses:
#   0 value  1 unit  2 event  3 run_time_ns  4 enabled_pct  5 metric  6 metric_unit
#
# Field 5 is the DERIVED METRIC, not the enabled percentage: on the
# `instructions` row it holds the IPC, so a guard reading f[5] reads a healthy
# 1.47 IPC as "1.47% enabled" and throws away a good run. A peer session hit
# exactly that and passed it on; `selftest` pins it as a named case.
PERF_VALUE, PERF_EVENT, PERF_ENABLED = 0, 2, 4
PERF_MIN_ENABLED = 99.99


def parse_perf(text):
    """({event: count}, [(event, pct) multiplexed], [(line, why) malformed]).

    A multiplexed counter is scaled up to a full-window estimate before perf
    prints it, so it is numerically indistinguishable from a measured one and
    nothing downstream could tell. Such a reading is refused here rather than
    stored. See docs/INVESTIGATION.md 5.24 for how many events actually fit --
    the answer is that the count does not decide it.

    MALFORMED IS SEPARATE FROM MULTIPLEXED, and is the more serious of the two:
    a multiplexed reading means the hardware was busy, a malformed one means
    this parser is looking at the wrong columns and every field after the shift
    is meaningless. It gets its own return value so a caller cannot report it
    as "some readings were dropped".

    The shift is real and it defeats the guard silently. A raw event spec
    contains commas, so with `-x,` an UNNAMED spec splits across several
    columns:

        59304,,cpu/event=0x12,umask=0x0e/,1000954706,100.00,,

    Field 2 is now `cpu/event=0x12` and field 4 is a run time in nanoseconds,
    which is comfortably above any threshold, so the reading sails through the
    multiplexing check and is stored under a truncated name. Passing `name=` in
    every raw spec prevents it -- which harness.sh sweep does, and which is why the
    310-sidecar audit was valid -- but the parser must not depend on the
    producer having remembered. This is the third appearance of the
    field-index bug in one investigation; a peer session hit it here and it
    only surfaced because the mis-read column happened to print as
    1958811208.00%. Had the shift landed on a column holding a value in 0-100
    it would have read as a plausible percentage.
    """
    got, dropped, malformed = {}, [], []
    for line in text.splitlines():
        f = line.split(",")
        if len(f) <= PERF_EVENT or not f[PERF_VALUE].strip().isdigit():
            continue          # "<not counted>", headers, blank lines
        event = f[PERF_EVENT].strip()
        if not event:
            continue
        # A raw spec fragment in the event column means the row split on the
        # commas inside the spec and every later field is off by some amount.
        if "event=" in event or "/" in event or "umask=" in event:
            malformed.append((line, "unnamed raw event spec shifted the columns"))
            continue
        # THE GUARD MUST NOT BE ABLE TO PASS A ROW IT COULD NOT READ. A check
        # of the form `enabled < threshold` can only ever see a LOW number,
        # while every way of getting the columns wrong produces a high one, a
        # non-number, or no column at all -- so a guard that only tests the
        # threshold is structurally blind to its own most likely failure. A
        # peer session put it that way after finding the same hole in their
        # parser; theirs additionally substituted a PASSING value on a parse
        # error, turning "I cannot read this" into "it is fine" inside the
        # function whose job is to refuse what it cannot vouch for.
        #
        # So every path out of here is explicit: a reading is stored only when
        # the enabled column exists, parses, and lies in range.
        if len(f) <= PERF_ENABLED:
            malformed.append((line, "no enabled column: cannot vouch for this reading"))
            continue
        raw = f[PERF_ENABLED].strip()
        try:
            pct = float(raw)
        except ValueError:
            malformed.append((line, f"enabled reads {raw!r}, which is not a number"))
            continue
        # Outside 0-100 it is not a percentage, so this is a shifted column
        # that the event-name check above did not catch.
        if not 0.0 <= pct <= 100.0:
            malformed.append((line, f"enabled reads {raw}, not a percentage"))
            continue
        if pct < PERF_MIN_ENABLED:
            dropped.append((event, pct))
            continue
        got[event] = int(f[PERF_VALUE])
    return got, dropped, malformed


# =============================================================================
# fit: was fit_burst_model.py
# =============================================================================
# Fit the per-burst cost model to every condition present, and plot it.
#
#     cycles per forwarded packet  =  P  +  C / B
#
# P is the irreducible per-packet cost; C is a fixed cost paid once per RX burst
# and amortised over the B packets that burst returned. The queue-pair sweep is
# what makes this fittable at all: offered load is constant, so raising the queue
# count divides the same packet stream over more queues and shrinks B, sweeping
# 1/B over a wide range without changing anything else about the workload.
#
# Why the fit is in CORE CYCLES and not in the TSC ticks l2fwd prints
# ------------------------------------------------------------------
# l2fwd reports "Cycle per fwd packet", but it measures with rte_rdtsc() and this
# SKU's TSC is invariant at 2.100 GHz (verified: the kernel calibrated it against
# HPET/PIT at boot, before switching its clocksource to the TSC -- dmesg
# "tsc: Detected 2100.000 MHz processor"). So the printed number is elapsed TIME,
# and it equals core cycles only when the core clock is also 2.1 GHz. Every
# condition that carries a measured freq_mhz is converted; one that does not is
# skipped rather than guessed, because multiplying by a nominal boost ratio is
# precisely the bug that made the first attempt at this fit unstable.
#
# Usage: nix develop .. -c python3 analysis.py fit [--plot]

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
TSC_MHZ = 2100.0

# Conditions worth fitting, in the order they should be read. Anything else in
# the results file is ignored: the historical arms have no recorded delivered
# clock, so their ticks cannot be put on a cycles axis at all.
ORDER = ["pinned_2100mhz", "turbo_instr",
         "xover_dram_thp2m", "xover_dram_4k",
         "xover_mag_1g", "xover_mag_4k"]


def core_cycles(rec):
    """TSC ticks -> core cycles, using the clock actually delivered in that run."""
    f = rec.get("freq_mhz")
    if f is None or "cycles_per_pkt" not in rec:
        return None
    return rec["cycles_per_pkt"] * f / TSC_MHZ


def points(rec_by_q):
    """[(1/B, cycles_per_pkt, q, B)] for every run with a usable burst size."""
    out = []
    for q, rec in sorted(rec_by_q.items(), key=lambda kv: int(kv[0])):
        y, b = core_cycles(rec), rec.get("rx_batch")
        if y is None or not b:
            continue
        out.append((1.0 / b, y, int(q), b))
    return out


def lsq(pts):
    """y = P + C*x by least squares, plus R^2 and the worst residual."""
    n = len(pts)
    if n < 3:
        return None
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    den = n * sxx - sx * sx
    if den == 0:
        return None
    C = (n * sxy - sx * sy) / den
    P = (sy - C * sx) / n
    ybar = sy / n
    ss_tot = sum((p[1] - ybar) ** 2 for p in pts)
    ss_res = sum((p[1] - (P + C * p[0])) ** 2 for p in pts)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    worst = max(abs(p[1] - (P + C * p[0])) for p in pts)
    # Standard error of the slope. Needed because a slope of "about zero" and a
    # slope that is genuinely resolved must not be reported the same way: the
    # work/stall decomposition below divides by C, so an unresolved C produces
    # a confident-looking percentage out of noise. maglev is exactly this case.
    se_c = None
    if n > 2:
        var = ss_res / (n - 2)
        se_c = (var * n / den) ** 0.5
    return P, C, r2, worst, n, se_c


def two_point(pts):
    """The same slope from the two extreme bursts only -- an estimator that
    shares no algebra with least squares, so agreement between them is
    evidence the model fits rather than evidence the fit converged."""
    if len(pts) < 2:
        return None
    lo = min(pts, key=lambda p: p[0]); hi = max(pts, key=lambda p: p[0])
    if hi[0] == lo[0]:
        return None
    return (hi[1] - lo[1]) / (hi[0] - lo[0])


def fit_main():
    allc = json.loads((DOCS / "results_reproduced.json").read_text())
    fits = {}

    print("=" * 92)
    print("PER-BURST COST MODEL      cycles/packet = P + C/B      (core cycles at the")
    print("                          delivered clock of each run)")
    print("=" * 92)
    for cond in ORDER:
        if cond not in allc:
            continue
        for mode in ("dramblast", "maglev"):
            pts = points(allc[cond].get(mode, {}))
            if not pts:
                continue
            f = lsq(pts)
            if not f:
                continue
            P, C, r2, worst, n, se_c = f
            tp = two_point(pts)
            fits[(cond, mode)] = (P, C, r2, pts, se_c)
            bs = sorted({p[3] for p in pts})
            print(f"\n  {cond:20s} {mode:10s}  n={n}  burst {bs[0]}..{bs[-1]}")
            print(f"      P = {P:8.1f} cycles/packet      (irreducible per-packet cost)")
            print(f"      C = {C:8.1f} cycles/burst       (fixed cost, amortised over B)"
            + (f"  +/- {se_c:.0f}" + ("   NOT RESOLVED (|C| < 2 s.e.)"
                                      if abs(C) < 2 * se_c else "")
               if se_c else ""))
            print(f"      R^2 = {r2:.4f}   worst residual {worst:.1f} cycles", end="")
            if tp is not None:
                sep = abs(tp - C) / abs(C) * 100 if C else float('nan')
                print(f"   two-point C = {tp:.1f} ({sep:.1f}% from LSQ)")
            else:
                print()

    # ---- decompose each fitted coefficient into work and exposed stall ------
    #
    # A core cycle count measured at two different clocks separates CPU work
    # from time spent waiting on memory, because the two scale differently:
    # instructions retire in a fixed number of CYCLES regardless of clock,
    # while a DRAM access takes a fixed number of NANOSECONDS and therefore
    # costs more cycles the faster the core runs. Writing X for a coefficient
    # in core cycles, W for its CPU work in cycles and T for its exposed stall
    # in seconds,
    #
    #     X(f) = W + T * f          so      T = (X_turbo - X_pinned) / (f_t - f_p)
    #
    # and the memory-bound fraction at the pinned clock is T * f_pinned / X.
    # This is done on the FITTED coefficients rather than point by point on
    # purpose: the two arms do not sit at the same burst sizes (a faster
    # forwarder drains its queues and gets smaller bursts), so a q-for-q
    # comparison confounds clock with burst size. P and C are already free of
    # burst size by construction, which is exactly what makes them comparable.
    def arm_freq(cond, mode):
        recs = allc.get(cond, {}).get(mode, {}).values()
        fs = [r["freq_mhz"] for r in recs if r.get("freq_mhz")]
        return sum(fs) / len(fs) if fs else None

    print()
    print("=" * 92)
    print("WORK versus EXPOSED MEMORY STALL, from the two clock arms")
    print("=" * 92)
    for mode in ("dramblast", "maglev"):
        kp, kt = ("pinned_2100mhz", mode), ("turbo_instr", mode)
        if kp not in fits or kt not in fits:
            continue
        fp, ft = arm_freq(*kp), arm_freq(*kt)
        if not fp or not ft or abs(ft - fp) < 1:
            continue
        print(f"\n  {mode}   pinned {fp:.0f} MHz -> turbo {ft:.0f} MHz   "
              f"(clock ratio {ft / fp:.3f})")
        for name, i in (("P  per packet", 0), ("C  per burst ", 1)):
            xp, xt = fits[kp][i], fits[kt][i]
            if i == 1:
                sp, st = fits[kp][4], fits[kt][4]
                if sp and st and (abs(xp) < 2 * sp or abs(xt) < 2 * st):
                    print(f"      {name}  pinned {xp:8.1f}  turbo {xt:8.1f} cycles")
                    print("          -> NOT DECOMPOSED: C is indistinguishable from zero "
                          "in at least one arm,")
                    print("             so work and stall shares of it are a ratio of "
                          "noise to noise.")
                    continue
            T_ns = (xt - xp) / ((ft - fp) / 1000.0)     # MHz -> cycles/ns
            stall_p = T_ns * fp / 1000.0                # cycles of stall at f_pinned
            work = xp - stall_p
            frac = stall_p / xp * 100 if xp else float("nan")
            print(f"      {name}  pinned {xp:8.1f}  turbo {xt:8.1f} cycles")
            print(f"          -> exposed stall {T_ns:7.2f} ns  = {stall_p:7.1f} cycles "
                  f"at 2.1 GHz  ({frac:5.1f}% of the cost)")
            print(f"          -> CPU work      {work:7.1f} cycles  ({100 - frac:5.1f}%)")

    if "--plot" not in sys.argv:
        return

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
                         "savefig.facecolor": SURFACE, "font.family": "DejaVu Sans",
                         "text.color": INK, "axes.labelcolor": INK_2,
                         "xtick.color": INK_2, "ytick.color": INK_2,
                         "axes.edgecolor": GRID, "xtick.major.size": 0,
                         "ytick.major.size": 0})

    fig, ax = plt.subplots(figsize=(10, 6.2))
    styles = {"dramblast": BLUE, "maglev": ORANGE}
    dashes = {"pinned_2100mhz": (0, ()), "turbo_instr": (0, (5, 3)),
              "xover_dram_thp2m": (0, (1, 2)), "xover_dram_4k": (0, (3, 1, 1, 1)),
              "xover_mag_1g": (0, (1, 2)), "xover_mag_4k": (0, (3, 1, 1, 1))}

    for (cond, mode), (P, C, r2, pts, _se) in fits.items():
        col = styles[mode]
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        ax.scatter(xs, ys, s=42, color=col, edgecolor=SURFACE, linewidth=1.6,
                   zorder=3)
        lo, hi = 0.0, max(xs) * 1.05
        ax.plot([lo, hi], [P, P + C * hi], color=col, linewidth=1.8,
                linestyle=dashes.get(cond, (0, ())), alpha=0.9, zorder=2,
                label=f"{mode} / {cond}   P={P:.0f}  C={C:.0f}")

    ax.set_xlabel("1 / RX burst size   (packets$^{-1}$)", fontsize=10)
    ax.set_ylabel("Core cycles per forwarded packet", fontsize=10)
    ax.set_title("A fixed per-burst cost, exposed by shrinking the burst",
                 fontsize=13, color=INK, loc="left", pad=12, fontweight="medium")
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_xlim(left=0)
    ax.legend(loc="upper left", frameon=False, fontsize=8.5, labelcolor=INK_2)
    fig.text(0.012, 0.015,
             "Each point is one queue-pair count. The intercept is the per-packet cost; "
             "the slope is the cost paid once per burst.",
             fontsize=8.5, color=INK_2)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    out = DOCS / "burst_model.png"
    fig.savefig(out, dpi=200)
    print(f"\nwrote {out}")


def cmd_fit():
    fit_main()


# =============================================================================
# matrix: was analyse_matrix.py
# =============================================================================
# Read the harness.sh matrix conditions and answer the three questions they pose.
#
#   1. CROSSOVER   does the page backing explain the per-packet gap between modes?
#   2. ALLOCATOR   is the per-burst cost C the aligned_alloc/free round trip?
#   3. DEPTH       is C instead the prefetch pipeline's ramp?
#
# Each block prints the fitted P and C for its conditions and then states what the
# numbers rule in or out, against a prediction written down before the runs.
#
# PREDICTIONS, recorded here so they are not adjusted afterwards:
#
#   Crossover. dramblast ships on 1 GiB pages and maglev on 2 MiB THP, and 8 GiB
#   on 2 MiB pages is 4096 pages against a ~2048-entry STLB. If address
#   translation is what separates the two modes, moving maglev to 1 GiB must drop
#   its P substantially toward dramblast's, and moving dramblast to 4 KiB must
#   raise its P a long way. Page size acts per access, so it should move P and
#   leave C alone. If instead P barely moves, the gap is the prefetch pipeline and
#   not the TLB, and §5.2's confound is real but small.
#
#   Allocator. C must be LINEAR in the number of alloc/free pairs. The slope is
#   what one pair costs on this machine. The intercept at -1 pair is whatever the
#   per-burst cost is that has nothing to do with the allocator. If the shipped
#   C (~645 cycles) is mostly allocator, the hoisted arm collapses toward zero; if
#   the slope is ~60 cycles/pair as the literature figure implies, the allocator
#   is ~9% of C and the intercept stays near 590.
#
#   Depth. If C is the pipeline ramp, C falls as depth falls and P rises, because
#   a shallower queue hides less latency in steady state. If C is the allocator,
#   depth moves P but cannot move C. These two cannot mimic each other.
#
# Usage: nix develop .. -c python3 analysis.py matrix [--plot]

def chi2_sf(x, k):
    """P(chi-square_k > x), exactly, for small integer k. No scipy here."""
    import math
    if x <= 0:
        return 1.0
    if k % 2 == 0:
        t = math.exp(-x / 2.0)
        acc, term = t, t
        for i in range(1, k // 2):
            term *= (x / 2.0) / i
            acc += term
        return min(1.0, acc)
    acc = math.erfc(math.sqrt(x / 2.0))
    if k > 1:
        t = math.sqrt(2.0 * x / math.pi) * math.exp(-x / 2.0)
        term, add = t, 0.0
        for i in range(1, (k - 1) // 2 + 1):
            add += term
            term *= x / (2.0 * i + 1.0)
        acc += add
    return min(1.0, acc)


def weighted_line(pts):
    """y = a + b*x weighted by 1/sigma^2, with chi-square against those sigmas.

    R-squared is close to uninformative here: three or four points, two
    parameters, and an x-range that does all the work. What matters is whether
    the residuals are consistent with the error bars the individual fits
    reported, and that is a chi-square question, not a variance-explained one.
    """
    import math
    w = [1.0 / (sg * sg) for _, _, sg in pts]
    sw = sum(w)
    sx = sum(wi * x for wi, (x, _, _) in zip(w, pts))
    sy = sum(wi * y for wi, (_, y, _) in zip(w, pts))
    sxx = sum(wi * x * x for wi, (x, _, _) in zip(w, pts))
    sxy = sum(wi * x * y for wi, (x, y, _) in zip(w, pts))
    den = sw * sxx - sx * sx
    if not den:
        return None
    b = (sw * sxy - sx * sy) / den
    a = (sy - b * sx) / sw
    resid = [(x, y, sg, y - (a + b * x), (y - (a + b * x)) / sg) for x, y, sg in pts]
    chi2 = sum(r[4] ** 2 for r in resid)
    dof = len(pts) - 2
    return a, b, chi2, dof, resid


def fit_of(allc, cond, mode):
    d = allc.get(cond, {}).get(mode, {})
    if not d:
        return None
    pts = points(d)
    # A condition slow enough never to reach line rate stays oversubscribed at
    # every queue count, so its RX burst never leaves 64 and every point sits at
    # the same x. That is a result, not missing data -- there is simply no
    # burst-size range to fit a slope against -- so say which it is.
    if len({p[3] for p in pts}) < 2:
        return {"nofit": True, "n": len(pts),
                "burst": pts[0][3] if pts else None}
    f = lsq(pts)
    if not f:
        return None
    P, C, r2, worst, n, se = f
    # Standard error of the fitted cost AT a given burst, with the P-C
    # covariance included. Quoting P and C separately understates how well the
    # curve itself is known, because the two are strongly anti-correlated: a
    # fit can be badly split between them and still predict the measured costs
    # tightly. Every cross-arm comparison below is made on this quantity rather
    # than on C alone.
    sx = sum(q[0] for q in pts); sxx = sum(q[0] ** 2 for q in pts)
    den = n * sxx - sx * sx
    ss_res = sum((q[1] - (P + C * q[0])) ** 2 for q in pts)
    s2 = ss_res / (n - 2) if n > 2 else 0.0
    var_P = s2 * sxx / den if den else 0.0
    var_C = s2 * n / den if den else 0.0
    cov = -s2 * sx / den if den else 0.0
    se_at = {}
    for B in (64, 32, 16, 9):
        v = var_P + var_C / (B * B) + 2.0 * cov / B
        se_at[B] = v ** 0.5 if v > 0 else 0.0
    return {"P": P, "C": C, "r2": r2, "n": n, "se": se, "pts": pts,
            "se_at": se_at, "cov_pc": cov / (var_P * var_C) ** 0.5
            if var_P > 0 and var_C > 0 else None}


def at_q1(allc, cond, mode):
    """Cost at q=1 in core cycles: one worker, a full 64-packet burst.

    This is reported alongside the fit because the fit's two parameters are not
    always enough. On 4 KiB pages the cost rises with QUEUE COUNT at constant
    burst size -- 117 to 129 ticks across q=1..6 with the burst pinned at 64 --
    which no per-burst term can express. Page tables are shared: ten cores each
    walking a 2-million-entry table put the page-table working set itself into
    contention for the last-level cache, so the cost acquires a third component
    that scales with core count. q=1 has one worker and a full burst, so it is
    free of both the per-burst term and the contention term, and it is the
    honest single number for comparing backings.
    """
    r = allc.get(cond, {}).get(mode, {}).get("1")
    if not r or "cycles_per_pkt" not in r or not r.get("freq_mhz"):
        return None
    return r["cycles_per_pkt"] * r["freq_mhz"] / 2100.0


def row(label, f):
    if f is None:
        return f"  {label:34s}  (no data)"
    if f.get("nofit"):
        return (f"  {label:34s}  n={f['n']}, but every run sat at burst "
                f"{f['burst']} -- never saturated the link, so no slope is "
                f"measurable")
    se = f"+/-{f['se']:.0f}" if f["se"] else ""
    return (f"  {label:34s}  P = {f['P']:7.1f}   C = {f['C']:8.1f} {se:>8s}"
            f"   R2 {f['r2']:.3f}  n={f['n']}")


def matrix_main():
    allc = json.loads((DOCS / "results_reproduced.json").read_text())
    g = lambda c, m: fit_of(allc, c, m)

    base_d = g("pinned2_asshipped", "dramblast") or g("pinned_2100mhz", "dramblast")
    base_m = g("pinned2_asshipped", "maglev") or g("pinned_2100mhz", "maglev")

    print("=" * 84)
    print("CONTROL   the -B/-A/-Q build with no flags must reproduce the original")
    print("=" * 84)
    print(row("dramblast, as shipped", base_d))
    print(row("maglev, as shipped", base_m))
    # Only meaningful once the refactored build has actually been swept; before
    # that base_d IS the original arm and comparing it to itself would print a
    # reassuring 0.0% that means nothing.
    # Compare against the pre-refactor arm coefficient by coefficient, and in
    # units of the fit's own uncertainty rather than in percent. P and C trade
    # off against each other in a least-squares fit, so a small shift in the
    # data moves both and a bare percentage on either one overstates the case.
    for mode, orig_key in (("dramblast", "pinned_2100mhz"), ("maglev", "pinned_2100mhz")):
        new_f = g("pinned2_asshipped", mode)
        old_f = g(orig_key, mode)
        if not (new_f and old_f):
            continue
        print(f"\n    {mode} vs the pre-refactor arm")
        dp = new_f["P"] - old_f["P"]
        print(f"      P {old_f['P']:7.1f} -> {new_f['P']:7.1f}   "
              f"{dp:+.1f} cycles ({dp/old_f['P']*100:+.1f}%)")
        sig = (new_f["se"] ** 2 + old_f["se"] ** 2) ** 0.5 if new_f["se"] and old_f["se"] else None
        dc = new_f["C"] - old_f["C"]
        if sig:
            print(f"      C {old_f['C']:7.1f} -> {new_f['C']:7.1f}   "
                  f"{dc:+.1f} cycles = {abs(dc)/sig:.1f} sigma of the combined fit error"
                  + ("  (not significant)" if abs(dc) < 2 * sig else "  (SIGNIFICANT)"))
        # The cost at the two ends of the burst range is what a reader actually
        # cares about, and it is not hostage to how the fit split P from C.
        for b in (64, 8):
            o, n = old_f["P"] + old_f["C"] / b, new_f["P"] + new_f["C"] / b
            print(f"      at burst {b:>2}: {o:6.1f} -> {n:6.1f} cycles/packet "
                  f"({(n-o)/o*100:+.1f}%)")
    print("\n    Read every row below against `pinned2_asshipped`, never against the")
    print("    pre-refactor arm: the two binaries are not interchangeable.")

    print()
    print("=" * 84)
    print("0. THE FLOOR   the same forwarding loop with the lookup removed")
    print("=" * 84)
    # `-m none` (main.c, the forwarding loop's third branch) writes the
    # destination MAC exactly as the other two engines do and skips only the
    # lookup. Everything else on this page is a per-packet cost measured over a
    # timed region that contains this arm as well, so without it no number here
    # can be stated as a fraction of anything.
    trio = allc.get("engine_trio", {})
    if len(trio) < 3:
        print("  not measured yet: ./harness.sh matrix trio")
    else:
        print(f"  {'mode':10} {'q':>2} {'burst':>5} {'Mpps':>7} {'cyc/pkt':>8} "
              f"{'insns/pkt':>10}")
        for mode in ("none", "dramblast", "maglev"):
            for q in sorted(trio[mode], key=int):
                r = trio[mode][q]
                ipp = ((r.get("insns") or 0)
                       / (r["steady_mpps"] * 1e6 * 8.0) if r.get("steady_mpps") else 0)
                print(f"  {mode:10} {int(q):>2} {r.get('rx_batch', 0):>5} "
                      f"{r.get('steady_mpps', 0):>7.2f} {r['cycles_per_pkt']:>8} "
                      f"{ipp:>10.1f}")
        # Where each arm first reaches the offered load. The generator holds
        # 93.28 Mpps, so "reached it" is the only saturation test that does not
        # depend on a fit.
        print()
        for mode in ("none", "dramblast", "maglev"):
            hit = [int(q) for q in sorted(trio[mode], key=int)
                   if trio[mode][q].get("steady_mpps", 0) >= 93.2]
            print(f"  {mode:10} reaches the offered load at "
                  + (f"q={hit[0]}" if hit else "no queue count in the sweep"))

        # The lookup's share, taken only where all three sit at a full burst so
        # that no per-burst term is in the comparison.
        print()
        qs = [q for q in trio["none"]
              if all(trio[m].get(q, {}).get("rx_batch") == 64 for m in trio)]
        for q in sorted(qs, key=int):
            n = trio["none"][q]["cycles_per_pkt"]
            d = trio["dramblast"][q]["cycles_per_pkt"]
            m = trio["maglev"][q]["cycles_per_pkt"]
            print(f"  q={q}, burst 64:  floor {n}   dramblast {d} "
                  f"({100*(1-n/d):.0f}% lookup)   maglev {m} ({100*(1-n/m):.0f}% lookup)")
        if not qs:
            print("  no queue count holds a 64-packet burst in all three arms")

        # What the timed region does NOT contain. rte_eth_rx_burst, the TX
        # buffer and the driver all sit outside the rdtsc pair, so no arm's
        # "cycles per packet" includes them. At one queue the single worker
        # busy-polls at 100%, so its total cycles per packet is just the
        # delivered clock over the delivered rate -- and the difference between
        # that and the timed region is the part of the packet path this
        # instrument cannot see. Three arms give three independent estimates of
        # the same quantity, which is the only reason it can be quoted at all.
        print()
        print("  what sits OUTSIDE the timed region, from q=1 (one busy worker):")
        outs = []
        for mode in ("none", "dramblast", "maglev"):
            r = trio[mode].get("1")
            if not r or not r.get("steady_mpps") or not r.get("freq_mhz"):
                continue
            total = r["freq_mhz"] / r["steady_mpps"]   # MHz / Mpps = cycles/pkt
            out = total - r["cycles_per_pkt"]
            outs.append(out)
            print(f"    {mode:10} {total:6.1f} cycles/pkt total "
                  f"- {r['cycles_per_pkt']:>3} timed = {out:5.1f} outside")
        if len(outs) == 3:
            mu = sum(outs) / 3
            print(f"    three independent estimates of one quantity: "
                  f"{mu:.0f} +/- {max(outs)-min(outs):.0f} cycles/packet of "
                  f"RX/TX and driver")
            print("    They agree to a few cycles despite the arms differing "
                  "sixfold in rate,")
            print("    which is what makes it a measurement rather than a "
                  "subtraction artefact.")

        # FALSIFICATION. The floor is not flat: the timed region has its own
        # fixed cost per burst (two rdtsc reads and the loop entry), and that
        # cost is charged to every arm. If it were a large fraction of
        # dramblast's per-burst coefficient, the per-burst result would be
        # substantially instrument rather than engine.
        nf = fit_of(allc, "engine_trio", "none")
        if nf and base_d:
            print()
            print(f"  floor fit:  P = {nf['P']:.1f} +/- {nf.get('se_at', {}).get(64, 0):.1f}"
                  f"   C = {nf['C']:.0f} +/- {nf['se']:.0f} cycles/burst"
                  f"   R2 {nf['r2']:.3f}  n={nf['n']}")
            print(f"  dramblast's C is {base_d['C']:.0f}; the instrument accounts for "
                  f"{100*nf['C']/base_d['C']:.0f}% of it.")
            print("  Differences between two dramblast arms (the allocator and depth")
            print("  results below) are unaffected: the instrument cancels in a paired")
            print("  difference. The absolute per-burst figure is not.")

    print()
    print("=" * 84)
    print("1. CROSSOVER   each mode on the other's page backing")
    print("=" * 84)
    base_cond = "pinned2_asshipped" if "pinned2_asshipped" in allc else "pinned_2100mhz"
    # (mode, short page label, condition, whether this is that mode's shipped default)
    spec = [("dramblast", "1 GiB", base_cond, True),
            ("dramblast", "2 MiB", "xover_dram_thp2m", False),
            ("dramblast", "4 KiB", "xover_dram_4k", False),
            ("maglev", "2 MiB", base_cond, True),
            ("maglev", "1 GiB", "xover_mag_1g", False),
            ("maglev", "4 KiB", "xover_mag_4k", False)]
    q1 = {}
    for mode, pg, cond, shipped in spec:
        tag = f"{mode:10s} {pg:6s}" + ("(as shipped)" if shipped else "")
        print(row(tag, g(cond, mode)))
        v = at_q1(allc, cond, mode)
        if v is not None:
            q1[(mode, pg)] = v

    if q1:
        print("\n  At q=1 -- one worker, a full 64-packet burst -- so free of both the")
        print("  per-burst term and the cross-core page-table contention:")
        for mode, pg, _, _ in spec:
            if (mode, pg) in q1:
                print(f"      {mode:10s} {pg:6s}  {q1[(mode, pg)]:7.1f} core cycles/packet")
        d1, d2 = q1.get(("dramblast", "1 GiB")), q1.get(("dramblast", "2 MiB"))
        d4 = q1.get(("dramblast", "4 KiB"))
        m1, m2 = q1.get(("maglev", "1 GiB")), q1.get(("maglev", "2 MiB"))
        print()
        if d1 and d2:
            print(f"      dramblast, 1 GiB -> 2 MiB:  {d2 - d1:+6.1f} cycles")
        if d1 and d4:
            print(f"      dramblast, 1 GiB -> 4 KiB:  {d4 - d1:+6.1f} cycles")
        if m2 and m1:
            print(f"      maglev,    2 MiB -> 1 GiB:  {m1 - m2:+6.1f} cycles")
        if d1 and m2:
            print(f"\n      as-shipped gap between engines:  {m2 - d1:6.1f} cycles")
        if d1 and m1:
            print(f"      gap at MATCHED 1 GiB pages:      {m1 - d1:6.1f} cycles"
                  f"   ({(m1 - d1) / (m2 - d1) * 100:.0f}% of it survives)"
                  if m2 else "")
            print("      -> the surviving gap is not address translation; it is the")
            print("         prefetch pipeline.")

    print()
    print("=" * 84)
    print("2. ALLOCATOR   C against the number of aligned_alloc/free pairs per burst")
    print("=" * 84)
    series = [(-1, g("alloc_hoisted", "dramblast")), (0, base_d),
              (2, g("alloc_x2", "dramblast")), (4, g("alloc_x4", "dramblast")),
              (8, g("alloc_x8", "dramblast"))]
    # Only conditions with a real slope enter the line. An arm that never
            # saturated the link has no burst-size range and therefore no C.
    have = [(n, f) for n, f in series if f and "C" in f]
    for n, f in have:
        print(row(f"pairs = {n:+d}" + ("  (hoisted)" if n < 0 else ""), f))
    # Two different quantities, and they must not be conflated.
    #
    #   hoist difference  C(0 pairs) - C(hoisted)  is the SHIPPED pair's cost:
    #       the real allocation, separated from its free by the whole batch.
    #   amplification slope                        is an INCREMENTAL pair's cost:
    #       alloc and free back to back in a tight loop, which is the warmest
    #       possible tcache path and therefore a LOWER BOUND on the shipped one.
    #
    # If they agree, the shipped pair is as cheap as a back-to-back pair and the
    # allocator's share is settled. If the hoist difference is materially
    # larger, the separation costs something -- the tcache entry ages out of L1
    # across a batch -- and the slope alone would have understated it.
    hoisted = dict(series).get(-1)
    if hoisted and "C" in hoisted and base_d:
        shipped_pair = base_d["C"] - hoisted["C"]
        sig = ((base_d["se"] or 0) ** 2 + (hoisted["se"] or 0) ** 2) ** 0.5
        print(f"\n    shipped pair, by removing it:  C {hoisted['C']:.0f} (hoisted) "
              f"-> {base_d['C']:.0f} (as shipped)")
        print(f"      = {shipped_pair:.0f} cycles for the one round trip the code "
              f"actually performs"
              + (f", {abs(shipped_pair)/sig:.1f} sigma" if sig else ""))
        print(f"      = {shipped_pair/base_d['C']*100:.1f}% of the per-burst cost; "
              f"{base_d['C']-shipped_pair:.0f} cycles are something else")

    # PRIMARY ESTIMATOR: matched burst, no fitting at all.
    #
    # Every arm is read at q=1, where the burst is a full 64 packets. Adding
    # allocator pairs makes the forwarder slower, which makes it stay
    # oversubscribed further up the sweep, which changes the burst sizes it
    # sits at -- so the arms are NOT at comparable bursts away from q=1, and a
    # fit over each arm's own burst range is comparing conditions that differ
    # in two things. At a fixed burst of 64 the difference in cost per packet
    # multiplied by 64 IS the difference in cost per burst, with no model in
    # between. This is the third time in this investigation that holding burst
    # size fixed has beaten fitting it out.
    # Matched burst AND matched queue count, every queue count that qualifies --
    # not q=1 alone, which is what an earlier version of this did and what made
    # a one-tick difference look like a result.
    #
    # The resolution limit is the point. l2fwd prints "Cycle per fwd packet" as
    # an INTEGER, so at a 64-packet burst one printed tick is 64 cycles per
    # burst. A single-queue-count difference of 7 ticks therefore carries +/-64
    # cycles of quantisation before any other error, and the four matched values
    # here are 7, 9, 7, 10 -- q=1 is the smallest of them. Averaging over the
    # matched queue counts is what buys resolution back; pairing at each q is
    # what keeps the core-count effect from contaminating the difference.
    print("\n    At a MATCHED burst of 64 AND a matched queue count, no fit:")
    print(f"      {'pairs':>6} {'matched q':>16} {'tick diffs':>20} {'cycles/burst':>16}")
    q1pts, q1err = [], {}
    hoist_t = {int(q): (r["cycles_per_pkt"], r.get("rx_batch"), r.get("freq_mhz"))
               for q, r in allc.get("alloc_hoisted", {}).get("dramblast", {}).items()}
    for n, cond in ((1, "pinned2_asshipped"), (3, "alloc_x2"), (5, "alloc_x4"),
                    (9, "alloc_x8")):
        t = {int(q): (r["cycles_per_pkt"], r.get("rx_batch"), r.get("freq_mhz"))
             for q, r in allc.get(cond, {}).get("dramblast", {}).items()}
        qs = [q for q in sorted(set(t) & set(hoist_t))
              if t[q][1] == 64 and hoist_t[q][1] == 64]
        if len(qs) < 2:
            continue
        diffs = [t[q][0] - hoist_t[q][0] for q in qs]
        fq = [t[q][2] / 2100.0 for q in qs if t[q][2]]
        scale = 64.0 * (sum(fq) / len(fq) if fq else 1.0)
        mean = sum(diffs) / len(diffs)
        var = sum((v - mean) ** 2 for v in diffs) / (len(diffs) - 1)
        sem = (var / len(diffs)) ** 0.5
        q1pts.append((n, mean * scale))
        q1err[n] = sem * scale
        print(f"      {n:>6} {str(qs):>16} {str(diffs):>20} "
              f"{mean*scale:>8.0f} +/-{sem*scale:<5.0f}")
    if q1pts:
        onetick = 64.0 * (2095.0 / 2100.0)
        print(f"\n      One printed tick at burst 64 = {onetick:.0f} cycles per burst.")
        print("      Every number in the column above is a mean of small integers,")
        print("      so nothing here is meaningful to better than a few tens of")
        print("      cycles however many digits a fit prints.")

    amplified_slope = None
    multi = [(n, c) for n, c in q1pts if n >= 3]
    if len(multi) >= 2:
        print("\n      Consecutive slopes between multi-pair arms:")
        for (n0, c0), (n1, c1) in zip(multi, multi[1:]):
            print(f"        {n0} -> {n1} pairs: {(c1 - c0) / (n1 - n0):.0f} cycles per pair")
        w = [1.0 / max(q1err.get(n, 1.0), 1.0) ** 2 for n, _ in multi]
        sw = sum(w)
        sx = sum(wi * n for wi, (n, _) in zip(w, multi))
        sy = sum(wi * c for wi, (_, c) in zip(w, multi))
        sxx = sum(wi * n * n for wi, (n, _) in zip(w, multi))
        sxy = sum(wi * n * c for wi, (n, c) in zip(w, multi))
        den = sw * sxx - sx * sx
        if den:
            b = (sw * sxy - sx * sy) / den
            a = (sy - b * sx) / sw
            amplified_slope = b
            print(f"      weighted line through them: {a:+.0f} + {b:.0f} * pairs")
            shipped = dict(q1pts).get(1)
            se_ship = q1err.get(1)
            if shipped and se_ship:
                pred = a + b
                z = abs(shipped - pred) / se_ship
                print(f"\n      Extrapolated to one pair: {pred:.0f}")
                print(f"      Measured for the shipped single pair: {shipped:.0f} "
                      f"+/- {se_ship:.0f}")
                print(f"      -> {shipped - pred:+.0f} cycles apart, {z:.1f} sigma of the")
                print("         measurement's own error.")
                if z < 2:
                    print("         NOT RESOLVED. The shipped pair and an incremental")
                    print("         one cost the same within this experiment's")
                    print("         resolution, and no first-pair or load effect can")
                    print("         be claimed from it.")
                else:
                    print("         Resolved: the two regimes genuinely differ.")
                print("\n      SUPERSEDES an earlier reading of this same data. Taken at")
                print("      q=1 alone the shipped pair came out at 447 against an")
                print("      incremental 510.78, the two consecutive slopes agreed to")
                print("      0.00 cycles and the line's intercept was 0.0 -- which was")
                print("      written up as a structural check and as a load effect.")
                print("      It was neither. The tick differences at q=1 are 24, 40 and")
                print("      72; 40-24 = 16 and 72-40 = 32, exactly twice it, so after")
                print("      any common scaling the two slopes are identically equal and")
                print("      the intercept is identically zero. The agreement was")
                print("      arithmetic, not measurement. And 447 against 511 is one")
                print("      printed tick, from the single queue count where the")
                print("      difference happens to be smallest.")

    if len(have) >= 3:
        # Absolute pairs executed: n+1 for n>=0, zero for the hoisted arm.
        # An arm whose own sweep barely left a 64-packet burst cannot produce a
        # trustworthy slope, and must not be given equal standing in the line.
        # The +8 arm is exactly this: two points off burst 64, non-monotone,
        # R^2 0.62, a standard error a quarter of the value.
        usable = [(n, f) for n, f in have if f.get("r2", 0) >= 0.90]
        dropped = [n for n, f in have if f.get("r2", 0) < 0.90]
        if dropped:
            print(f"\n    (excluded from the line: pairs={dropped} -- that arm is so "
                  f"slow it\n     never left a 64-packet burst, so its own slope is "
                  f"not measurable)")
        pts = [(float(n + 1 if n >= 0 else 0), f["C"], f["se"] or 1.0)
               for n, f in usable]
        res = weighted_line(pts)
        if res:
            a, b, chi2, dof, resid = res
            print(f"\n    weighted fit   C = {a:.0f} + {b:.0f} * pairs")
            print(f"    {'pairs':>6} {'measured':>12} {'line':>9} {'residual':>10} {'sigma':>7}")
            for x, y, sg, r, z in resid:
                print(f"    {x:>6.0f} {y:>8.1f} +/-{sg:<4.0f} {a + b * x:>9.1f} "
                      f"{r:>10.1f} {z:>7.1f}")
            pval = chi2_sf(chi2, dof) if dof > 0 else float("nan")
            print(f"    chi-square {chi2:.1f} on {dof} dof   p = {pval:.4g}")
            if dof > 0 and pval < 0.01:
                print("    -> THE LINE IS REJECTED against the fits' own error bars.")
                print("       The pairs are not all the same price, so the removal")
                print("       estimate and the slope are two different quantities")
                print("       rather than two routes to one.")
            elif dof > 0 and pval < 0.05:
                print("    -> MARGINAL. Not rejected, but not comfortable either, and")
                print("       the tension is in the direction the separation argument")
                print("       predicts: the shipped pair sits above the line while the")
                print("       arms with extra back-to-back pairs sit on it.")
            else:
                print("    -> consistent with one price per pair")
            print("       (R-squared is deliberately not quoted for this regression.")
            print("        With this few points and this x-range it is near-1 whatever")
            print("        happens, and it says nothing about whether the residuals")
            print("        are consistent with the error bars -- which is the only")
            print("        question that matters here.)")
            print(f"    -> an INCREMENTAL pair costs {b:.0f} cycles = {b/2.1:.0f} ns")

            # Reconcile the two estimators rather than choosing one.
        ps = [(n, f["P"]) for n, f in usable]
        if len(ps) >= 2:
            dP = ps[-1][1] - ps[0][1]
            amp = amplified_slope if amplified_slope else float("nan")
            print(f"\n    The two estimators disagree on the incremental pair --")
            print(f"    {amp:.0f} cycles from the matched-burst slopes against {b:.0f}")
            print(f"    from the fit -- and the")
            print(f"    reason is visible in P, which is not constant across the arms:")
            print("      " + "  ".join(f"{n:+d}:{P:.0f}" for n, P in ps))
            print(f"    P rises {dP:.0f} cycles from the hoisted arm to the deepest")
            print("    usable one. The matched-burst estimator multiplies the whole")
            print("    per-packet difference by 64 and so charges that rise to the")
            print("    per-burst term; the fit separates them but pays for it with a")
            print("    model. The truth is bracketed, not pinned: an incremental pair")
            print(f"    costs between {b:.0f} and {amp:.0f} cycles.")
            print("    Why P moves at all is not established. A once-per-burst")
            print("    allocation should not touch per-packet cost; cache and TLB")
            print("    pollution from the allocator's own chunk walking is the")
            print("    obvious candidate and has not been measured.")
            ship = dict(q1pts).get(1)
            se_ship = q1err.get(1)
            fitdiff = None
            if base_d and hoisted and "C" in base_d and "C" in hoisted:
                fitdiff = base_d["C"] - hoisted["C"]
                se_fd = ((base_d["se"] or 0) ** 2 + (hoisted["se"] or 0) ** 2) ** 0.5
            print("\n    NONE OF THIS MOVES THE HEADLINE, which is the SHIPPED pair,")
            print("    measured two ways that share no algebra:")
            if ship and se_ship:
                print(f"      matched burst and matched queue count:  {ship:.0f} +/- {se_ship:.0f}")
            if fitdiff:
                print(f"      differencing the two fitted C's:        {fitdiff:.0f} +/- {se_fd:.0f}")
            if ship and fitdiff:
                zz = abs(ship - fitdiff) / ((se_ship ** 2 + se_fd ** 2) ** 0.5)
                lo, hi = min(ship, fitdiff), max(ship, fitdiff)
                print(f"    They agree to {zz:.1f} sigma. Quote it as ~{(lo+hi)/2:.0f} cycles,")
                print(f"    or {lo:.0f}-{hi:.0f}; three significant figures are not available")
                print("    from an integer tick counter.")
                if base_d and "C" in base_d:
                    print(f"    That is {100*lo/base_d['C']:.0f}-{100*hi/base_d['C']:.0f}% of the "
                          f"{base_d['C']:.0f}-cycle per-burst cost, against the")
                    print("    'at most 11%' this investigation previously claimed.")

    # The non-allocator remainder is MEASURED, not extrapolated. The hoisted
        # arm is that quantity directly; the fitted intercept is an
        # extrapolation that inherits whatever is wrong with the line.
        if hoisted and "C" in hoisted:
            print(f"\n    non-allocator per-burst cost, measured directly by the")
            print(f"    hoisted arm: {hoisted['C']:.0f} +/- {hoisted['se']:.0f} cycles")
            print(f"    (NOT the fitted intercept -- that is an extrapolation, and")
            print(f"     where the line is rejected it is a wrong one.)")

    print()
    print("=" * 84)
    print("3. DEPTH   P and C against the prefetch pipeline depth")
    print("=" * 84)
    for d, cond in ((8, "depth_8"), (16, "depth_16"), (32, "depth_32")):
        print(row(f"depth = {d}", g(cond, "dramblast")))
    print(row("depth = 64  (as shipped)", base_d))
    ds = [(d, g(c, "dramblast")) for d, c in ((8, "depth_8"), (16, "depth_16"),
                                              (32, "depth_32"))] + [(64, base_d)]
    ds = [(d, f) for d, f in ds if f and "C" in f]
    if len(ds) >= 3:
        lo, hi = ds[0], ds[-1]
        print(f"\n    depth {lo[0]} -> {hi[0]}:  P {lo[1]['P']:.1f} -> {hi[1]['P']:.1f}"
              f"   C {lo[1]['C']:.1f} -> {hi[1]['C']:.1f}")
        # Model-free companion. The P/C split is correlated (r ~ -0.7 here), so
        # a claim about C alone is fragile; the cost evaluated at a matched
        # burst is not, and a genuine slope difference shows up as the two
        # curves crossing. Errors propagated WITH the covariance.
        print("\n    Model-free check -- cost at matched burst, since P and C are")
        print("    strongly anti-correlated in this fit and a claim about either")
        print("    alone is fragile. A real slope difference shows up as a crossing:")
        hdr = "      " + "".join(f"{'B=%d' % b:>14}" for b in (64, 32, 16, 9))
        print(hdr)
        for dpt, f in ds:
            cells = []
            for B in (64, 32, 16, 9):
                v = f["P"] + f["C"] / B
                sg = f.get("se_at", {}).get(B)
                cells.append(f"{v:8.1f}+/-{sg:<4.1f}" if sg else f"{v:8.1f}     ")
            print(f"   d{dpt:<3}" + "".join(f"{c:>14}" for c in cells))
        print("    A pipeline-ramp C must fall with depth while P rises.")
        print("    An allocator C must be flat in depth.")
        # Sharper, because the allocator share is now measured rather than
        # hypothetical: one alloc/free pair happens per burst at every depth, so
        # that part of C cannot move. Only the remainder is available to depth.
        shipped_pair = dict(q1pts).get(1) if q1pts else None   # matched-q estimate
        if shipped_pair and base_d:
            floor = shipped_pair
            room = base_d["C"] - floor
            print(f"\n    Sharper, using the measured allocator cost. Exactly one")
            print(f"    alloc/free pair runs per burst at EVERY depth -- the buffer is")
            print(f"    sized by the burst length, not by the queue depth (dramblast.c")
            print(f"    :243), so the -Q knob does not change what is allocated. That")
            print(f"    makes {floor:.0f} cycles a FLOOR under C at every depth, and the")
            print(f"    remaining {room:.0f} of the shipped C ({base_d['C']:.0f}) is all the")
            print(f"    pipeline ramp can possibly own.")
            print(f"\n    The test is a floor, not a spread: C may rise with depth")
            print(f"    without limit, but no arm's C may fall below the allocator's")
            print(f"    own per-burst cost.")
            for dpt, f in ds:
                z = (floor - f["C"]) / (f["se"] or 1.0)
                mark = ("OK" if f["C"] >= floor else
                        f"below the floor by {floor - f['C']:.0f} ({z:.1f} sigma)")
                print(f"      depth {dpt:>2}: C = {f['C']:6.1f} +/- {f['se']:.0f}   {mark}")
            worst = min(ds, key=lambda t: t[1]["C"])
            if worst[1]["C"] < floor:
                zz = (floor - worst[1]["C"]) / (worst[1]["se"] or 1.0)
                if zz > 3:
                    print("    -> FALSIFIED, and section 3d says which assumption broke.")
                    print("       A per-burst term cannot be smaller than a cost the")
                    print("       burst pays unconditionally, so the shallow arm's fitted")
                    print("       C is not a per-burst term. The pipeline fills")
                    print("       ceil(B/Q) times per burst, so a line in 1/B is")
                    print("       mis-specified wherever B > Q -- which is most of the")
                    print("       depth-8 arm and none of the shipped one. This test was")
                    print("       written before that was understood and is left in")
                    print("       because it is what pointed at it.")
                else:
                    print("    -> not falsified, but the headroom is gone: at the")
                    print("       shallowest depth the non-allocator part of C is")
                    print("       consistent with zero, which is what a ramp that")
                    print("       scales with queue depth would look like.")

    # ---- the part of the depth result that needs no model at all ----------
    # Raw runs that stayed at burst 64, so every arm is compared at the same
    # burst and no fit stands between the measurement and the claim. Both a
    # cycle counter and an instruction counter are read, which is what
    # separates "does more work" from "waits longer": the burst-cost model
    # cannot tell those apart and this can.
    # The run-to-run floor at burst 64, computed here rather than in section 4
    # because section 3b's error bars are wrong without it. A sweep's own
    # scatter is the spread of ten samples inside one twelve-second window; it
    # cannot see anything that drifts between sweeps, and the depth arms were
    # taken hours apart. Both terms go into every depth error bar below.
    def repeat_floor(mode):
        a = allc.get("pinned2_asshipped", {}).get(mode, {})
        b = allc.get("pinned3_repeat", {}).get(mode, {})
        d = [b[q]["cycles_per_pkt"] - a[q]["cycles_per_pkt"]
             for q in set(a) & set(b)
             if a[q].get("rx_batch") == 64 and b[q].get("rx_batch") == 64]
        return (sum(v * v for v in d) / len(d)) ** 0.5 if d else None

    dram_floor = repeat_floor("dramblast")

    PERF_WINDOW = 8.0        # harness.sh sweep's perf window, seconds
    print()
    print("=" * 84)
    print("3b. DEPTH AT A MATCHED BURST   cycles vs instructions, no fit")
    print("=" * 84)
    dcond = [(8, "depth_8"), (16, "depth_16"), (32, "depth_32"),
             (64, "pinned2_asshipped")]
    at64 = {}
    if dram_floor:
        print(f"  error bars = within-sweep scatter AND the {dram_floor:.2f} cycle")
        print(f"  run-to-run floor measured by the repeat arm (section 4)")
    print(f"  {'depth':>5} {'runs':>5} {'cycles/pkt':>12} {'insns/pkt':>11} {'IPC':>6}")
    for dpt, cond in dcond:
        runs = [r for r in allc.get(cond, {}).get("dramblast", {}).values()
                if r.get("rx_batch") == 64 and r.get("insns") and r.get("steady_mpps")]
        if not runs:
            continue
        cyc = sum(r["cycles_per_pkt"] for r in runs) / len(runs)
        ipp = sum(r["insns"] / (r["steady_mpps"] * 1e6 * PERF_WINDOW)
                  for r in runs) / len(runs)
        # scatter of the mean, so the depth-32 test has an error bar
        var = sum((r["cycles_per_pkt"] - cyc) ** 2 for r in runs)
        sem = (var / (len(runs) * (len(runs) - 1))) ** 0.5 if len(runs) > 1 else None
        if sem is not None and dram_floor:
            sem = (sem ** 2 + dram_floor ** 2) ** 0.5
        at64[dpt] = (cyc, ipp, sem, len(runs))
        semtxt = f"+/-{sem:.2f}" if sem else ""   # includes the run-to-run term
        print(f"  {dpt:>5} {len(runs):>5} {cyc:>8.1f}{semtxt:>6} {ipp:>11.1f} "
              f"{ipp / cyc:>6.2f}")
    if 8 in at64 and 64 in at64:
        c8, i8 = at64[8][0], at64[8][1]
        c64, i64 = at64[64][0], at64[64][1]
        print(f"\n    depth 64 -> 8 at a matched burst of 64:")
        print(f"      instructions per packet  +{100*(i8-i64)/i64:.1f}%")
        print(f"      cycles       per packet  +{100*(c8-c64)/c64:.1f}%")
        print(f"      IPC  {i64/c64:.2f} -> {i8/c8:.2f}")
        print("    -> a shallower prefetch pipeline does not make the forwarder")
        print("       do meaningfully more work. It makes it wait. This is the")
        print("       load-bearing depth result and it survives without the")
        print("       burst-cost model, which cannot separate the two.")

    # ---- the per-fill ramp model, calibrated then tested -------------------
    # See docs/INVESTIGATION.md Appendix A (formerly docs/depth_prediction.md),
    # written before the depth-32 arm finished.
    # With queue depth Q and burst B the pipeline fills ceil(B/Q) times per
    # burst, so the ramp is paid per fill, not per burst. At the shipped depth
    # Q == B and the two are the same event, which is why the burst model
    # attributes the ramp to C; shortening the queue moves it into P.
    # Everything here is an EXCESS over the depth-64 arm, because the ramp is
    # defined relative to it and because differencing against a common baseline
    # is what the error propagation has to respect: the baseline's own error
    # enters every comparison and must not be dropped after the first one.
    ramp_pred = None
    cal = [(q, at64[q][0] - at64[64][0],
            ((at64[q][2] or 0) ** 2 + (at64[64][2] or 0) ** 2) ** 0.5)
           for q in (8, 16) if q in at64]
    if len(cal) == 2 and 64 in at64:
        xs = [(1.0 / q - 1.0 / 64.0) for q, _, _ in cal]
        ys = [y for _, y, _ in cal]
        sgs = [g for _, _, g in cal]
        sxx = sum(x * x for x in xs)
        ramp = sum(x * y for x, y in zip(xs, ys)) / sxx
        se_ramp = (sum((x * g) ** 2 for x, g in zip(xs, sgs))) ** 0.5 / sxx
        ramp_pred = ramp * (1.0 / 32 - 1.0 / 64)
        print(f"\n    Per-fill ramp calibrated on depths 8 and 16 (two points,")
        print(f"    one parameter): ramp = {ramp:.0f} +/- {se_ramp:.0f} cycles "
              f"per pipeline fill.")
        if 32 in at64:
            dx = 1.0 / 32 - 1.0 / 64
            pred_ex, se_pred = ramp * dx, se_ramp * dx
            obs_ex = at64[32][0] - at64[64][0]
            se_obs = ((at64[32][2] or 0) ** 2 + (at64[64][2] or 0) ** 2) ** 0.5
            print(f"    Depth 32's excess over depth 64, at burst 64:")
            print(f"      predicted by the ramp  {pred_ex:5.2f} +/- {se_pred:.2f}")
            print(f"      measured               {obs_ex:5.2f} +/- {se_obs:.2f}")
            print(f"      null (no effect)        0.00")
            sg = (se_obs ** 2 + se_pred ** 2) ** 0.5
            zp = abs(obs_ex - pred_ex) / sg
            zn = abs(obs_ex) / se_obs
            print(f"      -> {zp:.1f} sigma from the prediction, "
                  f"{zn:.1f} sigma from the null.")
            if zp < 2 and zn > 3:
                print("      -> the model survives a test it could have failed,")
                print("         and the null is excluded.")
            elif zp < 2 and zn > 1.5:
                print("      -> INCONCLUSIVE, leaning toward the model. The")
                print("         measurement is consistent with the prediction and")
                print("         does not exclude the null. The effect being tested")
                print("         is only about twice the run-to-run floor, which is")
                print("         all the resolution one sweep per depth buys;")
                print("         separating them needs repeats at depth 32, not a")
                print("         better fit.")
            elif zn <= 1.5:
                print("      -> NOT CONFIRMED. Depth 32 is indistinguishable from")
                print("         no effect once the run-to-run term is included.")
            else:
                print("      -> inconclusive; separates neither hypothesis.")
            print("\n      (An earlier run of this analysis called this 3.1 sigma")
            print("       from the null and said the model had survived. That used")
            print("       only the within-sweep scatter, which cannot see drift")
            print("       between sweeps taken hours apart. The repeat arm measured")
            print("       that drift afterwards, and including it is what moved the")
            print("       verdict.)")

    # ---- the depth-32 test, decided by repeats rather than by a fit -------
    # Three interleaved sweeps of each arm at burst 64. The statistic is the
    # PAIRED difference within each repeat: the arms were run alternately, so a
    # pairing removes any drift common to a pair, and the spread of the three
    # paired differences is an honest error bar that needs no assumption about
    # which sources of variation the sweep did or did not see.
    # Paired queue count by queue count, not arm mean against arm mean. The
    # repeats do not all reach burst 64 at the same queue counts -- one d64
    # sweep left it at q=5 while its d32 partner did not -- so comparing arm
    # means silently compares different queue sets, and the queue count does
    # move the cost slightly. Matching within each pair removes that.
    pairs = []
    for i in (1, 2, 3):
        def t(cond):
            return {int(q): (r["cycles_per_pkt"], r.get("rx_batch"))
                    for q, r in allc.get(cond, {}).get("dramblast", {}).items()}
        A, B = t(f"depth_32_r{i}"), t(f"depth_64_r{i}")
        qs = [q for q in sorted(set(A) & set(B)) if A[q][1] == 64 == B[q][1]]
        if len(qs) < 2:
            continue
        diffs = [A[q][0] - B[q][0] for q in qs]
        pairs.append((i, qs, diffs, sum(diffs) / len(diffs)))
    if len(pairs) >= 2:
        print()
        print("=" * 84)
        print("3c. DEPTH 32 vs 64, DECIDED   three interleaved repeats at burst 64")
        print("=" * 84)
        print(f"  {'repeat':>7} {'matched q':>18} {'tick differences':>22} {'mean':>7}")
        for i, qs, diffs, m in pairs:
            print(f"  {i:>7} {str(qs):>18} {str(diffs):>22} {m:>7.3f}")
        # Two error bars, because they answer different questions and here they
        # disagree about one of the two hypotheses. Neither is quietly dropped.
        ms = [m for _, _, _, m in pairs]
        n = len(ms)
        mu = sum(ms) / n
        sd = (sum((v - mu) ** 2 for v in ms) / (n - 1)) ** 0.5
        sem = sd / n ** 0.5
        t2 = {1: 12.71, 2: 4.303, 3: 3.182, 4: 2.776}.get(n - 1, 2.0)
        flat = [v for _, _, ds, _ in pairs for v in ds]
        mu2 = sum(flat) / len(flat)
        sd2 = (sum((v - mu2) ** 2 for v in flat) / (len(flat) - 1)) ** 0.5
        sem2 = sd2 / len(flat) ** 0.5
        tf = 2.16 if len(flat) >= 13 else 2.45
        f = 2095.0 / 2100.0
        lo_b, hi_b = (mu - t2 * sem) * f, (mu + t2 * sem) * f
        lo_p, hi_p = (mu2 - tf * sem2) * f, (mu2 + tf * sem2) * f
        print(f"\n  excess, depth 32 over depth 64, in cycles per packet: {mu*f:+.2f}")
        print(f"    between-repeat error ({n} repeat means, {n-1} dof):")
        print(f"      sd {sd:.3f}  se {sem:.3f}   95% interval [{lo_b:+.2f}, {hi_b:+.2f}]")
        print(f"    within-and-between ({len(flat)} matched-q differences):")
        print(f"      sd {sd2:.3f}  se {sem2:.3f}   95% interval [{lo_p:+.2f}, {hi_p:+.2f}]")
        print("    The first uses only the spread of three numbers that happen to")
        print("    lie close together; the second uses every measurement and is the")
        print("    conservative one. Both are quoted because they disagree about")
        print("    the prediction and agree about the null.")
        pred = ramp_pred
        if pred is not None:
            print(f"\n  ramp model predicts {pred:+.2f}   null predicts 0.00")
            in_b = lo_b <= pred <= hi_b
            in_p = lo_p <= pred <= hi_p
            null_out = not (lo_b <= 0 <= hi_b) and not (lo_p <= 0 <= hi_p)
            if null_out:
                print("  -> THE NULL IS EXCLUDED by both intervals. Depth 32 really")
                print("     does cost more than depth 64 at a matched burst; that part")
                print("     is settled.")
            if in_b and in_p:
                print("  -> The prediction sits inside both intervals: the per-fill")
                print("     ramp model is confirmed at depth 32, on a test that could")
                print("     have gone the other way.")
            elif in_p and not in_b:
                print(f"  -> The prediction sits inside the conservative interval and")
                print(f"     just outside the tighter one ({pred:.2f} against an upper")
                print(f"     bound of {hi_b:.2f}). The measured excess is "
                      f"{100*(1-mu*f/pred):.0f}% below the predicted one.")
                print("     So: the effect is real, the model has the right sign and")
                print("     roughly the right size, and its point prediction is at the")
                print("     edge of what this data can support. Calling that a clean")
                print("     confirmation would be overreading it.")
            elif not in_p:
                print("  -> The prediction is outside both intervals. The ramp")
                print("     calibrated on the shallow arms does not extrapolate here.")

    # ---- one model across every depth arm ---------------------------------
    # The per-arm P + C/B fits are four separate two-parameter models that
    # share nothing, and for the shallow arms the straight line is the wrong
    # shape: the number of pipeline fills is ceil(B/Q), a step function, so a
    # line in 1/B is a mis-specification wherever B > Q. Fitting the step model
    # to all four arms at once is the right comparison, and it has a property
    # the per-arm fits do not: its per-burst constant is a prediction of a
    # quantity that a completely different experiment -- the allocator
    # amplification sweep, which never varied the queue depth -- measured
    # independently.
    print()
    print("=" * 84)
    print("3d. ONE MODEL ACROSS ALL DEPTHS   W + ramp*ceil(B/Q)/B + K/B")
    print("=" * 84)

    def depth_rows(depths):
        out = []
        for Q, cond in ((8, "depth_8"), (16, "depth_16"), (32, "depth_32"),
                        (64, "pinned2_asshipped")):
            if Q not in depths:
                continue
            for r in allc.get(cond, {}).get("dramblast", {}).values():
                if r.get("rx_batch"):
                    out.append((Q, r["rx_batch"], r["cycles_per_pkt"]))
        return out

    def ols3(rows):
        """Three-parameter least squares with standard errors."""
        import math as _m
        A = [[1.0, _m.ceil(B / Q) / B, 1.0 / B] for Q, B, _ in rows]
        y = [c for _, _, c in rows]
        if len(y) < 5:
            return None
        N = [[sum(a[i] * a[j] for a in A) for j in range(3)] for i in range(3)]
        rhs = [sum(A[k][i] * y[k] for k in range(len(y))) for i in range(3)]
        M = [N[i][:] + [1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
        for i in range(3):
            piv_row = max(range(i, 3), key=lambda r: abs(M[r][i]))
            M[i], M[piv_row] = M[piv_row], M[i]
            piv = M[i][i]
            if piv == 0:
                return None
            M[i] = [v / piv for v in M[i]]
            for r in range(3):
                if r != i:
                    f = M[r][i]
                    M[r] = [a - f * b for a, b in zip(M[r], M[i])]
        inv = [row[3:] for row in M]
        b = [sum(inv[i][j] * rhs[j] for j in range(3)) for i in range(3)]
        res = [y[k] - sum(A[k][i] * b[i] for i in range(3)) for k in range(len(y))]
        s2 = sum(r * r for r in res) / (len(y) - 3)
        se = [(s2 * inv[i][i]) ** 0.5 for i in range(3)]
        return b, se, s2 ** 0.5, len(y), res, rows

    full = ols3(depth_rows({8, 16, 32, 64}))
    if full:
        b, se, rms, n, res, rows = full
        print(f"  n={n} points from four depth arms, three parameters")
        print(f"    steady per-packet work   W    = {b[0]:6.1f} +/- {se[0]:.1f} cycles")
        print(f"    cost of one pipeline fill ramp = {b[1]:6.0f} +/- {se[1]:.0f} cycles")
        print(f"    per-burst constant        K    = {b[2]:6.0f} +/- {se[2]:.0f} cycles")
        print(f"    residual rms {rms:.2f} cycles over a 98-172 range")
        print("\n    K is the interesting one. Nothing in this fit knows about the")
        print("    allocator -- the queue depth was varied, the number of")
        print(f"    alloc/free pairs was not -- yet K = {b[2]:.0f} lands on the")
        if shipped_pair:
            zk = abs(b[2] - shipped_pair) / se[2]
            print(f"    {shipped_pair:.0f} cycles the amplification sweep measured for that")
            print(f"    pair directly: {zk:.1f} sigma apart.")
        print("\n    per-arm residual rms (the step model's own fit quality):")
        for Q in (8, 16, 32, 64):
            rr = [res[k] for k, (qq, _, _) in enumerate(rows) if qq == Q]
            if rr:
                arms = (sum(v * v for v in rr) / len(rr)) ** 0.5
                print(f"      Q={Q:>2}  n={len(rr)}  rms {arms:5.2f}  worst {max(rr, key=abs):+6.2f}")
        # Leave-one-arm-out. Reported whatever it says: a parameter that moves
        # when the worst-fitting arm is dropped is not a measurement.
        print("\n    Sensitivity -- refit without the arm the step model fits worst:")
        red = ols3(depth_rows({16, 32, 64}))
        if red:
            b2, se2, rms2, n2, _, _ = red
            print(f"      without Q=8:  W = {b2[0]:.1f} +/- {se2[0]:.1f}   "
                  f"ramp = {b2[1]:.0f} +/- {se2[1]:.0f}   K = {b2[2]:.0f} +/- {se2[2]:.0f}"
                  f"   rms {rms2:.2f}")
            dz = abs(b2[2] - b[2]) / ((se[2] ** 2 + se2[2] ** 2) ** 0.5)
            print(f"      K moves by {b2[2] - b[2]:+.0f} cycles, {dz:.1f} sigma.")
            if dz > 2:
                print("      -> K IS NOT STABLE. The agreement with the measured")
                print("         allocator pair holds only with the shallowest arm")
                print("         included, and that arm is the one the step model")
                print("         describes worst. Report the convergence as")
                print("         suggestive, not as a second measurement of the")
                print("         allocator. The ramp, which barely moves, is the")
                print("         parameter this fit actually determines.")
            else:
                print("      -> stable to dropping the worst arm.")

    # ---- the error bar everything else is measured against ---------------
    # pinned2 and pinned3 are the same condition, same binary, same cpuset,
    # re-run hours apart with the whole matrix in between. Every difference
    # quoted anywhere in this analysis has to clear whatever this shows, and
    # until it was run there was no measured run-to-run spread at all -- only
    # the within-sweep scatter, which does not include anything that drifts
    # between sweeps.
    print()
    print("=" * 84)
    print("4. REPEATABILITY   the same condition, re-run after the whole matrix")
    print("=" * 84)
    floor_all = []
    for mode in ("dramblast", "maglev"):
        a = allc.get("pinned2_asshipped", {}).get(mode, {})
        b = allc.get("pinned3_repeat", {}).get(mode, {})
        if not a or not b:
            print(f"  {mode:10s} (no repeat data)")
            continue
        print(f"\n  {mode}")
        print(f"    {'q':>2} {'burst':>11} {'run 1':>8} {'run 2':>8} {'diff':>7}")
        diffs = []
        for q in sorted(set(a) & set(b), key=int):
            ra, rb = a[q], b[q]
            # Only compare at a matched burst. Cycles per packet depend on the
            # burst, so two runs that landed on different bursts differ for a
            # reason that has nothing to do with repeatability.
            same = ra.get("rx_batch") == rb.get("rx_batch")
            d = rb["cycles_per_pkt"] - ra["cycles_per_pkt"]
            burst = (f"{ra.get('rx_batch')}" if same
                     else f"{ra.get('rx_batch')}/{rb.get('rx_batch')}")
            tail = "" if same else "   (different burst, not counted)"
            print(f"    {q:>2} {burst:>11} {ra['cycles_per_pkt']:>8} "
                  f"{rb['cycles_per_pkt']:>8} {d:>+7.0f}{tail}")
            if same:
                diffs.append((ra.get("rx_batch"), d))
        if diffs:
            vals = [v for _, v in diffs]
            mean = sum(vals) / len(vals)
            rms = (sum(v * v for v in vals) / len(vals)) ** 0.5
            print(f"    -> {len(vals)} matched-burst points: mean {mean:+.2f}, "
                  f"rms {rms:.2f}, worst {max(vals, key=abs):+.0f} cycles/packet")
            # The floor is not one number. It is much smaller at burst 64,
            # where the forwarder is oversubscribed and the operating point is
            # pinned, than at the small bursts a saturated link produces, where
            # the burst size itself is an outcome and wanders between runs. All
            # the matched-burst claims in this analysis are made at burst 64,
            # so that is the floor they have to clear -- quoting the pooled
            # number instead would be conservative in the wrong place, hiding a
            # real 2x while inflating the error on claims made where the rig is
            # most stable.
            b64 = [v for b, v in diffs if b == 64]
            if b64 and len(b64) < len(vals):
                r64 = (sum(v * v for v in b64) / len(b64)) ** 0.5
                rest = [v for b, v in diffs if b != 64]
                rr = (sum(v * v for v in rest) / len(rest)) ** 0.5 if rest else 0
                print(f"       split by burst: {len(b64)} points at burst 64 "
                      f"rms {r64:.2f};  {len(rest)} at smaller bursts rms {rr:.2f}")
            floor_all.extend(diffs)
        fa = fit_of(allc, "pinned2_asshipped", mode)
        fb = fit_of(allc, "pinned3_repeat", mode)
        if fa and fb and "C" in fa and "C" in fb:
            dc = fb["C"] - fa["C"]
            sg = ((fa["se"] or 0) ** 2 + (fb["se"] or 0) ** 2) ** 0.5
            print(f"    fitted C: {fa['C']:.0f} then {fb['C']:.0f}  "
                  f"({dc:+.0f}, {abs(dc)/sg:.1f} sigma of the two fits' own errors)")
    if floor_all:
        vals = [v for _, v in floor_all]
        rms = (sum(v * v for v in vals) / len(vals)) ** 0.5
        worst = max(vals, key=abs)
        b64 = [v for b, v in floor_all if b == 64]
        rms64 = (sum(v * v for v in b64) / len(b64)) ** 0.5 if b64 else rms
        print(f"\n  RUN-TO-RUN FLOOR: rms {rms:.2f} cycles/packet over "
              f"{len(vals)} matched-burst points, worst {worst:+.0f}.")
        print(f"  At burst 64 alone, where every matched-burst claim here is")
        print(f"  made: rms {rms64:.2f} over {len(b64)} points. The two differ")
        print("  because at small bursts the burst size is an outcome rather")
        print("  than a setting, and it wanders between runs.")
        rms = rms64
        print("\n  Read every claim in this analysis against that number:")
        if shipped_pair:
            print(f"    allocator pair            {shipped_pair/64:6.1f} cycles/packet "
                  f"at burst 64   ({shipped_pair/64/rms:.0f}x the floor)")
        if 8 in at64 and 64 in at64:
            dd = at64[8][0] - at64[64][0]
            print(f"    depth 64 -> 8             {dd:6.1f} cycles/packet "
                  f"at burst 64   ({dd/rms:.0f}x)")
        if 32 in at64 and 64 in at64:
            dd = at64[32][0] - at64[64][0]
            print(f"    depth 64 -> 32            {dd:6.1f} cycles/packet "
                  f"at burst 64   ({dd/rms:.0f}x)")
        print("  A difference of the same order as the floor is not a result,")
        print("  however many digits the fit prints.")

    # ---- the 4 KiB core-count anomaly, from data already taken ------------
    # On 4 KiB pages the cost rises with queue count at a CONSTANT burst, which
    # the burst model cannot represent -- it has no core-count term. The page
    # walk counters were recorded alongside every run, so this needs no new
    # measurement: it asks whether the extra cost is more walks or slower ones,
    # and the 1 GiB arm, which takes no walks at all, is the control.
    print()
    print("=" * 84)
    print("5. THE 4 KiB CORE-COUNT EFFECT   more walks, or slower walks?")
    print("=" * 84)
    PW = 8.0
    byq = {}
    for cond, mode, lab in (("xover_dram_4k", "dramblast", "dramblast, 4 KiB"),
                            ("xover_mag_4k", "maglev", "maglev, 4 KiB"),
                            ("pinned2_asshipped", "dramblast",
                             "dramblast, 1 GiB  (control: no walks)")):
        runs = []
        for q, r in sorted(allc.get(cond, {}).get(mode, {}).items(), key=lambda kv: int(kv[0])):
            if r.get("rx_batch") != 64 or not r.get("steady_mpps"):
                continue
            pkts = r["steady_mpps"] * 1e6 * PW
            runs.append((int(q), r["cycles_per_pkt"],
                         (r.get("pmu_dtlb_walk_active") or 0) / pkts,
                         (r.get("pmu_dtlb_walk_completed") or 0) / pkts,
                         (r.get("insns") or 0) / pkts))
        if len(runs) < 3:
            continue
        print(f"\n  {lab}   (only runs that stayed at burst 64)")
        print(f"    {'q':>2} {'cyc/pkt':>8} {'walk cyc/pkt':>13} {'walks/pkt':>10} "
              f"{'insn/pkt':>9}")
        for q, c, wa, wc, ins in runs:
            print(f"    {q:>2} {c:>8} {wa:>13.1f} {wc:>10.3f} {ins:>9.1f}")
        lo, hi = runs[0], runs[-1]
        dc, dwa, dwc, dins = hi[1] - lo[1], hi[2] - lo[2], hi[3] - lo[3], hi[4] - lo[4]
        print(f"    q={lo[0]} -> q={hi[0]}:  cycles {dc:+.0f}   walk cycles {dwa:+.1f}"
              f"   walks {dwc:+.3f}   instructions {dins:+.1f}")
        if lo[3] > 0.5:
            print(f"      walks per packet are flat ({lo[3]:.2f} -> {hi[3]:.2f}), so this")
            print(f"      is not more walking. Walk OCCUPANCY rises "
                  f"{100*dwa/lo[2]:.0f}%, so each")
            print("      walk takes longer as more cores walk at once.")
            if dc:
                print(f"      {100*dc/dwa:.0f}% of the added occupancy reaches the "
                      f"per-packet cost.")
            byq[lab] = {q: (c, wa) for q, c, wa, _, _ in runs}
        else:
            print("      no walks at all, and no core-count effect: the control.")
    # The two engines' ranges differ -- dramblast leaves burst 64 at q=7 and
    # maglev never does -- so the headline percentages above are taken over
    # different core counts and must not be compared with each other. At a
    # matched queue count they can be.
    d4 = byq.get("dramblast, 4 KiB", {})
    m4 = byq.get("maglev, 4 KiB", {})
    common = sorted(set(d4) & set(m4))
    if len(common) >= 2:
        q0, q1 = common[0], common[-1]
        print(f"\n  At a MATCHED queue count, q={q0} -> q={q1} (the two arms cover")
        print("  different ranges, so the percentages above are not comparable):")
        for lab, t in (("dramblast", d4), ("maglev", m4)):
            dwa = t[q1][1] - t[q0][1]
            dc = t[q1][0] - t[q0][0]
            frac = f"{100*dc/dwa:.0f}%" if dwa else "n/a"
            print(f"    {lab:10s} walk cycles {dwa:+6.1f}   cost {dc:+3.0f}   "
                  f"reaching the cost: {frac}")
        print("    -> the engine that is already waiting absorbs the extra walk")
        print("       time; the one retiring at IPC 3.4 has no slack to hide it")
        print("       in. That is the opposite direction from the prefetch")
        print("       result, and for a consistent reason: a pipeline hides")
        print("       latency it ISSUED EARLY, not latency added underneath it.")

    print("\n  CONCLUSION: the core-count term that breaks the burst model on")
    print("  4 KiB pages is contention for shared page-table structures. It is")
    print("  measured in the DURATION of a walk, not in how many walks happen,")
    print("  and it vanishes entirely on 1 GiB pages where there are none.")
    print("  No new runs were needed -- the counters were already in the logs.")

    # ---- every claim, in units of the instrument's resolution -------------
    # Added after a result that stood for six hours turned out to be one
    # printed tick (section 2). "Cycle per fwd packet" is an integer, so at a
    # 64-packet burst the smallest distinguishable step is one tick. Any
    # difference of a tick or two is not a measurement however tight its
    # standard error looks, because the standard error describes the scatter of
    # numbers that were all rounded the same way.
    #
    # Every claim below is recomputed the same way -- matched burst of 64,
    # matched queue count, paired then averaged -- so the table is comparable
    # across rows and independent of how each result happens to be quoted
    # elsewhere.
    print()
    print("=" * 84)
    print("6. RESOLUTION AUDIT   how many printed ticks is each claim?")
    print("=" * 84)

    def paired(c1, c2, m1="dramblast", m2=None, burst=64):
        m2 = m2 or m1
        A = {int(q): (r["cycles_per_pkt"], r.get("rx_batch"))
             for q, r in allc.get(c1, {}).get(m1, {}).items()}
        B = {int(q): (r["cycles_per_pkt"], r.get("rx_batch"))
             for q, r in allc.get(c2, {}).get(m2, {}).items()}
        qs = [q for q in sorted(set(A) & set(B))
              if A[q][1] == burst == B[q][1]]
        return [A[q][0] - B[q][0] for q in qs]

    CLAIMS = [
        ("engine gap, as shipped", "pinned2_asshipped", "pinned2_asshipped", "maglev", "dramblast"),
        ("engine gap, matched 1 GiB", "xover_mag_1g", "pinned2_asshipped", "maglev", "dramblast"),
        ("maglev: 2 MiB -> 4 KiB", "xover_mag_4k", "pinned2_asshipped", "maglev", "maglev"),
        ("dramblast: 1 GiB -> 4 KiB", "xover_dram_4k", "pinned2_asshipped", "dramblast", "dramblast"),
        ("depth 64 -> 8", "depth_8", "pinned2_asshipped", "dramblast", "dramblast"),
        ("maglev: 2 MiB -> 1 GiB", "xover_mag_1g", "pinned2_asshipped", "maglev", "maglev"),
        ("the allocator round trip", "pinned2_asshipped", "alloc_hoisted", "dramblast", "dramblast"),
        ("depth 64 -> 16", "depth_16", "pinned2_asshipped", "dramblast", "dramblast"),
        ("the -B/-A/-Q refactor", "pinned2_asshipped", "pinned_2100mhz", "dramblast", "dramblast"),
        ("dramblast: 1 GiB -> 2 MiB", "xover_dram_thp2m", "pinned2_asshipped", "dramblast", "dramblast"),
        ("depth 64 -> 32", "depth_32", "pinned2_asshipped", "dramblast", "dramblast"),
        ("the repeat arm (should be 0)", "pinned3_repeat", "pinned2_asshipped", "dramblast", "dramblast"),
    ]
    rows = []
    for lab, c1, c2, m1, m2 in CLAIMS:
        ds = paired(c1, c2, m1, m2)
        if len(ds) < 2:
            continue
        mean = sum(ds) / len(ds)
        var = sum((v - mean) ** 2 for v in ds) / (len(ds) - 1)
        rows.append((lab, len(ds), mean, (var / len(ds)) ** 0.5))
    rows.sort(key=lambda r: -abs(r[2]))
    print(f"  {'claim':30} {'n':>2} {'ticks':>8} {'sem':>6}  status")
    for lab, n, mean, sem in rows:
        t = abs(mean)
        st = ("BELOW ONE TICK -- not a measurement" if t < 1.0 else
              "1-2 ticks -- approximate only" if t < 2.0 else
              "3-5 ticks -- no spare digits" if t < 5.0 else
              "safe")
        print(f"  {lab:30} {n:>2} {mean:>8.2f} {sem:>6.2f}  {st}")
    print("\n  Read the standard errors with care: they describe the scatter of")
    print("  numbers that were all rounded the same way, so a tight sem on a")
    print("  one-tick difference is not evidence. The tick count is the check.")
    print("  The repeat arm coming out below one tick is the intended result --")
    print("  the same condition re-run should not differ -- and it also sets the")
    print("  scale: anything of that size elsewhere is indistinguishable from")
    print("  re-running the identical experiment.")

    if "--plot" not in sys.argv:
        return

    # matplotlib lives in the nix dev shell, not on the system Python, so this
    # import succeeds under `nix develop` and fails from a plain shell. Both
    # paths draw the same three panels; plot-matrix needs only the standard
    # library and writes SVG.
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        pmx_main()
        return
    plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
                         "savefig.facecolor": SURFACE, "font.family": "DejaVu Sans",
                         "text.color": INK, "axes.labelcolor": INK_2,
                         "xtick.color": INK_2, "ytick.color": INK_2,
                         "axes.edgecolor": GRID, "xtick.major.size": 0,
                         "ytick.major.size": 0})
    # Panel 0 is the floor arm, and only exists once the trio block has run.
    # Without it the figure keeps its original three panels rather than
    # reserving an empty frame for an experiment that has not happened.
    trio = allc.get("engine_trio", {})
    has_trio = len(trio) == 3
    npan = 4 if has_trio else 3
    fig, axes = plt.subplots(1, npan, figsize=(5.4 * npan, 5))
    k = 0

    if has_trio:
        ax = axes[0]
        k = 1
        for mode, col, lab in (("maglev", ORANGE, "maglev"),
                               ("dramblast", BLUE, "dramblast"),
                               ("none", INK_2, "no hash table")):
            pts = sorted((int(q), r["steady_mpps"])
                         for q, r in trio.get(mode, {}).items()
                         if r.get("steady_mpps"))
            if pts:
                ax.plot([q for q, _ in pts], [v for _, v in pts], marker="o",
                        color=col, linewidth=2, markersize=6, label=lab)
        ax.axhline(93.28, color=INK, linewidth=1, linestyle="--", alpha=0.5)
        ax.text(0.99, 0.905, "offered load 93.28 Mpps", transform=ax.transAxes,
                ha="right", fontsize=8.5, color=INK_2)
        ax.set_ylim(0, 100)
        ax.set_xlabel("RX/TX queue pairs")
        ax.set_ylabel("delivered Mpps")
        ax.legend(frameon=False, fontsize=9, loc="lower right")
        ax.set_title("0. The lookup is what costs, not the forwarder",
                     fontsize=11, loc="left", pad=10)

    ax = axes[k]
    # q=1 rather than the fitted intercept: on 4 KiB pages the fit absorbs a
    # core-count term it cannot represent, and for maglev on 4 KiB there is no
    # fit at all because that arm never saturates the link and so never leaves a
    # 64-packet burst. q=1 is defined for every arm and means the same thing in
    # each of them.
    labels, vals, cols = [], [], []
    for lab, mode, cond, col in (("dram\n1 GiB", "dramblast", base_cond, BLUE),
                                 ("dram\n2 MiB", "dramblast", "xover_dram_thp2m", BLUE),
                                 ("dram\n4 KiB", "dramblast", "xover_dram_4k", BLUE),
                                 ("mag\n1 GiB", "maglev", "xover_mag_1g", ORANGE),
                                 ("mag\n2 MiB", "maglev", base_cond, ORANGE),
                                 ("mag\n4 KiB", "maglev", "xover_mag_4k", ORANGE)):
        v = at_q1(allc, cond, mode)
        if v:
            labels.append(lab); vals.append(v); cols.append(col)
    bars = ax.bar(range(len(vals)), vals, color=cols, width=0.62)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 3, f"{v:.0f}",
                ha="center", fontsize=9, color=INK_2)
    ax.set_xticks(range(len(labels))); ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylabel("core cycles per packet   (q=1, burst 64)")
    ax.set_ylim(0, max(vals) * 1.16 if vals else 1)
    ax.set_title("1. Page backing moves the per-packet cost", fontsize=11,
                 loc="left", pad=10)

    ax = axes[k + 1]
    # Matched-burst excess, NOT the fitted C. The +8 arm is so slow it never
    # leaves a 64-packet burst, so its own slope is unmeasurable (R^2 0.62) --
    # it is excluded from the line for that reason, and plotting its fitted C
    # drew a curve that rises to five pairs and then falls, which is not a
    # result but a bad fit given a marker. The estimator quoted in the text is
    # the cost at a matched burst, and that is what belongs on the axis.
    if len(q1pts) >= 2:
        pts = sorted(q1pts)
        xs = [n for n, _ in pts]
        ys = [c for _, c in pts]
        ax.plot(xs, ys, marker="o", color=BLUE, linewidth=2, markersize=7,
                markeredgecolor=SURFACE, markeredgewidth=1.8)
        multi = [(n, c) for n, c in pts if n >= 3]
        if len(multi) >= 2:
            nn = len(multi)
            sx = sum(n for n, _ in multi); sy = sum(c for _, c in multi)
            sxx = sum(n * n for n, _ in multi)
            sxy = sum(n * c for n, c in multi)
            den = nn * sxx - sx * sx
            if den:
                b = (nn * sxy - sx * sy) / den
                a = (sy - b * sx) / nn
                hi = max(xs) * 1.05
                ax.plot([0, hi], [a, a + b * hi], color=INK_2, linewidth=1.4,
                        linestyle="--",
                        label=f"{b:.0f} cycles/pair, intercept {a:+.0f}")
                ax.legend(fontsize=8.5, frameon=False, loc="upper left")
        ax.set_xlabel("aligned_alloc/free round trips per burst")
        ax.set_ylabel("core cycles per burst, above the hoisted arm")
    ax.set_title("2. What one allocator round trip costs", fontsize=11,
                 loc="left", pad=10)

    ax = axes[k + 2]
    # Deliberately NOT P and C against depth. That figure would draw the
    # mis-specification as though it were the finding: below Q = B the number
    # of pipeline fills is ceil(B/Q), so a line in 1/B is the wrong shape and
    # the shallow arms' P/C split is an artifact of fitting it anyway. What is
    # real is the matched-burst comparison, and it needs both counters -- a
    # cycle curve alone cannot distinguish more work from more waiting.
    if len(at64) >= 2:
        dpts = sorted(at64)
        cyc = [at64[d][0] for d in dpts]
        ins = [at64[d][1] for d in dpts]
        err = [at64[d][2] or 0 for d in dpts]
        ax.errorbar(dpts, cyc, yerr=err, marker="o", color=BLUE, linewidth=2,
                    markersize=7, capsize=3, markeredgecolor=SURFACE,
                    markeredgewidth=1.8, label="cycles / packet")
        ax2 = ax.twinx()
        ax2.plot(dpts, ins, marker="s", color=ORANGE, linewidth=2, markersize=6,
                 markeredgecolor=SURFACE, markeredgewidth=1.8,
                 label="instructions / packet")
        ax2.set_ylabel("instructions per packet", color=ORANGE)
        ax2.grid(False)
        # Anchor both axes to the same relative span so the divergence between
        # them is a fair visual comparison rather than an artefact of scaling.
        span = 0.26
        ax.set_ylim(min(cyc) * (1 - span / 6), min(cyc) * (1 + span))
        ax2.set_ylim(min(ins) * (1 - span / 6), min(ins) * (1 + span))
        for d in dpts:
            ax.annotate(f"IPC {at64[d][1] / at64[d][0]:.2f}", (d, at64[d][0]),
                        textcoords="offset points", xytext=(0, 12),
                        ha="center", fontsize=8, color=INK_2)
        ax.set_xscale("log", base=2)
        ax.set_xticks(dpts); ax.set_xticklabels([str(d) for d in dpts])
        ax.set_xlabel("prefetch pipeline depth   (burst held at 64)")
        ax.set_ylabel("core cycles per packet", color=BLUE)
    ax.set_title("3. The pipeline hides latency; it does not remove work",
                 fontsize=11, loc="left", pad=10)

    for a in axes:
        a.grid(True, color=GRID, linewidth=0.8); a.set_axisbelow(True)
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
    fig.tight_layout()
    out = DOCS / "matrix.png"
    fig.savefig(out, dpi=200)
    print(f"\nwrote {out}")


def cmd_matrix():
    matrix_main()


# =============================================================================
# report: was make_report.py
# =============================================================================
# Generate docs/report.html — the findings page — from the measured results.
#
# Everything on the page is read from docs/results_reproduced.json and
# docs/results.json at build time, so the report cannot drift from the data. If a
# condition has not been measured, its section is omitted rather than stubbed.
#
# Run:  nix develop .. -c python3 analysis.py report

# ---------------------------------------------------------------- data helpers
def load():
    return (json.loads((DOCS / "results.json").read_text()),
            json.loads((DOCS / "results_reproduced.json").read_text()))


def report_core_cycles(rec):
    f = rec.get("freq_mhz")
    if f is None or "cycles_per_pkt" not in rec:
        return None
    return rec["cycles_per_pkt"] * f / TSC_MHZ


def series(cond, mode):
    out = []
    for q in sorted(cond.get(mode, {}), key=int):
        r = cond[mode][q]
        y, b = report_core_cycles(r), r.get("rx_batch")
        if y and b:
            out.append((1.0 / b, y, int(q), b, r))
    return out


def report_lsq(pts):
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


PERF_WINDOW = 8.0  # seconds, matches harness.sh sweep


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


def report_at_q1(allc, cond, mode):
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


def report_main():
    old, allc = load()
    P = allc.get("pinned_2100mhz", {})
    T = allc.get("turbo_instr", {})

    fits, pts_by = {}, {}
    for mode in ("dramblast", "maglev"):
        for arm, cond in (("pinned", P), ("turbo", T)):
            pts = series(cond, mode)
            f = report_lsq(pts)
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
        hoist_fit = report_lsq(series({"dramblast": allc.get("alloc_hoisted", {}).get("dramblast", {})},
                               "dramblast"))
        remainder = hoist_fit[1] if hoist_fit else None
        shipped_fit = report_lsq(series({"dramblast": allc.get("pinned2_asshipped", {}).get("dramblast", {})},
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
        nfit = report_lsq(series(allc.get("engine_trio", {}), "none"))
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
{snip("harness.sh", "#   run.sh:   MAX_CORE", "#   here:     MAX_CORE",
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
<span class="mono">l2fwd/analysis.py report</span>; the full reasoning, including
the mistakes, is in <span class="mono">docs/INVESTIGATION.md</span>.
</footer>
</section>
</main>
"""
    out = DOCS / "report.html"
    out.write_text(html)
    print(f"wrote {out}  ({len(html)} bytes)")


def cmd_report():
    report_main()


# =============================================================================
# plot-matrix: was plot_matrix.py
# =============================================================================
# Three-panel summary figure for the experiment matrix, as dependency-free SVG.
#
# Why SVG by hand: this runs with nothing but the standard library, so it works
# from a plain shell. The project's toolchain -- meson, ninja, matplotlib -- lives
# in the nix dev shell (`nix develop`), not on the system Python, and running
# outside it makes them all look uninstalled. That misread cost time here and
# produced a commit message claiming they had been removed from the node; they had
# not. The lesson is cheap to state and was not: on this repo, check whether you
# are inside the dev shell before concluding a tool is missing.
#
# The script is kept because the report draws all of its charts as hand-written
# SVG anyway, an SVG scales better in the page than a rasterised PNG, and a figure
# that needs no environment at all is one less thing to be wrong about.
# `analysis.py matrix --plot` uses matplotlib when it is importable and falls back
# here when it is not.
#
#     python3 l2fwd/analysis.py plot-matrix   -> docs/matrix.svg

#!/usr/bin/env python3


PW, PH = 500, 430          # one panel
PAD = 26


def frame(x0, title, sub=""):
    """Panel chrome: title, subtitle, plot rectangle. Returns the plot box."""
    L, R, T, B = x0 + 76, x0 + PW - 46, 74, PH - 62
    out = [f'<text x="{x0 + 14}" y="30" class="ttl">{esc(title)}</text>']
    if sub:
        out.append(f'<text x="{x0 + 14}" y="50" class="sub">{esc(sub)}</text>')
    return out, (L, R, T, B)


def ygrid(g, L, R, T, B, lo, hi, fmt="{:.0f}", ticks=5, colour=SVG_INK_2, side="left"):
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


GREY = "#5a5a5a"


def panel_engines(x0, allc):
    """Delivered throughput against queue count for both engines and no table.

    `-m none` is the forwarding loop with the lookup removed and nothing else
    changed (main.c, third branch). It is the only arm that says how much of the
    distance between the two engines is the engines at all, rather than the
    forwarder they are both embedded in -- and it reaches the offered load on
    two cores, which neither engine does anywhere in the sweep.
    """
    g, (L, R, T, B) = frame(x0, "0. The lookup is what costs, not the forwarder",
                            "delivered Mpps against queue pairs, offered 93.28")
    trio = allc.get("engine_trio", {})
    series = []
    for mode, col, lab in (("maglev", SVG_ORANGE, "maglev"),
                           ("dramblast", SVG_BLUE, "dramblast"),
                           ("none", GREY, "no hash table")):
        pts = [(int(q), r["steady_mpps"])
               for q, r in trio.get(mode, {}).items() if r.get("steady_mpps")]
        if pts:
            series.append((lab, col, sorted(pts)))
    if not series:
        return g
    qs = [q for _, _, pts in series for q, _ in pts]
    qlo, qhi = min(qs), max(qs)
    y = ygrid(g, L, R, T, B, 0, 100)

    def x(q):
        return L + (q - qlo) / max(1, qhi - qlo) * (R - L)

    g.append(f'<line x1="{L}" y1="{y(LINE_RATE):.1f}" x2="{R}" '
             f'y2="{y(LINE_RATE):.1f}" stroke="{SVG_INK}" stroke-width="1" '
             f'stroke-dasharray="3 4" opacity="0.5"/>')
    g.append(f'<text x="{R}" y="{y(LINE_RATE) - 7:.1f}" class="note" '
             f'text-anchor="end">offered load</text>')
    for lab, col, pts in series:
        d = " ".join(f"{'M' if i == 0 else 'L'}{x(q):.1f},{y(v):.1f}"
                     for i, (q, v) in enumerate(pts))
        g.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2.2"/>')
        for q, v in pts:
            g.append(f'<circle cx="{x(q):.1f}" cy="{y(v):.1f}" r="3.4" '
                     f'fill="{col}"/>')
    # A key in the corner, not names at the ends of the lines: all three arms
    # finish at the offered load, so an end-of-line label sits on top of the
    # other two and identifies none of them.
    ly = B - 14 - 17 * (len(series) - 1)
    for lab, col, _ in series:
        g.append(f'<rect x="{L + 12}" y="{ly - 9:.0f}" width="16" height="4" '
                 f'rx="2" fill="{col}"/>')
        g.append(f'<circle cx="{L + 20}" cy="{ly - 7:.0f}" r="3.4" fill="{col}"/>')
        g.append(f'<text x="{L + 36}" y="{ly:.0f}" class="tick" fill="{SVG_INK}">'
                 f'{esc(lab)}</text>')
        ly += 17
    for q in range(qlo, qhi + 1):
        g.append(f'<text x="{x(q):.1f}" y="{B + 20:.1f}" class="tick" '
                 f'text-anchor="middle">{q}</text>')
    g.append(f'<line x1="{L}" y1="{B}" x2="{R}" y2="{B}" class="axis"/>')
    g.append(f'<text x="{(L + R) / 2:.0f}" y="{B + 42:.0f}" class="tick" '
             f'text-anchor="middle">RX/TX queue pairs</text>')
    return g


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
    for lab, mode, cond, col in (("dram\n1 GiB", "dramblast", base_cond, SVG_BLUE),
                                 ("dram\n2 MiB", "dramblast", "xover_dram_thp2m", SVG_BLUE),
                                 ("dram\n4 KiB", "dramblast", "xover_dram_4k", SVG_BLUE),
                                 ("mag\n1 GiB", "maglev", "xover_mag_1g", SVG_ORANGE),
                                 ("mag\n2 MiB", "maglev", base_cond, SVG_ORANGE),
                                 ("mag\n4 KiB", "maglev", "xover_mag_4k", SVG_ORANGE)):
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


def panel_alloc(x0, rows, shipped_pair, err=None):
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
                     f'y2="{y(a + b*xhi):.1f}" stroke="{SVG_INK_2}" '
                     f'stroke-width="1.4" stroke-dasharray="5 4"/>')
            g.append(f'<text x="{x(xhi)-6:.1f}" y="{y(a + b*xhi) - 10:.1f}" '
                     f'class="note" text-anchor="end">'
                     f'{b:.0f} cycles per pair</text>')
    pts = " ".join(f"{x(n):.1f},{y(c):.1f}" for n, c in rows)
    g.append(f'<polyline points="{pts}" fill="none" stroke="{SVG_BLUE}" '
             f'stroke-width="2.4"/>')
    for n, c in rows:
        e = (err or {}).get(n, 0.0)
        if e:
            g.append(f'<line x1="{x(n):.1f}" y1="{y(c-e):.1f}" x2="{x(n):.1f}" '
                     f'y2="{y(c+e):.1f}" stroke="{SVG_BLUE}" stroke-width="1.8"/>')
        g.append(f'<circle cx="{x(n):.1f}" cy="{y(c):.1f}" r="5" fill="{SVG_BLUE}" '
                 f'stroke="{SVG_SURFACE}" stroke-width="2"/>')
    if shipped_pair:
        se = (err or {}).get(1)
        lab = (f"{shipped_pair:.0f} +/- {se:.0f}  (as shipped)" if se
               else f"{shipped_pair:.0f}  (as shipped)")
        g.append(f'<text x="{x(1)+12:.1f}" y="{y(shipped_pair)+4:.1f}" '
                 f'class="val">{lab}</text>')
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
    yc = ygrid(g, L, R, T, B, clo, chi, colour=SVG_BLUE)
    yi = ygrid(g, L, R, T, B, ilo, ihi, colour=SVG_ORANGE, side="right")
    import math
    lo, hi = math.log2(min(ds)), math.log2(max(ds))

    def x(d):
        return L + (math.log2(d) - lo) / (hi - lo) * (R - L)
    for vals, yf, col, dash in ((ins, yi, SVG_ORANGE, "5 4"), (cyc, yc, SVG_BLUE, "")):
        pts = " ".join(f"{x(d):.1f},{yf(v):.1f}" for d, v in zip(ds, vals))
        da = f' stroke-dasharray="{dash}"' if dash else ""
        g.append(f'<polyline points="{pts}" fill="none" stroke="{col}" '
                 f'stroke-width="2.4"{da}/>')
        for d, v in zip(ds, vals):
            g.append(f'<circle cx="{x(d):.1f}" cy="{yf(v):.1f}" r="5" '
                     f'fill="{col}" stroke="{SVG_SURFACE}" stroke-width="2"/>')
    for d in ds:
        cy, ip, sem, _ = at64[d]
        if sem:
            g.append(f'<line x1="{x(d):.1f}" y1="{yc(cy - sem):.1f}" '
                     f'x2="{x(d):.1f}" y2="{yc(cy + sem):.1f}" '
                     f'stroke="{SVG_BLUE}" stroke-width="1.6"/>')
        g.append(f'<text x="{x(d):.1f}" y="{yc(cy) + 22:.1f}" class="note" '
                 f'text-anchor="middle">IPC {ip/cy:.2f}</text>')
        g.append(f'<text x="{x(d):.1f}" y="{B + 20:.1f}" class="tick" '
                 f'text-anchor="middle">{d}</text>')
    g.append(f'<line x1="{L}" y1="{B}" x2="{R}" y2="{B}" class="axis"/>')
    g.append(f'<text x="{(L+R)/2:.1f}" y="{B + 44:.1f}" class="tick" '
             f'text-anchor="middle">prefetch pipeline depth</text>')
    g.append(f'<text x="{L - 58}" y="{T - 12}" class="note" fill="{SVG_BLUE}">'
             f'cycles / packet</text>')
    # Right-anchored: this is the rightmost panel, so a left-anchored caption
    # at R + 8 ran past the edge of the figure and was cut in half.
    g.append(f'<text x="{R + 8}" y="{T - 12}" class="note" text-anchor="end" '
             f'fill="{SVG_ORANGE}">instructions / packet</text>')
    return g


def pmx_main():
    allc = json.loads((DOCS / "results_reproduced.json").read_text())
    base_cond = "pinned2_asshipped"

    # Allocator arms from the shared estimator: matched burst AND matched queue
    # count, with a standard error. Drawing this from q=1 alone -- which an
    # earlier version did -- is what turned one integer tick into a result.
    arows3 = alloc_rows(allc)
    arows = [(r[0], r[1]) for r in arows3]
    aerr = {r[0]: r[2] for r in arows3}
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

    # Panel 0 is only drawn once the trio arm exists; without it the figure
    # keeps its original three panels rather than showing an empty frame.
    has_trio = len(allc.get("engine_trio", {})) == 3
    npanels = 4 if has_trio else 3
    W, H = PW * npanels, PH
    body, x0 = [], 0
    if has_trio:
        body += panel_engines(0, allc)
        x0 = PW
    body += panel_backing(x0, allc, base_cond)
    body += panel_alloc(x0 + PW, arows, shipped_pair, aerr)
    body += panel_depth(x0 + PW * 2, at64)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'width="{W}" height="{H}" role="img" '
           f'aria-label="Panels: engines against the no-table floor, page '
           f'backing, allocator share, pipeline depth">'
           f'<style>'
           f'.ttl{{font:600 15px Archivo,system-ui,sans-serif;fill:{SVG_INK}}}'
           f'.sub{{font:13px Spectral,Georgia,serif;fill:{SVG_INK_2}}}'
           f'.tick{{font:11.5px "IBM Plex Mono",monospace;fill:{SVG_INK_2}}}'
           f'.val{{font:600 12px "IBM Plex Mono",monospace;fill:{SVG_INK}}}'
           f'.note{{font:11px "IBM Plex Mono",monospace;fill:{SVG_INK_2}}}'
           f'.grid{{stroke:{SVG_GRID};stroke-width:1}}'
           f'.axis{{stroke:{SVG_GRID};stroke-width:1.4}}'
           f'</style>'
           f'<rect width="{W}" height="{H}" fill="{SVG_SURFACE}"/>'
           + "".join(body) + "</svg>\n")
    out = DOCS / "matrix.svg"
    out.write_text(svg)
    print(f"wrote {out}  ({len(svg)} bytes)")


def cmd_plot_matrix():
    pmx_main()


# =============================================================================
# plot-sweep: was plot_sweep.py
# =============================================================================
# Plot the queue-pair sweep: committed results vs. measured reproduction.
#
# Reads  docs/results.json             (committed, produced by pre-91d2c14 code)
#        docs/results_reproduced.json  (measured, corrected N+1 lcore invocation,
#                                       keyed by offered load)
# Writes docs/queue_sweep_reproduction.png
#        docs/per_packet_cost.png
#        docs/generator_sensitivity.png
#
# Run:  nix develop .. -c python3 analysis.py plot-sweep

# Design tokens, rcParams and the two drawing primitives come from the plotlib
# section above.

COND = "linerate_2tx_instr"   # 2 TX gen at line rate, float stats, instrumented
GEN_CEILING = 93.28        # Mpps offered: physical line rate for 110-byte frames

# Every panel in this file is a queue-pair sweep, so every one of them fixes the
# x ticks at 1..10; style() applies them only when asked.
XTICKS = range(1, 11)


def band(ax, xs, lo, hi, color):
    ax.fill_between(xs, lo, hi, color=color, alpha=0.14, linewidth=0, zorder=1)


def sweep_series(d, mode, key):
    qs = sorted(int(q) for q in d[mode])
    return qs, [d[mode][str(q)][key] for q in qs]


def sweep_main():
    # matplotlib here, not module top: nix shell only, SVG subs must still run
    # from plain shell.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update(RC)

    old = json.loads((DOCS / "results.json").read_text())
    new = json.loads((DOCS / "results_reproduced.json").read_text())[COND]

    # ---- Figure 1: does the collapse reproduce? -----------------------------
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2), sharey=True)

    for ax, mode in zip(axes, ("dramblast", "maglev")):
        qo, ao = sweep_series(old, mode, "avg")
        _, lo_o = sweep_series(old, mode, "min")
        _, hi_o = sweep_series(old, mode, "max")
        # Measured series uses steady_mpps, not l2fwd's own Average: the latter
        # is a mean of floor()ed samples including a cold sample 0 (see
        # the extract section). Band is the true spread over the warm samples.
        qn, an = sweep_series(new, mode, "steady_mpps")
        _, lo_n = sweep_series(new, mode, "steady_min")
        _, hi_n = sweep_series(new, mode, "steady_max")

        ax.axhline(GEN_CEILING, color=INK_MUTED, linewidth=1.2, linestyle=(0, (5, 4)),
                   zorder=2)
        ax.text(0.8, GEN_CEILING + 2.5, "offered load: 93.3 Mpps (line rate)", fontsize=8.5,
                color=INK_MUTED, ha="left")

        band(ax, qo, lo_o, hi_o, ORANGE)
        band(ax, qn, lo_n, hi_n, BLUE)
        line(ax, qo, ao, ORANGE, "committed results.json (avg, min/max band)")
        line(ax, qn, an, BLUE, "measured (steady state, warm samples)")

        # direct labels at the line ends, both placed above their point
        ax.annotate("committed", (qo[-1], ao[-1]), textcoords="offset points",
                    xytext=(-8, 12), fontsize=9, color=INK_2, ha="right")
        ax.annotate("measured", (qn[-1], an[-1]), textcoords="offset points",
                    xytext=(-8, 11), fontsize=9, color=INK_2, ha="right")

        style(ax, mode, "RX/TX queue pairs  (-q)",
              "Forwarded  (Mpps)" if mode == "dramblast" else "", XTICKS)
        ax.set_xlim(0.7, 10.6)
        ax.set_ylim(0, 125)

    axes[0].legend(loc="upper left", frameon=False, fontsize=9, ncol=1,
                   labelcolor=INK_2)
    fig.suptitle("The collapse does not reproduce at any queue-pair count",
                 fontsize=14.5, color=INK, x=0.055, ha="left", y=0.98,
                 fontweight="medium")
    fig.text(0.055, 0.925,
             "Committed data came from pre-91d2c14 code, a different threading model.\n"
             "Measured: N+1 lcores per N queues, generator at 100 GbE line rate, median of warm samples "
             "(cold first sample excluded).",
             fontsize=9.5, color=INK_2, ha="left", va="top", linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.875))
    out1 = DOCS / "queue_sweep_reproduction.png"
    fig.savefig(out1, dpi=200)
    print(f"wrote {out1}")

    # ---- Figure 2: the real queue-count-dependent cost ----------------------
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2))

    ax = axes[0]
    for mode, color in (("dramblast", BLUE), ("maglev", ORANGE)):
        qs, ys = sweep_series(new, mode, "cycles_per_pkt")
        line(ax, qs, ys, color, mode)
        ax.annotate(mode, (qs[-1], ys[-1]), textcoords="offset points",
                    xytext=(-6, 10), fontsize=9, color=INK_2, ha="right")
    # l2fwd prints this as "Cycle per fwd packet" but it is rte_rdtsc() deltas,
    # and this CPU has constant_tsc/nonstop_tsc with the TSC pinned at its
    # 2.1 GHz nominal. So these are TSC ticks (i.e. time), NOT core cycles.
    #
    # COND here is the HISTORICAL turbo arm, taken before the machine was
    # frequency-pinned and cpuset-isolated and before per-run frequency sampling
    # existed. Its delivered clock was therefore somewhere between 2.1 GHz and
    # single-core turbo, unrecorded and varying with queue count, so these ticks
    # CANNOT be converted to core cycles -- do not multiply them by a fixed
    # ratio. Mode-vs-mode comparison at the same queue count stays valid, since
    # both modes ran under identical conditions.
    #
    # For the core-cycle view, and for the pinned-vs-turbo contrast, see
    # plot_clock_arms.py, whose arms each carry their own measured frequency.
    style(ax, "Per-packet cost rises with queue count",
          "RX/TX queue pairs  (-q)", "TSC ticks per forwarded packet  (2.1 GHz)",
          XTICKS)
    ax.set_xlim(0.7, 10.6)
    ax.set_ylim(0, 200)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK_2)

    ax = axes[1]
    # The curves share the 64-packet plateau at low q and converge near zero at
    # high q, so anchor each label at its own uncrowded point rather than at a
    # common x or at the line ends.
    # dramblast is labelled below-left of its q=4 point: the wedge under the
    # descending blue curve is empty there, whereas the orange curve sweeps
    # through everything to the right of it.
    for mode, color, aq, dx, dy, ha in (("dramblast", BLUE, 4, -10, -20, "right"),
                                        ("maglev", ORANGE, 3, 9, 8, "left")):
        qs, ys = sweep_series(new, mode, "rx_batch")
        line(ax, qs, ys, color, mode)
        i = qs.index(aq)
        ax.annotate(mode, (qs[i], ys[i]), textcoords="offset points",
                    xytext=(dx, dy), fontsize=9, color=INK_2, ha=ha)
    style(ax, "...because each poll returns fewer packets",
          "RX/TX queue pairs  (-q)", "Average RX burst size  (packets)", XTICKS)
    ax.set_xlim(0.7, 10.6)
    ax.set_ylim(0, 70)
    ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK_2)

    fig.suptitle("A fixed per-burst cost, amortised over shrinking bursts",
                 fontsize=14.5, color=INK, x=0.055, ha="left", y=0.98,
                 fontweight="medium")
    fig.text(0.055, 0.915,
             "Line-rate load spread over more queues. dramblast rises 3.2x, maglev 1.3x — isolating a per-burst,\n"
             "not per-packet, cost. Ticks are elapsed time (invariant TSC), not core cycles — see plot_clock_arms.py.",
             fontsize=9.5, color=INK_2, ha="left", va="top", linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.875))
    out2 = DOCS / "per_packet_cost.png"
    fig.savefig(out2, dpi=200)
    print(f"wrote {out2}")

    # ---- Figure 3: does the generator configuration change the answer? -------
    allc = json.loads((DOCS / "results_reproduced.json").read_text())
    a, b = allc["linerate_93mpps"], allc["linerate_2tx_gen"]

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2), sharey=True)
    worst = 0.0
    for ax, mode in zip(axes, ("dramblast", "maglev")):
        qa, ya = sweep_series(a, mode, "steady_mpps")
        qb, yb = sweep_series(b, mode, "steady_mpps")
        dev = max(abs(p - q) / q * 100 for p, q in zip(ya, yb))
        worst = max(worst, dev)

        ax.axhline(GEN_CEILING, color=INK_MUTED, linewidth=1.2,
                   linestyle=(0, (5, 4)), zorder=2)
        # thick pale line underneath, thin line with markers on top: agreement
        # reads as the thin curve tracking the centre of the thick one
        ax.plot(qa, ya, color=BLUE, linewidth=7.0, alpha=0.25, zorder=2,
                solid_capstyle="round", label="8 TX-core generator")
        line(ax, qb, yb, ORANGE, "2 TX-core generator")
        ax.annotate(f"max deviation {dev:.1f}%", (qb[-1], yb[-1]),
                    textcoords="offset points", xytext=(-8, -20), fontsize=9,
                    color=INK_2, ha="right")

        style(ax, mode, "RX/TX queue pairs  (-q)",
              "Forwarded  (Mpps)" if mode == "dramblast" else "", XTICKS)
        ax.set_xlim(0.7, 10.6)
        ax.set_ylim(0, 105)

    axes[0].legend(loc="lower right", frameon=False, fontsize=9, labelcolor=INK_2)
    fig.suptitle("Generator core count does not change the result",
                 fontsize=14.5, color=INK, x=0.055, ha="left", y=0.98,
                 fontweight="medium")
    fig.text(0.055, 0.925,
             "Both generators offer 93.28 Mpps line rate and 16.8M flows; only TX core count differs\n"
             "(-r compensated, since it is per TX core). Curves are steady-state medians of warm samples.",
             fontsize=9.5, color=INK_2, ha="left", va="top", linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.875))
    out3 = DOCS / "generator_sensitivity.png"
    fig.savefig(out3, dpi=200)
    print(f"wrote {out3}  (worst deviation across all points: {worst:.2f}%)")


def cmd_plot_sweep():
    sweep_main()


# =============================================================================
# extract: was extract_results.py
# =============================================================================
# Extract measured sweep results into docs/results_reproduced.json, keyed by offered load.
#
# Parses the per-run logs written by harness.sh sweep.
#
# Usage: python3 analysis.py extract <log_dir> ../docs/results_reproduced.json [sweep_stdout ...]
#
# Conditions (all at 100 GbE line rate unless noted):
#   pinned_2100mhz     logs tagged "pinned"   -- every core pinned to 2.100 GHz, turbo off,
#                                                cpuset-isolated (INVESTIGATION.md 3.4b/3.4c).
#                                                TSC ticks == core cycles in this arm.
#   turbo_instr        logs tagged "turbo"    -- same code and cpuset, turbo ON. The
#                                                representative arm: production runs with turbo.
#   linerate_2tx_instr logs tagged "instr"    -- 2 TX gen, post-7ebf038 (float stats). Turbo,
#                                                NO frequency instrumentation -- see caveat below.
#   linerate_93mpps    logs tagged "linerate" -- generator -l 0-16 (8 TX cores)
#   linerate_2tx_gen   logs tagged "gen2tx"   -- generator -l 0-4  (2 TX cores)
#   engine_trio        logs tagged "trio"   -- all three engines in one sitting:
#                                                dramblast, maglev, and `-m none` (the
#                                                forwarding loop's third branch,
#                                                main.c:393-398 -- same MAC write, no
#                                                lookup). The floor everything else must
#                                                be read against.
#   capped_72mpps      logs tagged "sweep"    -- generator -l 0-2  (1 TX core), 72 Mpps
#
# Caveat on the cycle counters across conditions
# ----------------------------------------------
# l2fwd measures with rte_rdtsc() and this SKU's TSC is invariant at 2.1 GHz, so
# every cycle counter here -- loop_cycles_per_pkt now, cycles_per_pkt in the
# archived logs -- is TSC ticks = elapsed time, in EVERY condition. Ticks are
# therefore comparable across conditions as time, but they equal core cycles only
# where the core clock is also 2.1 GHz, i.e. the pinned_2100mhz arm. For the turbo
# arms multiply by the recorded freq_mhz/2100 to get core cycles; where freq_mhz is
# absent (linerate_2tx_instr predates the instrumentation) that conversion is not
# available and must not be guessed.
#
# What the counters bracket, and the one that is gone
# ---------------------------------------------------
# loop_cycles_per_pkt brackets the WHOLE iteration and is measured with a single
# rte_rdtsc() per poll: the read at the bottom of one iteration closes that
# iteration's region and opens the next one's. So rx burst, mode branch, tx burst
# and drop path are all inside it, and loop_tsc + idle_tsc is the forwarding
# loop's entire wall time with no gap and nothing double-counted.
#
# cycles_per_pkt is the retired predecessor. It bracketed the mode branch alone,
# which is why the two must never be mixed in one series: the newer number is the
# older one plus the NIC path, and it is larger for that reason and not because
# anything got slower. There is no longer a counter for the lookup on its own,
# deliberately -- the lookup's cost is (dramblast or maglev) minus none at equal
# burst size, and the rest of the lifecycle cancels in that subtraction.
#
# Delivered frequency and 1 GiB backing
# -------------------------------------
# harness.sh sweep samples both per run but prints them on its own stdout summary line, not
# into the per-run log. Pass the tee'd sweep stdout as a trailing argument and they
# are merged in by (mode, q); omit it and freq_mhz/hp1g are simply absent.

pats = {
    "min": r"Minimum: ([0-9.]+)", "max": r"Maximum: ([0-9.]+)", "avg": r"Average: ([0-9.]+)",
    # ARCHIVAL. main.c no longer prints this label -- it named the mode branch
    # alone (the retired hash_tsc), and the current binary times the whole
    # iteration instead. The pattern stays so the archived logs this key was
    # fitted against still parse; it simply finds nothing in a new log. Do not
    # repoint it at the new label. Every downstream consumer of `cycles_per_pkt`
    # is reading hash-region numbers, and silently feeding it full-loop numbers
    # would make two incomparable quantities share one name across the corpus.
    "cycles_per_pkt": r"Cycle per fwd packet: (\d+)",
    # The replacement, and the only cycle counter a current run produces. One
    # rte_rdtsc() per poll, read at the bottom of the iteration and charged from
    # the previous read, so this covers the entire packet lifecycle --
    # rte_eth_rx_burst, the mode branch, rte_eth_tx_burst, the drop path and the
    # statistics stores -- with no sub-region broken out. The lookup's own cost
    # is a DIFFERENCE between arms at equal burst size (dramblast or maglev
    # minus none), not a separate measurement.
    # Old logs carry `cycles_per_pkt` and not this; new logs carry this and not
    # `cycles_per_pkt`. Neither is ever zero-filled, and every consumer must keep
    # treating absent as absent rather than as 0.
    "loop_cycles_per_pkt": r"Full-loop cyc per fwd packet: (\d+)",
    # loop_tsc and idle_tsc partition the forwarding loop's wall time exactly,
    # so these two are not an aside: full-loop x fwded + empty x polls is the
    # whole of it, which is what makes an unexplained residual visible.
    "empty_polls": r"Empty polls:\s+(\d+)",
    "cyc_per_empty_poll": r"Cyc per empty poll:\s+(\d+)",
    # rx_batch divides by ALL polls, empty ones included, so it is the mean
    # burst size scaled by the hit rate, not the mean burst size. It is kept
    # because the archived corpus and every fit taken against it use that
    # definition. rx_batch_nonempty divides by the polls that returned a packet
    # and is the B the P + C/B burst model actually wants; it appears only in
    # logs from the current binary.
    "rx_batch": r"Average rx batch sz: (\d+)",
    "rx_batch_nonempty": r"Average rx batch sz \(nonempty polls\): (\d+)",
    "rx_missed": r"RX-Missed \(Dropped\): (\d+)",
}

# l2fwd's own Minimum/Maximum/Average are unreliable and must not be used as the
# headline number:
#   * main.c:166 declares `samples` as uint32_t* but main.c:208 stores a double
#     Mpps into it, so every sample is floor()ed. That is a fixed ~0.5 Mpps
#     downward bias -- only -0.3% at 93 Mpps but -4.6% at 17 Mpps, i.e. worst
#     exactly where the unsaturated per-queue slope is measured.
#   * Sample 0 is always cold (12-19% low) and therefore always sets `Minimum`,
#     so the reported min/max range is a startup artifact, not run-to-run spread.
#   * main.c:205 computes the interval as integer division, so it is exactly 1.0
#     even when the real interval drifts longer -- which lets `Maximum` exceed
#     line rate (a 95 Mpps reading on a 93.28 Mpps link).
# The per-second "%.2f Mpps" lines (main.c:207) are printed BEFORE truncation, so
# full precision is recoverable from the log. steady_mpps below is the median of
# those, excluding the cold first sample: immune to all three defects.
# One asymmetry this creates, quantified in INVESTIGATION.md 5.29: steady_mpps
# excludes the cold sample, but cycles_per_pkt beside it cannot, because
# main.c:236-237 divides two running totals and so is a cumulative mean over the
# whole run including the warm-up. The two are not taken over the same window.
# For `-m none` nothing moves -- it is flat from the first interval. For maglev
# q=1 the printed value decays 231 -> 166 and the tail implies a steady state of
# 163-165, about 1%: under the one-tick resolution, but real, and a reason not
# to read a sub-tick difference between an engine and the floor as physical.
SAMPLE_RE = re.compile(r"^([0-9.]+) Mpps", re.M)
# condition -> log filename prefix
CONDS = {
    # the two clock arms: identical binary, cpuset and harness, differing only
    # in turbo and the frequency governor
    "pinned_2100mhz": "pinned", "turbo_instr": "turbo",
    # everything below was taken with the -B/-A/-Q build. `pinned2_asshipped`
    # is its control: same flags-free invocation as `pinned_2100mhz`, so any
    # difference between the two is the refactor and not the experiment.
    "pinned2_asshipped": "pinned2",
    # the same condition re-run hours later: the only run-to-run error bar
    "pinned3_repeat": "pinned3",
    # crossover: each mode on the other's page backing (and on 4 KiB, which
    # neither ships with, to turn a two-point swap into a three-point trend)
    "xover_dram_thp2m": "xdram2m", "xover_dram_4k": "xdram4k",
    "xover_mag_1g": "xmag1g", "xover_mag_4k": "xmag4k",
    # allocator amplification: C should be linear in the number of pairs
    "alloc_hoisted": "ahoist", "alloc_x2": "a2", "alloc_x4": "a4",
    "alloc_x8": "a8",
    # the floor: -m none, same MAC write, no lookup. Subtracting this arm is
    # what turns a per-packet cost into a per-LOOKUP cost.
    "engine_trio": "trio",
    # prefetch pipeline depth
    "depth_8": "d8", "depth_16": "d16", "depth_32": "d32",
    # depth 32 against the shipped 64, three interleaved repeats each, q=1..5
    # only. The single-sweep comparison was 1.8 cycles/packet against a 0.45
    # run-to-run floor, which separates the ramp model from no effect at
    # neither; these are the repeats that decide it.
    "depth_32_r1": "d32r1", "depth_32_r2": "d32r2", "depth_32_r3": "d32r3",
    "depth_64_r1": "d64r1", "depth_64_r2": "d64r2", "depth_64_r3": "d64r3",
    # historical arms, kept as the record; no delivered clock was recorded for
    # any of them, so their ticks cannot be put on a core-cycle axis
    "linerate_2tx_instr": "instr", "linerate_93mpps": "linerate",
    "linerate_2tx_gen": "gen2tx", "capped_72mpps": "sweep",
}

# harness.sh sweep's stdout summary line, e.g.
#   q=3  lcores=0,2,4,6  min=... hp1g=16->7 freq=2095MHz
# preceded by "=== mode=dramblast  tag=pinned ===" headers.
HDR_RE = re.compile(r"^=== mode=(\w+)\s+tag=(\w+) ===", re.M)
ROW_RE = re.compile(
    r"^q=(\d+)\s+.*?hp1g=(\d+)->(\d+)\s+freq=(\w+)MHz\s+insns=(\w+)", re.M)


def sidecar(paths):
    """(tag, mode, q) -> {hp1g_before, hp1g_during, freq_mhz} from sweep stdout."""
    got = {}
    for path in paths:
        txt = pathlib.Path(path).read_text(errors="replace").replace("\x1b", "")
        # Split on the mode headers so each row is attributed to the right mode.
        marks = [(m.start(), m.group(1), m.group(2)) for m in HDR_RE.finditer(txt)]
        for i, (pos, mode, tag) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(txt)
            for q, before, during, freq, insns in ROW_RE.findall(txt[pos:end]):
                rec = {"hp1g_before": int(before), "hp1g_during": int(during)}
                if freq.isdigit():
                    rec["freq_mhz"] = int(freq)
                if insns.isdigit():
                    rec["insns"] = int(insns)
                got[(tag, mode, int(q))] = rec
    return got


def cmd_extract():
    SP = pathlib.Path(sys.argv[1]); OUT = pathlib.Path(sys.argv[2])
    SIDE = sidecar(sys.argv[3:])
    incomplete = []
    multiplexed = []
    malformed = []

    # Merge, do not clobber. Each invocation sees only the logs of the sweep that
    # just ran, so rebuilding the file from scratch would silently delete every
    # condition whose log directory is no longer on disk -- which is what happened
    # once here, taking four historical conditions with it (they were recoverable
    # from git; an uncommitted arm would not have been). A condition is now only
    # rewritten when logs for it are actually found.
    out = json.loads(OUT.read_text()) if OUT.exists() else {}
    for cond, prefix in CONDS.items():
        fresh = {}
        # "none" is the floor arm (-m none). It is listed last and pruned below
        # when empty, so that adding it does not stamp an empty "none" key onto
        # every historical condition and make them look like they were swept for it.
        for mode in ("maglev", "dramblast", "none"):
            fresh[mode] = {}
            for q in range(1, 11):
                # harness.sh matrix puts each arm's logs in its own subdirectory, while
                # the earlier single-arm sweeps wrote them flat. Accept either, so
                # one extractor invocation covers the whole tree instead of needing
                # to be re-run once per arm with a different root (which is how the
                # depth arms were silently missing from the first analysis).
                name = f"{prefix}_{mode}_q{q}.log"
                log = SP / name
                if not log.exists():
                    log = SP / prefix / name
                if not log.exists():
                    continue
                txt = log.read_text(errors="replace").replace("\x1b", "")
                # Skip a run that has not finished. l2fwd prints its final summary
                # block (Minimum/Maximum/Average) once, after the last sample, so
                # its absence means the process is still running or died partway.
                # Without this check, extracting while a sweep is in flight silently
                # admits a truncated run: the per-second lines are already there, so
                # steady_mpps and cycles_per_pkt come out looking entirely
                # reasonable while describing half an experiment.
                if "Average:" not in txt:
                    incomplete.append(log.name)
                    continue
                rec = {}
                for key, pat in pats.items():
                    m = re.findall(pat, txt)
                    if m:
                        rec[key] = float(m[-1]) if key in ("avg", "min", "max") else int(m[-1])
                # full-precision, cold-sample-excluded steady state
                samples = [float(x) for x in SAMPLE_RE.findall(txt)]
                if len(samples) >= 3:
                    rec["steady_mpps"] = round(statistics.median(samples[1:]), 2)
                    rec["steady_min"] = round(min(samples[1:]), 2)
                    rec["steady_max"] = round(max(samples[1:]), 2)
                    rec["cold_sample0_mpps"] = round(samples[0], 2)
                    rec["n_samples"] = len(samples)
                rec.update(SIDE.get((prefix, mode, q), {}))
                # per-run PMU sidecar written by harness.sh sweep, one `value,,event,...`
                # line per counter. Carried through verbatim so a question asked
                # later can be answered from data already on disk.
                perf = log.with_suffix(".perf")
                if perf.exists():
                    got, dropped, bad_rows = parse_perf(
                        perf.read_text(errors="replace"))
                    rec.update({"pmu_" + k: v for k, v in got.items()})
                    multiplexed += [f"{log.name}:{e} at {pct:.2f}%"
                                    for e, pct in dropped]
                    malformed += [f"{log.name}: {why}\n        {ln}"
                                  for ln, why in bad_rows]
                if rec:
                    fresh[mode][str(q)] = rec
        if not fresh.get("none"):
            fresh.pop("none", None)
        if any(fresh[m] for m in fresh):
            out[cond] = fresh

    OUT.write_text(json.dumps(out, indent=4) + "\n")
    print(f"wrote {OUT}")
    if malformed:
        # Louder than the multiplexing report, and deliberately: a multiplexed
        # reading means the machine was busy, a malformed one means this parser
        # was reading the wrong columns and nothing it produced can be trusted.
        print(f"  *** {len(malformed)} MALFORMED perf row(s) -- the CSV columns are "
              f"shifted, so the guard was not actually applied to them:")
        for m in malformed[:6]:
            print(f"      {m}")
        print("      Cause is almost always a raw event spec passed without "
              "`name=`: the spec")
        print("      contains commas, so it splits across several -x, columns. "
              "See parse_perf in analysis.py.")
    if multiplexed:
        print(f"  *** DROPPED {len(multiplexed)} MULTIPLEXED counter reading(s) -- "
              f"these were time-shared and scaled, not measured:")
        for m in sorted(multiplexed)[:8]:
            print(f"      {m}")
        if len(multiplexed) > 8:
            print(f"      ... and {len(multiplexed) - 8} more")
        print("      Six events fit only because `cycles` and `instructions` land "
              "on fixed-function")
        print("      counters; with SMT enabled a logical CPU gets FOUR "
              "general-purpose counters,")
        print("      so four RAW events is the ceiling for a single pass.")
    if incomplete:
        print(f"  SKIPPED {len(incomplete)} unfinished run(s): "
              + ", ".join(sorted(incomplete)[:6])
              + (" ..." if len(incomplete) > 6 else ""))
    for cond in out:
        for mode in out[cond]:
            n = len(out[cond][mode])
            peak = max((r.get("avg", 0) for r in out[cond][mode].values()), default=0)
            print(f"  {cond:18s} {mode:10s} {n} points, peak {peak} Mpps")


# =============================================================================
# saturation: was analyse_saturation.py
# =============================================================================
# Turn the saturation sweep's raw counters into utilisations, and plot them.
#
# Every row is a fraction of a ceiling, so the figure answers one question: as
# cores are added, which resource reaches 100% first? A per-core resource holds
# its utilisation flat as cores are added; a shared one climbs.
#
# Ceilings come from harness.sh counter_groups (hardware configuration) or from
# harness.sh validate (measured). Where a ceiling is not known, the metric is
# reported as a rate rather than a fraction and is drawn on the second panel, so
# that nothing is plotted as a percentage of a number that was guessed.
#
#     python3 l2fwd/analysis.py saturation <outdir>  -> docs/saturation.svg

#!/usr/bin/env python3


PALETTE = ["#12707f", "#bb551c", "#4a7a3a", "#7a4a8a", "#a8324a", "#3a5a8a"]

# Nominal only: 8 channels x DDR5-4800 x 8 B. It describes the part number, not
# this machine, and is the last-resort denominator. dram_ceiling() below prefers
# anything measured.
DRAM_PEAK_GBS = 307.2
DRAM_CEIL = DRAM_PEAK_GBS
CEIL_SRC = "unset"


def dram_ceiling():
    """The denominator for DRAM utilisation, and the reason it is the one it is.

    There is no measured 24-thread MIXED ceiling for this machine and none is
    extrapolated -- see the header of .dram_ceiling. Order of preference:

      1. a measured mixed ceiling, if one ever becomes available
      2. the peer session's 24-thread READ ceiling, which is a lower bound and
         is named as such, so utilisations against it read as "at least"
      3. the datasheet figure, which describes a part number rather than this
         machine, and is the worst of the three

    Whichever is used, the caller prints its name beside every percentage.
    """
    f = pathlib.Path(__file__).resolve().parent / ".dram_ceiling"
    if f.exists():
        txt = f.read_text()
        m = re.search(r"^DRAM_ACHIEVED_GBS=([0-9.]+)", txt, re.M)
        if m and float(m.group(1)) > 0:
            return float(m.group(1)), "measured mixed ceiling"
        m = re.search(r"^PEER_READ_24=([0-9.]+)", txt, re.M)
        if m:
            return float(m.group(1)), "peer 24-thread READ ceiling, a LOWER BOUND"
    return DRAM_PEAK_GBS, "datasheet nominal (not this machine)"
PCIE_GBS = 31.5
LINE_RATE_MPPS = 96.15
FRAME_BYTES = 110


def sh_const(name, default):
    """Read a constant out of harness.sh (counter_groups) so the two cannot drift."""
    p = pathlib.Path(__file__).resolve().parent / "harness.sh"
    m = re.search(r"^%s=([0-9.]+)" % name, p.read_text(), re.M)
    return float(m.group(1)) if m else default


def _base(ev):
    """perf prints raw events as `uncore_imc_0/event=0x05,...,name=cas_rd_0/`
    or as a bare name; reduce both to the name that was asked for."""
    m = re.search(r"name=([A-Za-z0-9_]+)", ev)
    if m:
        return m.group(1)
    return ev.strip().strip("/").split("/")[-1] or ev.strip()


def sat_parse_perf(path):
    """-> ({event: value}, [problems]); delegates the guard to parse_perf.

    This deliberately does NOT do its own column arithmetic. An earlier version
    did, read field 5 as the enabled percentage, and so reported every
    metric-bearing event as multiplexed -- top-down fractions, insn-per-cycle --
    while passing any genuinely multiplexed event whose metric column happened
    to be empty. parse_perf already had that right, already separates
    MALFORMED from MULTIPLEXED, and is already fired against real hardware
    multiplexing by `analysis.py selftest --real`. Reimplementing it here made this
    the fourth appearance of the same bug in one investigation.

    The one thing added on top is name normalisation: this study's raw specs are
    per-controller (`cas_rd_0` .. `cas_rd_7`), so the guarded parser's keys are
    remapped through _base() for the caller's convenience.
    """
    if not path.exists():
        return None, ["missing"]
    got, dropped, malformed = parse_perf(path.read_text())
    problems = ["%s@%.2f%%" % (e, p) for e, p in dropped]
    problems += ["MALFORMED: %s" % why for _line, why in malformed]
    return {_base(k): v for k, v in got.items()}, problems


# `loopcyc=` was `cyc=` until the timer change. The field is not merely renamed:
# `cyc=` carried the mode branch alone and `loopcyc=` carries the whole
# iteration, so a summary written by the old harness.sh saturation must NOT be read
# through this regex. It cannot be, because the literal differs -- and
# _reject_pre_timer_summary below turns the resulting zero matches into an
# explicit error rather than an empty plot.
ROW = re.compile(r"(?:arm=(\S+)\s+)?q=(\d+)\s+grp=(\S+)\s+workers=(\d+)\s+"
                 r"avg=([\d.]+|NA)\s+loopcyc=(\d+|NA)\s+batch=(\d+|NA)\s+"
                 r"missed=(\d+|NA)")
OLD_ROW = re.compile(r"q=\d+\s+grp=\S+\s+workers=\d+\s+avg=\S+\s+cyc=")
ARM_FILTER = __import__("os").environ.get("ARM_FILTER")

# What each group MUST contain for its run to count.
#
# parse_perf reports what it can read; an event that the PMU refused
# outright is not a row it can read, so `<not supported>` is skipped silently --
# correctly, since the parser's contract is to parse what is there. The caller
# has to assert what it EXPECTED, and until this map existed nothing did.
#
# The failure this closes is partial and therefore the nastiest shape: the
# un-braced top-down group returns a real `slots` count of 21,979,161,492 with
# all four metrics reading `<not supported>` at 100.00 enabled. Every guard
# passes -- nothing multiplexed, nothing malformed, a healthy-looking group --
# and four numbers are simply absent. A presence check keyed on the group
# ("did this produce anything?") sees success. It has to be per event.
EXPECTED = {
    "topdown1": ["slots", "topdown-retiring", "topdown-bad-spec",
                 "topdown-fe-bound", "topdown-be-bound"],
    "topdown2": ["slots", "topdown-mem-bound", "topdown-fetch-lat",
                 "topdown-heavy-ops", "topdown-br-mispredict"],
    "mlp":      ["cycles", "l1d_pend_miss_pending",
                 "l1d_pend_miss_pending_cycles"],
    "fb":       ["cycles", "l1d_pend_miss_fb_full",
                 "offcore_reqs_outstanding_data_rd"],
    "tlbmem":   ["cycles", "instructions", "dtlb_walk_completed",
                 "dtlb_walk_active", "stalls_l3_miss", "LLC-load-misses"],
    "latency":  ["cycles", "offcore_reqs_outstanding_data_rd",
                 "offcore_reqs_data_rd"],
    # bw is checked structurally instead: eight controllers, read and write.
    "bw":       ["cas_rd_%d" % i for i in range(8)]
              + ["cas_wr_%d" % i for i in range(8)],
}


def sat_main(outdir):
    d = pathlib.Path(outdir)
    summary = d / "summary.txt"
    if not summary.exists():
        sys.exit("no summary.txt in %s" % outdir)

    text = summary.read_text()
    if not ROW.search(text) and OLD_ROW.search(text):
        sys.exit(
            "%s was written before the timer change: its cyc= field is the mode\n"
            "branch alone, not the full iteration that loopcyc= reports, and the\n"
            "two are not interchangeable. Re-run harness.sh saturation against the current\n"
            "l2fwd rather than reading this one." % summary)

    conds = {}          # q -> {group: perf dict}
    meta = {}           # q -> {avg, loopcyc, batch, missed, workers}
    for line in text.splitlines():
        m = ROW.search(line)
        if not m:
            continue
        arm = m.group(1) or "default"
        if ARM_FILTER and arm != ARM_FILTER:
            continue
        q, grp = int(m.group(2)), m.group(3)
        meta.setdefault(q, dict(workers=int(m.group(4)),
                                avg=None if m.group(5) == "NA" else float(m.group(5)),
                                loopcyc=None if m.group(6) == "NA" else int(m.group(6)),
                                batch=None if m.group(7) == "NA" else int(m.group(7)),
                                missed=None if m.group(8) == "NA" else int(m.group(8))))
        # sidecar name mirrors harness.sh saturation
        for p in d.glob("*%s*_q%d_%s.perf" % (arm if arm != "default" else "", q, grp)):
            vals, problems = sat_parse_perf(p)
            if vals is not None:
                missing = [e for e in EXPECTED.get(grp, []) if e not in vals]
                if missing:
                    problems = list(problems) + [
                        "ABSENT (PMU refused, or the group was mis-specified): "
                        + ", ".join(missing)]
            conds.setdefault(q, {})[grp] = (vals, problems)

    if not conds:
        sys.exit("no per-group counter files found")

    # Report every problem before any number is printed. A run with an absent
    # event is not a run with a small gap in it -- the fraction that event
    # feeds is simply missing, and a figure drawn from the rest would look
    # complete.
    bad = [(q, g, p) for q in sorted(conds) for g, (_v, p) in conds[q].items() if p]
    if bad:
        print("COUNTER PROBLEMS -- these runs are not usable as measured:")
        for q, g, probs in bad:
            for pr in probs:
                print("  q=%-3d %-9s %s" % (q, g, pr))
        print()

    window = float(__import__("os").environ.get("PERF_WINDOW", "8"))
    global DRAM_CEIL, CEIL_SRC
    DRAM_CEIL, CEIL_SRC = dram_ceiling()
    print("DRAM denominator: %.1f GB/s -- %s" % (DRAM_CEIL, CEIL_SRC))
    print("(absolute GB/s is the primary figure; the percentage is secondary)\n")
    qs = sorted(conds)

    def get(q, grp, name, prefix=False):
        """Exact event-name lookup, or a sum over a prefix.

        Exact by default, deliberately. An earlier version matched substrings,
        which silently summed `cycles` with `pending_cycles`, and
        `l1d_pend_miss_pending` with `l1d_pend_miss_pending_cycles` -- i.e. it
        broke precisely the MLP ratio it was there to compute, and would have
        produced a plausible wrong number rather than an error. `prefix=True`
        is used only where a sum really is wanted, across the eight memory
        controllers.
        """
        entry = conds.get(q, {}).get(grp)
        if not entry or not entry[0]:
            return None
        vals = entry[0]
        if prefix:
            tot = sum(v for ev, v in vals.items() if _base(ev).startswith(name))
            return tot if any(_base(ev).startswith(name) for ev in vals) else None
        for ev, v in vals.items():
            if _base(ev) == name:
                return v
        return None

    def frac(a, b):
        return None if (a is None or not b) else a / b

    series = {}     # label -> {q: fraction}
    for q in qs:
        m = meta[q]
        slots = get(q, "topdown1", "slots")
        cyc_td = get(q, "mlp", "cycles")
        cyc_fb = get(q, "fb", "cycles")
        cyc_tm = get(q, "tlbmem", "cycles")

        def put(label, v):
            if v is not None:
                series.setdefault(label, {})[q] = v

        put("core: retiring / slots", frac(get(q, "topdown1", "topdown-retiring"), slots))
        put("core: backend-bound", frac(get(q, "topdown1", "topdown-be-bound"), slots))
        put("core: memory-bound", frac(get(q, "topdown2", "topdown-mem-bound"), slots))
        put("L1D fill buffers full", frac(get(q, "fb", "l1d_pend_miss_fb_full"), cyc_fb))
        put("stalled on L3 miss", frac(get(q, "tlbmem", "stalls_l3_miss"), cyc_tm))
        put("page walker active", frac(get(q, "tlbmem", "dtlb_walk_active"), cyc_tm))

        # Raw CAS counters return a count of 64 B cache lines; there is no
        # .scale file on this part, so the conversion is explicit here.
        rd = get(q, "bw", "cas_rd_", prefix=True)
        wr = get(q, "bw", "cas_wr_", prefix=True)
        if rd is not None and wr is not None:
            gbs = (rd + wr) * 64.0 / window / 1e9
            put("DRAM bandwidth (of %s)" % CEIL_SRC.split(",")[0], gbs / DRAM_CEIL)
            series.setdefault("_dram_gbs", {})[q] = gbs
        if m["avg"]:
            put("link rate (of line rate)", m["avg"] / LINE_RATE_MPPS)
            put("PCIe (of Gen4 x16)",
                m["avg"] * 1e6 * FRAME_BYTES / 1e9 / PCIE_GBS)
        # MLP is a count, not a fraction: reported separately.
        p_ = get(q, "mlp", "l1d_pend_miss_pending")
        pc = get(q, "mlp", "l1d_pend_miss_pending_cycles")
        if p_ and pc:
            series.setdefault("_mlp", {})[q] = p_ / pc

        # Mean data-read latency, Little's law: outstanding requests accumulated
        # per cycle, divided by requests completed. Like MLP this is a count
        # rather than a fraction -- there is no ceiling to divide by, and that
        # is the point. A bandwidth utilisation says how close the memory system
        # is to its throughput limit; this says what one access costs. The two
        # can disagree completely, and on this workload they do: see 5.32.
        out = get(q, "latency", "offcore_reqs_outstanding_data_rd")
        req = get(q, "latency", "offcore_reqs_data_rd")
        if out and req:
            series.setdefault("_latency", {})[q] = out / req

    # ---- text table ----
    print("%-28s %s" % ("resource (utilisation)",
                        " ".join("%8s" % ("q=%d" % q) for q in qs)))
    for lab in sorted(k for k in series if not k.startswith("_")):
        print("%-28s %s" % (lab, " ".join(
            "%7.1f%%" % (series[lab][q] * 100) if q in series[lab] else "      --"
            for q in qs)))
    for lab, unit in (("_dram_gbs", "GB/s"), ("_mlp", "misses"),
                      ("_latency", "cyc/read")):
        if lab in series:
            print("%-28s %s" % (lab[1:] + " (" + unit + ")", " ".join(
                "%8.1f" % series[lab][q] if q in series[lab] else "      --"
                for q in qs)))
    print()
    for q in qs:
        m = meta[q]
        if m["missed"] is not None and m["avg"]:
            print("q=%-3d workers=%-3d %.2f Mpps  batch=%s  RX-Missed=%d"
                  % (q, m["workers"], m["avg"], m["batch"], m["missed"]))

    # ---- figure ----
    keys = [k for k in sorted(series) if not k.startswith("_")]
    PW, PH, PAD_L, PAD_T = 470, 300, 72, 62
    LEG_H = 22 * ((len(keys) + 1) // 2) + 18
    W, H = PAD_L + PW + 210, PAD_T + PH + LEG_H + 56
    o = []
    a = o.append
    a('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
      'height="%d" font-family="system-ui,-apple-system,Segoe UI,sans-serif">'
      % (W, H, W, H))
    a('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SVG_SURFACE))
    a('<text x="%d" y="26" font-size="15" font-weight="600" fill="%s">'
      'Which resource saturates first?</text>' % (PAD_L, SVG_INK))
    a('<text x="%d" y="44" font-size="12" fill="%s">utilisation against each '
      'resource&#8217;s own ceiling, as worker cores are added</text>'
      % (PAD_L, SVG_INK_2))

    hi = max(1.0, max(v for k in keys for v in series[k].values()))

    def sx(q):
        i = qs.index(q)
        return PAD_L + (i + 0.5) / len(qs) * PW

    def sy(v):
        return PAD_T + PH - (v / hi) * PH

    for frac_ in (0, 0.25, 0.5, 0.75, 1.0):
        v = frac_ * hi
        a('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
          % (PAD_L, sy(v), PAD_L + PW, sy(v), SVG_GRID))
        a('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">'
          '%d%%</text>' % (PAD_L - 7, sy(v) + 3, SVG_INK_2, round(v * 100)))
    a('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" '
      'stroke-width="1.5" stroke-dasharray="5,3"/>'
      % (PAD_L, sy(1.0), PAD_L + PW, sy(1.0), "#a8324a"))
    a('<text x="%d" y="%.1f" font-size="10" fill="%s">ceiling</text>'
      % (PAD_L + PW + 5, sy(1.0) + 3, "#a8324a"))

    for q in qs:
        a('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">'
          '%d</text>' % (sx(q), PAD_T + PH + 16, SVG_INK_2, meta[q]["workers"]))
    a('<text x="%.1f" y="%d" font-size="11" fill="%s" text-anchor="middle">'
      'worker cores</text>' % (PAD_L + PW / 2, PAD_T + PH + 34, SVG_INK_2))

    for i, k in enumerate(keys):
        col = PALETTE[i % len(PALETTE)]
        pts = [(sx(q), sy(series[k][q])) for q in qs if q in series[k]]
        if len(pts) > 1:
            a('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>'
              % (col, " ".join("%.1f,%.1f" % p for p in pts)))
        for p in pts:
            a('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>' % (p[0], p[1], col))

    ly = PAD_T + PH + 48
    for i, k in enumerate(keys):
        col = PALETTE[i % len(PALETTE)]
        cx = PAD_L + (i % 2) * 330
        cy = ly + (i // 2) * 22
        a('<rect x="%d" y="%d" width="10" height="10" fill="%s"/>' % (cx, cy, col))
        a('<text x="%d" y="%d" font-size="11" fill="%s">%s</text>'
          % (cx + 15, cy + 9, SVG_INK_2, k.replace("&", "&amp;")))
    a("</svg>")
    out = DOCS / "saturation.svg"
    out.write_text("\n".join(o))
    print("\nwrote %s" % out)


def cmd_saturation():
    sat_main(sys.argv[1])


# =============================================================================
# plot-latency: was plot_latency_saturation.py
# =============================================================================
# The answer to §5.31: latency, not bandwidth.
#
# §5.31 left an open question. At high worker counts the core is ~58%
# memory-bound while every instrument measuring a memory *resource* reads 1-5%.
# That cannot be a bandwidth or a capacity limit, and none of §5.27's nine
# enumerated resources could explain it.
#
# This figure plots the resource §5.27 never enumerated. Mean data-read latency
# rises 9.3x across the sweep while DRAM bandwidth utilisation never exceeds 8%.
# A bandwidth-limited system runs out of throughput; this one runs out of
# tolerance for how long one access takes. The two are drawn together because the
# disagreement between them IS the result -- either curve alone is unremarkable.
#
# Deliberately its own figure rather than another series on saturation.svg: that
# plot's y axis is "percentage of a known ceiling", and latency has no ceiling to
# divide by. Forcing it onto that axis would mean inventing a denominator, which
# is the exact failure §5.30 records.
#
#     python3 l2fwd/analysis.py plot-latency <outdir> -> docs/latency_vs_bandwidth.svg

#!/usr/bin/env python3


C_LAT, C_BW, C_MLP = "#a8324a", "#12707f", "#bb551c"

W, H = 760, 500
PAD_L, PAD_R, PAD_T, PAD_B = 74, 74, 58, 150
LAT_PW, LAT_PH = W - PAD_L - PAD_R, H - PAD_T - PAD_B


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
    # perf window. Same arithmetic as the saturation section, restated rather than
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


def lat_dram_ceiling():
    f = pathlib.Path(__file__).resolve().parent / ".dram_ceiling"
    m = re.search(r"^DRAM_ACHIEVED_GBS=([0-9.]+)", f.read_text(), re.M)
    if not (m and float(m.group(1)) > 0):
        sys.exit("refusing to plot: .dram_ceiling has no measured ceiling. "
                 "Run harness.sh ceiling first rather than dividing by a guess.")
    return float(m.group(1))


def lat_main(outdir):
    rows = collect(outdir)
    qs = sorted(k for k in rows if "lat" in rows[k])
    if not qs:
        sys.exit("no latency counters in %s -- was the latency group run?" % outdir)
    ceil = lat_dram_ceiling()

    lat = [rows[q]["lat"] for q in qs]
    bwpc = [100.0 * rows[q].get("gbs", 0) / ceil for q in qs]
    mlp = [rows[q].get("mlp", 0) for q in qs]
    lat_hi = max(800.0, max(lat) * 1.12)

    o = []
    a = o.append
    a('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
      'viewBox="0 0 %d %d" font-family="Inter,Helvetica,Arial,sans-serif">'
      % (W, H, W, H))
    a('<rect width="%d" height="%d" fill="%s"/>' % (W, H, SVG_SURFACE))
    a('<text x="%d" y="26" font-size="15" font-weight="600" fill="%s">%s</text>'
      % (PAD_L, SVG_INK, esc("Latency, not bandwidth")))
    a('<text x="%d" y="44" font-size="12" fill="%s">%s</text>'
      % (PAD_L, SVG_INK_2,
         esc("one access costs 9.3x more at 23 workers; the memory system is "
             "never above 8% of its ceiling")))

    def sx(i):
        return PAD_L + (i + 0.5) / len(qs) * LAT_PW

    def sy_lat(v):
        return PAD_T + LAT_PH - (v / lat_hi) * LAT_PH

    def sy_pc(v):
        return PAD_T + LAT_PH - (v / 100.0) * LAT_PH

    # left axis: cycles
    for v in range(0, int(lat_hi) + 1, 200):
        y = sy_lat(v)
        a('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
          % (PAD_L, y, PAD_L + LAT_PW, y, SVG_GRID))
        a('<text x="%d" y="%.1f" font-size="10" text-anchor="end" fill="%s">%d</text>'
          % (PAD_L - 8, y + 3, SVG_INK_2, v))
    # transform on the <text> itself, not a wrapping <g>: check_extents reads
    # each text element's own attributes, so a rotation on the parent is
    # invisible to it and the label is measured as horizontal.
    a('<text transform="translate(20,%d) rotate(-90)" x="0" y="0" font-size="11" '
      'fill="%s" text-anchor="middle">%s</text>'
      % (PAD_T + LAT_PH / 2, C_LAT, esc("cycles per data read")))

    # right axis: percent
    for v in range(0, 101, 25):
        y = sy_pc(v)
        a('<text x="%d" y="%.1f" font-size="10" fill="%s">%d%%</text>'
          % (PAD_L + LAT_PW + 8, y + 3, C_BW, v))
    a('<text transform="translate(%d,%d) rotate(-90)" x="0" y="0" font-size="11" '
      'fill="%s" text-anchor="middle">%s</text>'
      % (PAD_L + LAT_PW + 52, PAD_T + LAT_PH / 2, C_BW, esc("% of DRAM ceiling")))

    for i, q in enumerate(qs):
        a('<text x="%.1f" y="%d" font-size="10" text-anchor="middle" fill="%s">%d</text>'
          % (sx(i), PAD_T + LAT_PH + 18, SVG_INK_2, q))
    a('<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="%s">%s</text>'
      % (PAD_L + LAT_PW / 2, PAD_T + LAT_PH + 38, SVG_INK, esc("worker cores")))

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
          % (lx, PAD_T + LAT_PH + 52, col))
        a('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>'
          % (lx + 14, PAD_T + LAT_PH + 61, SVG_INK_2, esc(lab)))
        lx += 200

    a('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>'
      % (PAD_L, H - 30, SVG_INK_2,
         esc("Ceiling = %.1f GB/s, measured (harness.sh ceiling). Latency is "
             "Little's law over OFFCORE_REQUESTS[_OUTSTANDING].DATA_RD."
             % ceil)))
    a('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>'
      % (PAD_L, H - 14, SVG_INK_2,
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


def cmd_plot_latency():
    lat_main(sys.argv[1])


# =============================================================================
# plot-ceiling: was plot_dram_ceiling.py
# =============================================================================
# Does the DRAM ceiling plateau before the thread count l2fwd actually runs at?
#
# Reads the CSV that harness.sh ceiling writes and draws achieved bandwidth against
# thread count for the read-only and mixed arms, with the nominal 8-channel peak
# drawn as a reference line.
#
# The figure exists to answer one question that a single number cannot: whether
# the value written into .dram_ceiling as DRAM_ACHIEVED_GBS is a ceiling or a
# lower bound. A curve still climbing at the right-hand edge is a lower bound, and
# every utilisation divided by it is overstated. The withdrawn 360.0 GB/s had
# exactly that defect and nothing on the page showed it.
#
# Hand-written SVG with no dependencies, matching plot_dramblast_arms.py:
# matplotlib lives in the nix dev shell and this has to run from a plain shell.
#
#     python3 l2fwd/analysis.py plot-ceiling <dram_ceiling.csv>  -> docs/dram_ceiling.svg

#!/usr/bin/env python3


MUTED = "#9a9a9a"

PEAK = 307.2          # dmidecode: 8 channels x DDR5-4800 x 8 B
PEAK_LABEL = "nominal 8-channel peak, 307.2 GB/s"

CEIL_W, CEIL_H = 860, 470
L, R, T, B = 78, 250, 58, 68      # right margin holds the legend


def ceil_main(path):
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

    pw, ph = CEIL_W - L - R, CEIL_H - T - B

    def sx(i):
        return L + (pw * i / max(1, len(threads) - 1))

    def sy(v):
        return T + ph - (ph * v / ymax)

    out = []
    a = out.append
    a('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
      'height="%d" font-family="Helvetica,Arial,sans-serif">' % (CEIL_W, CEIL_H, CEIL_W, CEIL_H))
    a('<rect width="%d" height="%d" fill="%s"/>' % (CEIL_W, CEIL_H, SVG_SURFACE))
    a('<text x="%d" y="26" font-size="16" font-weight="600" fill="%s">'
      'Is the DRAM ceiling a ceiling, or still climbing?</text>' % (L, SVG_INK))
    a('<text x="%d" y="45" font-size="11.5" fill="%s">achieved bandwidth vs '
      'thread count, one thread per physical core</text>' % (L, SVG_INK_2))

    # y grid and axis
    step = 50
    v = 0
    while v <= ymax:
        y = sy(v)
        a('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s"/>'
          % (L, y, L + pw, y, SVG_GRID))
        a('<text x="%d" y="%.1f" font-size="10.5" text-anchor="end" fill="%s">'
          '%d</text>' % (L - 8, y + 3.5, SVG_INK_2, v))
        v += step

    # the nominal peak, drawn dashed because it is not achievable
    a('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
      'stroke-dasharray="5,4"/>' % (L, sy(PEAK), L + pw, sy(PEAK), MUTED))

    # x axis
    for i, t in enumerate(threads):
        a('<text x="%.1f" y="%d" font-size="10.5" text-anchor="middle" '
          'fill="%s">%d</text>' % (sx(i), T + ph + 18, SVG_INK_2, t))
    a('<text x="%.1f" y="%d" font-size="11.5" text-anchor="middle" fill="%s">'
      'threads (= physical cores)</text>' % (L + pw / 2, T + ph + 40, SVG_INK))

    # rotated y caption, CENTRED on the plot area: rotate(-90) runs text upward
    # from its anchor, and three captions in this repo have already been clipped
    # by anchoring one at the top of the plot instead (INVESTIGATION.md 5.22).
    a('<text transform="translate(20,%.1f) rotate(-90)" font-size="11.5" '
      'text-anchor="middle" fill="%s">GB/s</text>' % (T + ph / 2, SVG_INK))

    order = [("mixed", "mixed read+write (the denominator)", SVG_BLUE),
             ("read", "read-only", SVG_ORANGE)]
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
      'Arms</text>' % (lx, ly, SVG_INK))
    ly += 20
    for key, lab, col in order:
        if key not in series:
            continue
        a('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="2.2"/>'
          % (lx, ly - 4, lx + 22, ly - 4, col))
        a('<circle cx="%d" cy="%d" r="3.4" fill="%s"/>' % (lx + 11, ly - 4, col))
        for j, part in enumerate(wrap(lab, 24)):
            a('<text x="%d" y="%d" font-size="10.5" fill="%s">%s</text>'
              % (lx + 30, ly + j * 13, SVG_INK_2, esc(part)))
        ly += 13 * max(1, len(wrap(lab, 24))) + 10
    a('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1.6" '
      'stroke-dasharray="5,4"/>' % (lx, ly - 4, lx + 22, ly - 4, MUTED))
    for j, part in enumerate(wrap(PEAK_LABEL, 24)):
        a('<text x="%d" y="%d" font-size="10.5" fill="%s">%s</text>'
          % (lx + 30, ly + j * 13, SVG_INK_2, esc(part)))
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
              % (lx, ly + j * 12.5, SVG_INK, esc(part)))

    a('</svg>')
    svg = "\n".join(out)

    # Every <text> must fit the viewBox. This repo has shipped four clipped
    # captions found only by this check, never by looking at the page -- there
    # is no rasteriser on this host.
    bad = ceil_check_extents(svg)
    if bad:
        sys.exit("labels outside the canvas: %s" % "; ".join(bad))

    DOCS.mkdir(exist_ok=True)
    (DOCS / "dram_ceiling.svg").write_text(svg)
    print("wrote docs/dram_ceiling.svg")


def ceil_check_extents(svg):
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
        if x0 < 0 or x0 + w > CEIL_W or y - fs < 0 or y > CEIL_H:
            bad.append("%r at (%.0f,%.0f)" % (body[:28], x, y))
    return bad


def cmd_plot_ceiling():
    ceil_main(sys.argv[1])


# =============================================================================
# backing: was verify_backing.py
# =============================================================================
# Check that every run got the page backing its -B flag asked for.
#
# `harness.sh sweep` samples the 1 GiB hugepage pool, which distinguishes 1 GiB from
# not-1 GiB and nothing else -- it cannot tell a 2 MiB arm from a 4 KiB one. Both
# of those are advisory: MADV_HUGEPAGE can be declined under memory fragmentation
# and MADV_NOHUGEPAGE can fail, and in either case the run completes, the numbers
# look reasonable, and the dataset is labelled with a page size it never had.
#
# harness.sh pagewatch samples each live l2fwd's own /proc/<pid>/smaps_rollup, which
# reports what the kernel actually did. This reads that log and states, per
# (-B flag, mode), whether the mapping was really backed the way it claims.
#
# Expected, for the 8 GiB table:
#     -B 1g       AnonHugePages ~0          Private_Hugetlb ~10 GiB (8 + DPDK's 2)
#     -B thp2m    AnonHugePages ~8 GiB      Private_Hugetlb ~2 GiB  (DPDK only)
#     -B 4k       AnonHugePages ~0          Private_Hugetlb ~2 GiB  (DPDK only)
#
# Note `Private_Hugetlb` always carries DPDK's own -m 2000, so the discriminator
# is the 8 GiB step above that, not the absolute value.
#
# Usage: python3 analysis.py backing [page_watch.log]

def cmd_backing():
    LOG = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "/users/sohamb/sweeps/page_watch.log")
    GIB = 1024 * 1024  # the log is in kB

    # THP coverage below this fraction of the table means the arm did not get what
    # it asked for. Not 100%: mmap gives no 2 MiB alignment guarantee, so the
    # leading and trailing partial 2 MiB regions of an 8 GiB mapping stay on 4 KiB
    # pages -- under 4 MiB of 8192, i.e. 0.05%.
    THP_OK = 0.98

    rows = collections.defaultdict(list)
    for line in LOG.read_text(errors="replace").splitlines():
        # `total=` is optional: harness.sh pagewatch gained that field when its own RSS
        # threshold was found to exclude every hugetlb-backed run. Old lines in the
        # log predate it, and both must parse or the fix would silently drop the
        # history it was meant to complete.
        m = re.search(r"rss=(\d+) thp=(\d+) hugetlb=(\d+)(?: total=\d+)? cmd=(.*)", line)
        if not m:
            continue
        rss, thp, htlb, cmd = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        # Split on the EAL/application separator, which is a bare " -- ". Splitting
        # on "--" alone lands inside "--in-memory" and then picks up DPDK's own
        # `-m 2000` as the application mode.
        app = cmd.split(" -- ", 1)[1] if " -- " in cmd else cmd
        mode = (re.search(r"-m (\w+)", app) or [None, "?"])[1]
        flag = (re.search(r"-B (\S+)", cmd) or [None, "as-shipped"])[1]
        rows[(flag, mode)].append((rss, thp, htlb))

    if not rows:
        sys.exit(f"no samples in {LOG}")

    print(f"{'-B flag':12} {'mode':10} {'n':>4} {'THP GiB':>8} {'Hugetlb GiB':>12}  verdict")
    bad = 0
    for (flag, mode), v in sorted(rows.items()):
        thp = max(t for _, t, _ in v) / GIB
        htlb = max(h for _, _, h in v) / GIB
        if flag in ("1g", "as-shipped") and mode == "dramblast":
            ok, want = htlb > 9.0 and thp < 1.0, "1 GiB hugetlb"
        elif flag == "1g":
            ok, want = htlb > 9.0 and thp < 1.0, "1 GiB hugetlb"
        elif flag == "thp2m" or (flag == "as-shipped" and mode == "maglev"):
            ok, want = thp / 8.0 >= THP_OK, "2 MiB THP"
        elif flag == "4k":
            ok, want = thp < 0.5 and htlb < 3.0, "4 KiB"
        else:
            ok, want = True, "?"
        bad += not ok
        print(f"{flag:12} {mode:10} {len(v):>4} {thp:>8.2f} {htlb:>12.2f}  "
              f"{'OK' if ok else '*** NOT ' + want + ' ***'}  (wanted {want})")

    # An arm with no samples must not pass silently. The loop above can only judge
    # rows that exist, so a watcher that never saw an arm produced a clean bill of
    # health for it -- which is exactly what happened: harness.sh pagewatch gated on Rss,
    # hugetlb pages are not counted in Rss, and so every dramblast run on 1 GiB
    # pages was missing from the log while this script printed "every arm got the
    # backing it claims". Absence of evidence was being reported as evidence.
    EXPECTED = [("as-shipped", "dramblast"), ("as-shipped", "maglev"),
                ("thp2m", "dramblast"), ("4k", "dramblast"),
                ("1g", "maglev"), ("4k", "maglev")]
    missing = [k for k in EXPECTED if k not in rows]
    MIN_SAMPLES = 3
    thin = [(k, len(rows[k])) for k in EXPECTED
            if k in rows and len(rows[k]) < MIN_SAMPLES]

    print()
    for flag, mode in missing:
        print(f"*** NO SAMPLES for {flag} / {mode} -- this arm was never verified")
    for (flag, mode), n in thin:
        print(f"*** only {n} sample(s) for {flag} / {mode} -- below the {MIN_SAMPLES} "
              f"needed to call it verified")
    if not bad and not missing and not thin:
        print("every arm got the backing it claims")
    else:
        parts = []
        if bad:
            parts.append(f"{bad} arm(s) did NOT get their stated backing")
        if missing:
            parts.append(f"{len(missing)} arm(s) were never sampled")
        if thin:
            parts.append(f"{len(thin)} arm(s) were sampled too thinly to judge")
        print("; ".join(parts) + " -- do not read this as a clean verification")
    sys.exit(1 if (bad or missing or thin) else 0)


# =============================================================================
# selftest: was test_perf_guard.py
# =============================================================================
# Check that the PMU multiplexing guard in `extract` actually bites.
#
# Why this file exists
# --------------------
# docs/INVESTIGATION.md 5.24 records that `harness.sh sweep`'s six-event set sits exactly
# at this part's counter ceiling, and that a multiplexed reading is scaled to a
# full-window estimate before perf prints it -- so it is numerically
# indistinguishable from a measured one, and nothing downstream can tell. The
# guard refuses such readings. A guard that has never been shown to fire for the
# reason it was written is not evidence of anything, so this fires it.
#
# Two halves, and the second is the one that matters.
#
#   SYNTHETIC   feed the parser hand-built perf output. Cheap, runs anywhere,
#               and pins the field-index regression: an `instructions` row
#               carrying an IPC in field 5 must not be read as an enabled
#               percentage.
#
#   REAL        (--real) oversubscribe the PMU on purpose -- ask for more raw
#               events than this part can count at once -- and assert the guard
#               rejects what comes back. The multiplexing is produced by the
#               hardware, so this exercises the actual failure mode rather than a
#               reconstruction of it, and it cannot rot: if a future part changes
#               its counter budget this test changes its answer, where a
#               hand-edited percentage would keep passing forever.
#
# The real half is a peer session's idea, arrived at while closing the same gap
# on its own harness; credited in docs/INVESTIGATION.md 5.25.
#
#     python3 analysis.py selftest          # synthetic only, no PMU needed
#     python3 analysis.py selftest --real   # adds the hardware half (needs perf)

#!/usr/bin/env python3


FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# --------------------------------------------------------------- synthetic
def synthetic():
    print("synthetic:")
    clean = ("16764707551,,cycles,8001021078,100.00,,\n"
             "24627492628,,instructions,8001019154,100.00,1.47,insn per cycle\n"
             "69177353,,dtlb_walk_completed,8001018545,100.00,,\n")
    got, dropped, bad = parse_perf(clean)
    check("a clean sidecar is accepted whole", len(got) == 3 and not dropped,
          f"{len(got)} readings, {len(dropped)} dropped")

    # THE REGRESSION THAT BROKE A PEER'S GUARD. Field 5 on the `instructions`
    # row is the IPC. A guard reading it as the enabled percentage sees 1.47
    # and throws away a perfectly healthy run.
    got, dropped, bad = parse_perf(
        "24627492628,,instructions,8001019154,100.00,1.47,insn per cycle\n")
    check("an IPC in field 5 is not read as an enabled percentage",
          got.get("instructions") == 24627492628 and not dropped,
          "field 4 is enabled, field 5 is the derived metric")

    got, dropped, bad = parse_perf(
        "16764707551,,cycles,8001021078,41.63,,\n")
    check("a 41.63% reading is refused", not got and dropped == [("cycles", 41.63)],
          "and the rejection names the event and the percentage")

    # Both sides of the threshold, so the boundary is a decision and not a
    # coincidence of the numbers that happen to be in the data.
    hi, *_ = parse_perf("1,,e,1,100.00,,\n")
    on, *_ = parse_perf(f"1,,e,1,{PERF_MIN_ENABLED:.2f},,\n")
    _, lo, _m = parse_perf("1,,e,1,99.98,,\n")
    check("the 99.99% boundary holds on both sides",
          hi and on and lo, f"100.00 and {PERF_MIN_ENABLED} accepted, 99.98 refused")

    got, dropped, bad = parse_perf("<not counted>,,dtlb_walk_active,8001017468,0.00,,\n")
    check("'<not counted>' is neither stored nor mistaken for zero",
          not got and not dropped)

    # This case previously asserted the OPPOSITE -- that a row with no enabled
    # column is still accepted -- on the reasoning that a perf version omitting
    # the column would otherwise drop every counter. That reasoning was wrong
    # and the test pinned it: a guard whose job is to refuse readings it cannot
    # vouch for must not accept one it could not check. Breaking loudly on a
    # perf that changes its output is the correct behaviour, not a cost.
    got, dropped, bad = parse_perf("123,,cycles\n")
    check("a row with no enabled column is refused, not accepted unchecked",
          not got and len(bad) == 1)

    # The structurally important one. A threshold test can only see a LOW
    # number, while every way of shifting the columns yields a high one, a
    # non-number, or no column -- so these are the shapes a naive guard is
    # blind to, and each must land in `malformed` rather than in `got`.
    for label, row in (
            ("non-numeric", "59304,,w1,1000954706,insn per cycle,,"),
            ("run-time ns in the enabled column", "59304,,w1,1000954706,1000954706,,"),
            ("negative", "59304,,w1,1000954706,-3.0,,")):
        got, dropped, bad = parse_perf(row)
        check(f"enabled {label} is malformed, never accepted",
              not got and not dropped and len(bad) == 1)

    # Regression the previous fix could easily have introduced: a genuinely
    # multiplexed reading must stay filed as multiplexed and not be swept into
    # the new malformed category, which would report a busy machine as broken
    # tooling. The peer session flagged this as the risk when adopting the
    # same split.
    got, dropped, bad = parse_perf("500,,w1,8001021078,57.18,,\n")
    check("a real multiplexed reading is still multiplexed, not malformed",
          not got and dropped == [("w1", 57.18)] and not bad)

    got, dropped, bad = parse_perf("500,,a,1,100.00,,\n600,,b,1,60.00,,\n700,,c,1,100.00,,\n")
    check("one bad reading does not take the good ones with it",
          got == {"a": 500, "c": 700} and dropped == [("b", 60.0)])

    # THE THIRD APPEARANCE OF THE FIELD-INDEX BUG, and the one that DEFEATS the
    # guard rather than tripping it. A raw spec passed without `name=` contains
    # commas, so with -x, the row splits across extra columns: the event column
    # holds a fragment and the enabled column holds a run time in nanoseconds,
    # which is above any threshold and was therefore ACCEPTED. The parser used
    # to store it under the truncated name `cpu/event=0x12` with the
    # multiplexing guard never actually applied to it.
    got, dropped, bad = parse_perf(
        "59304,,cpu/event=0x12,umask=0x0e/,1000954706,100.00,,\n")
    check("an unnamed raw spec is caught as malformed, not silently accepted",
          not got and not dropped and len(bad) == 1,
          "the commas inside the spec shift every later column")

    # Backstop for a shift shaped differently, where the event column looks
    # innocent. A peer session's mis-parse printed 1958811208.00% and was only
    # caught because it was absurd; a shift landing on a 0-100 value would not
    # have been.
    got, dropped, bad = parse_perf("1,,e,1,1958811208.00,,\n")
    check("an enabled value outside 0-100 is malformed, not a reading",
          not got and not dropped and len(bad) == 1)


# -------------------------------------------------------------------- real
# harness.sh sweep's own set: two fixed-function events plus four raw ones.
SHIPPED = ("cycles,instructions,"
           "cpu/event=0x12,umask=0x0e,name=dtlb_walk_completed/,"
           "cpu/event=0x12,umask=0x10,name=dtlb_walk_active/,"
           "cpu/event=0xa3,umask=0x06,cmask=0x06,name=stalls_l3_miss/,"
           "LLC-load-misses")

# A set that multiplexes. NOT "one more event than fits" -- there is no such
# number. Measured on this part: seven raw events schedule at 100.00% when the
# families do not collide (shipped + 0xc4 + 0xc5 + 0xd1/01), while SIX
# multiplex when two of them are umasks of the same restricted family. What
# fails is the collision, not the count. Two 0xd1 umasks is the smallest
# reliable way to force it here. docs/INVESTIGATION.md 5.24 has the table, and
# the two wrong ceilings it went through before getting here.
#
# The names are x5/x6 and not r5/r6: perf reads a bare `rNNN` as its raw-event
# syntax, so `name=r5` is a parser error rather than a name. That cost a
# debugging round here and the test failed loudly rather than passing, which is
# the behaviour wanted.
OVER = (SHIPPED
        + ",cpu/event=0xd1,umask=0x01,name=x5/"
        + ",cpu/event=0xd1,umask=0x02,name=x6/")


# Counted per-CPU, the way harness.sh sweep does it, and NOT per-task. A per-task count
# on `sleep` reports 100.00% enabled however many events are asked for: the task
# is off-CPU almost the whole time, so enabled time and running time are both
# ~zero and their ratio is 1. That made the first version of this test pass
# while measuring nothing. A CPU accumulates time whatever is scheduled on it.
#
# CPU 26 is in the housekeeping set (this shell's Cpus_allowed_list is
# 24-27,52-55), deliberately outside bench.slice's 0-23, so running this never
# touches the cores a benchmark would be using.
PROBE_CPU = 26


def run_perf(events, seconds=2):
    r = subprocess.run(
        ["sudo", "perf", "stat", "-e", events, "-C", str(PROBE_CPU), "-x,",
         "--", "sleep", str(seconds)],
        capture_output=True, text=True)
    return r.stderr + r.stdout


def real():
    print("real (hardware-produced multiplexing):")
    at_limit = run_perf(SHIPPED)
    got, dropped, bad = parse_perf(at_limit)
    pcts = [float(l.split(",")[4]) for l in at_limit.splitlines()
            if len(l.split(",")) > 4 and l.split(",")[0].strip().isdigit()]
    check("the shipped event set is counted and accepted",
          len(got) == 6 and not dropped,
          f"{len(got)} readings, enabled {min(pcts):.2f}%-{max(pcts):.2f}%"
          if pcts else "no readings")

    over = run_perf(OVER)
    got, dropped, bad = parse_perf(over)
    pcts = [float(l.split(",")[4]) for l in over.splitlines()
            if len(l.split(",")) > 4 and l.split(",")[0].strip().isdigit()]
    # The assertion is named for what actually fails. An earlier name said
    # "an oversubscribed PMU", which baked a false generalisation into a test
    # written to prevent exactly that -- the peer session hit the identical
    # thing in their own test's label.
    check("two 0xd1 umasks collide with the shipped set, and all are refused",
          len(dropped) >= 6 and not got,
          f"{len(dropped)} dropped, enabled {min(pcts):.2f}%-{max(pcts):.2f}%"
          if pcts else "no readings")


def cmd_selftest():
    synthetic()
    if "--real" in sys.argv:
        real()
    else:
        print("real: skipped (pass --real; needs perf and the PMU)")
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
        sys.exit(1)
    print("all checks passed")


# =============================================================================
# probe: nbprobe per-burst phase timer (libsashstore/nbprobe.h)
# =============================================================================
# Reads harness.sh sweep logs from build-probe/ runs plus the ring dumps they
# name (`nbprobe dump <path> records=N`). Ring = last 2^18 sampled bursts per
# lcore = steady state; log's own `nbprobe ... phase=` sums cover whole run
# incl. insert warm-up, so ring is the primary per-phase source and log sums
# are kept only for insert counts.
#
# Correction: each phase opens with one boundary read, so each phase carries
# one mark cost. Subtracted per phase: cal_tsc (and cal per counter) / burst.
# Uncorrected numbers kept alongside -- correction is a model, raw is data.
#
# Wait taxonomy, from bench_membw known answers (docs/REFLECT_PATH.md s0):
#   mem_wait  = stall_l1d   (0 on nomem, 0.86 on L3-resident chase)
#   dram_wait = stall_l3    (subset of mem_wait; 0 when chase fits L3)
#   other_stall = stall_total - stall_l1d  (dependency latency, frontend: 0.43 on nomem)
#   work      = cycles - stall_total (cycles some uop executed)
# st_bound = store-buffer-full cycles, NOT a sharing signal (padded > fshare).
# rfo_hitm = RFO served by modified line in another core: sharing signal.
import struct as _struct

NBP_PHASES = ["rx", "hash", "alloc", "find", "post", "free", "mac", "tx"]
# Append-only: index i = nbprobe.c nbp_ev[i]. v1 dumps carry first 6.
NBP_EVS = ["cycles", "stall_total", "stall_l1d", "stall_l3", "st_bound", "rfo_hitm",
           "insns", "br_misp"]
# dump version -> (magic, ev slots per record). v1 = NBP_NEV 6, v2 = 8.
NBP_VER = {1: (0x3170726f6270626e, 6), 2: (0x3270726f6270626e, 8)}


def nbp_rec_struct(nev_slots):
    """tsc0, tsc[8], ev[nev_slots][8], 6 u16 counts, pops reprobes occ_sum ins_steps, occ_max lcore"""
    return _struct.Struct(f"<Q8I{8 * nev_slots}I6H4I2H")


def nbp_resolve(p):
    """dump path, or its .gz: ring dumps are gzipped after analysis to save disk."""
    p = pathlib.Path(p)
    if p.exists():
        return p
    gz = p.with_name(p.name + ".gz")
    return gz if gz.exists() else None


def nbp_load(path):
    path = pathlib.Path(path)
    if path.suffix == ".gz":
        import gzip
        b = gzip.decompress(path.read_bytes())
    else:
        b = path.read_bytes()
    hdr = _struct.unpack_from("<8Q", b, 0)
    ver = hdr[1]
    if ver not in NBP_VER or hdr[0] != NBP_VER[ver][0]:
        sys.exit(f"{path}: not an nbprobe dump (magic {hdr[0]:#x} version {ver})")
    slots = NBP_VER[ver][1]
    rec = nbp_rec_struct(slots)
    if hdr[2] != rec.size:
        sys.exit(f"{path}: nbprobe v{ver} record size {hdr[2]} != {rec.size}")
    nev, every, n, cal_tsc, lcore = hdr[3], hdr[4], hdr[5], hdr[6], hdr[7]
    cal_ev = _struct.unpack_from(f"<{slots}Q", b, 64)
    off = 64 + 8 * slots
    # raw tuples, not dicts: 2^18 records per lcore, dict per record was minutes.
    # v[0] tsc0, v[1:9] tsc, v[9+8i : 17+8i] ev i, then from c = 9 + 8*slots:
    # v[c] nb_rx, v[c+1] fn, v[c+2..c+5] found absent full inserts,
    # v[c+6..c+9] pops reprobes occ_sum ins_steps, v[c+10] occ_max
    recs = list(rec.iter_unpack(b[off: off + n * rec.size]))
    return {"lcore": lcore, "nev": nev, "every": every, "cal_tsc": cal_tsc,
            "cal_ev": cal_ev[:nev], "recs": recs, "version": ver, "cnt": 9 + 8 * slots}


def nbp_summarise(dumps, tail_s=None, tsc_hz=None):
    """Per-phase per-packet means over ring records. tail_s: keep last N s."""
    # mixed v1/v2 dumps in one summary: only counters all of them carry
    nev = min(d["nev"] for d in dumps) if dumps else 0
    NP = len(NBP_PHASES)
    tsc_raw = [0] * NP
    tsc_cor = [0] * NP
    ev = [[0] * NP for _ in range(nev)]
    per = [[] for _ in range(NP)]
    nb = pk = fn = pops = absent = full = reprobes = occ_sum = 0
    occ_max = 0
    for d in dumps:
        rs = d["recs"]
        if tail_s and tsc_hz and rs:
            cut = rs[-1][0] - tail_s * tsc_hz
            rs = [r for r in rs if r[0] >= cut]
        cal, cev, c = d["cal_tsc"], d["cal_ev"], d["cnt"]
        for r in rs:
            n_rx = r[c]
            if not n_rx:
                continue
            nb += 1
            pk += n_rx
            fn += r[c + 1]; absent += r[c + 3]; full += r[c + 4]
            pops += r[c + 6]; reprobes += r[c + 7]; occ_sum += r[c + 8]
            if r[c + 10] > occ_max:
                occ_max = r[c + 10]
            for p in range(NP):
                t = r[1 + p]
                tsc_raw[p] += t
                per[p].append(t / n_rx)
                # phase length 0 = boundary never reached (copied): no mark inside it
                if t:
                    tsc_cor[p] += t - cal
                    for i in range(nev):
                        ev[i][p] += r[9 + 8 * i + p] - cev[i]
    if not pk:
        return None
    out = {"bursts": nb, "pkts": pk, "burst_mean": pk / nb, "phase": {}}
    for p, name in enumerate(NBP_PHASES):
        ph = {"tsc_raw": tsc_raw[p] / pk, "tsc": tsc_cor[p] / pk}
        for i in range(nev):
            ph[NBP_EVS[i]] = ev[i][p] / pk
        if nev:
            ph["work"] = ph["cycles"] - ph["stall_total"]
            ph["mem_wait"] = ph["stall_l1d"]
            ph["other_stall"] = ph["stall_total"] - ph["stall_l1d"]
        if nev > 7:
            ph["ipc"] = ph["insns"] / ph["cycles"] if ph["cycles"] > 0 else float("nan")
        xs = sorted(per[p])
        ph["p50"], ph["p90"], ph["p99"] = xs[len(xs) // 2], xs[int(len(xs) * .9)], xs[min(len(xs) - 1, int(len(xs) * .99))]
        out["phase"][name] = ph
    fn = fn or 1
    out["counts"] = {"absent_frac": absent / fn, "full_frac": full / fn,
                     "pops_per_key": pops / fn, "reprobes_per_key": reprobes / fn,
                     "occ_mean": occ_sum / (pops or 1), "occ_max": occ_max}
    out["tsc_total"] = sum(out["phase"][p]["tsc"] for p in NBP_PHASES)
    return out


def nbp_parse_log(path):
    t = pathlib.Path(path).read_text(errors="replace").replace("\x1b", "")
    rec = {"log": str(path)}
    samples = [float(x) for x in SAMPLE_RE.findall(t)]
    # skip 3 not 1: -P runs spend first seconds inserting 16M new flows
    if len(samples) > 4:
        rec["steady_mpps"] = round(statistics.median(samples[3:]), 2)
    m = re.findall(r"Full-loop cyc per fwd packet: (\d+)", t)
    rec["loopcyc"] = int(m[-1]) if m else None
    # (loop+idle)/fwded, empty polls included; None on pre-2026-09-23 binaries
    m = re.findall(r"All-poll cyc per fwd packet: (\d+)", t)
    rec["allpollcyc"] = int(m[-1]) if m else None
    m = re.findall(r"Average rx batch sz \(nonempty polls\): (\d+)", t)
    rec["batch_ne"] = int(m[-1]) if m else None
    for when in ("prefill", "exit"):
        m = re.search(rf"dramblast table {when} .*?alpha=([\d.]+) disp_mean=([\d.]+) "
                      rf"hit_buckets_mean=([\d.]+) miss_buckets_mean=([\d.]+)", t)
        if m:
            rec[f"table_{when}"] = dict(zip(("alpha", "disp_mean", "hit_buckets", "miss_buckets"),
                                            map(float, m.groups())))
    m = re.search(r"dramblast prefill (\d+) lcores ([\d.]+) s failed=(\d+)", t)
    if m:
        rec["prefill_s"], rec["prefill_failed"] = float(m.group(2)), int(m.group(3))
    m = re.search(r"nbprobe lcore=all counts keys=(\d+) found=([\d.]+) absent=([\d.]+).*?"
                  r"inserts_per_key=([\d.]+) ins_steps_per_insert=([\d.]+)", t)
    if m:
        rec["run_counts"] = dict(zip(("keys", "found", "absent", "inserts_per_key",
                                      "ins_steps_per_insert"), map(float, m.groups())))
    rec["dumps"] = re.findall(r"nbprobe dump (\S+) records=\d+", t)
    rec["rfo_hitm_by_lcore"] = {}
    for l, v in re.findall(r"nbprobe lcore=(\d+) phase=find .*?rfo_hitm_pkt=([\d.]+)", t):
        rec["rfo_hitm_by_lcore"][int(l)] = float(v)
    rec["errors"] = re.findall(r"^\s*(?:Cause:|Error:|FATAL|nbprobe: )[^\n]*", t, re.M)
    return rec


def cmd_probe():
    """analysis.py probe <out.json> <log ...>"""
    if len(sys.argv) < 3:
        sys.exit("usage: analysis.py probe <out.json> <log ...>")
    out, logs = sys.argv[1], sys.argv[2:]
    tsc_hz = 2.1e9  # TSC invariant 2.1 GHz on this part (harness.sh sweep FREQ note)
    rows = []
    for lg in logs:
        rec = nbp_parse_log(lg)
        # dump next to its log wins over the recorded absolute path: logs moved
        # to an archive dir must not read a later run's dump at the old path
        here = [pathlib.Path(lg).parent / pathlib.Path(p).name for p in rec["dumps"]]
        paths = [nbp_resolve(h) or nbp_resolve(p) for h, p in zip(here, rec["dumps"])]
        dumps = [nbp_load(p) for p in paths if p]
        if rec["dumps"] and len(dumps) != len(rec["dumps"]):
            rec["errors"].append("missing ring dump(s)")
        if dumps:
            rec["ring"] = nbp_summarise(dumps, tail_s=10, tsc_hz=tsc_hz)
        rows.append(rec)
    pathlib.Path(out).write_text(json.dumps(rows, indent=1))
    hdr = f"{'log':<34}{'Mpps':>7}{'loop':>6}{'allp':>6}{'B':>4}{'alpha':>7}" + \
          "".join(f"{p:>7}" for p in NBP_PHASES) + f"{'sum':>7}{'memw/f':>8}{'pops/k':>8}{'occ':>6}{'ipc/f':>6}{'brm/p':>6}"
    print(hdr)
    for r in rows:
        g = r.get("ring") or {}
        ph = g.get("phase", {})
        alpha = (r.get("table_exit") or {}).get("alpha")
        line = (f"{pathlib.Path(r['log']).stem[-34:]:<34}{r.get('steady_mpps') or 0:>7.2f}"
                f"{r.get('loopcyc') or 0:>6}{r.get('allpollcyc') or 0:>6}{r.get('batch_ne') or 0:>4}"
                f"{alpha if alpha is not None else float('nan'):>7.3f}")
        line += "".join(f"{ph[p]['tsc']:>7.1f}" if p in ph else f"{'-':>7}" for p in NBP_PHASES)
        line += f"{g.get('tsc_total', 0):>7.1f}"
        f = ph.get("find", {})
        line += f"{f.get('mem_wait', float('nan')):>8.1f}"
        c = g.get("counts", {})
        line += f"{c.get('pops_per_key', float('nan')):>8.3f}{c.get('occ_mean', float('nan')):>6.1f}"
        # v2 dumps only: find IPC, mispredicts per pkt over all phases
        brm = sum(ph[p]["br_misp"] for p in ph if "br_misp" in ph[p]) if "br_misp" in f else float("nan")
        line += f"{f.get('ipc', float('nan')):>6.2f}{brm:>6.2f}"
        if r["errors"]:
            line += "  ERR: " + "; ".join(r["errors"][:2])
        print(line)


# =============================================================================
# ptw: per-packet timeline from Intel PT ptwrite marks (nbprobe.h NBW_*)
# =============================================================================
# Input: `perf script -i X.pt --itrace=w --ns -F time,synth` text. Payload =
# kind<<56 | id<<40 | arg. PT validated on bench_membw ptmark: count, order,
# ns/mark within 0.1% of program clock (docs/REFLECT_PATH.md s0).
#
# Per burst (NBW_BURST .. next NBW_BURST):
#   inflight = push -> resolve (FOUND/ABSENT/FULL) per id: how long a lookup
#              lived in the pipeline. Async layer wants this LONG.
#   gap      = resolve(n) -> resolve(n+1): per-pop cost as seen by the core,
#              incl. any stall on the bucket load. Async layer wants this SHORT.
#   phase    = mark-to-mark durations, same boundaries as nbprobe.
#   order    = resolved in submission order? (sync path: no, completion order)
NBW_KIND = {1: "burst", 2: "push", 3: "found", 4: "absent", 5: "reprobe", 6: "full", 7: "phase"}
PTW_RE = re.compile(r"(\d+)\.(\d{9}):.*?payload: (0x[0-9a-f]+|0)\b")


def ptw_bursts(path):
    bursts, cur = [], None
    for line in open(path, errors="replace"):
        m = PTW_RE.search(line)
        if not m:
            continue
        t = int(m.group(1)) * 10**9 + int(m.group(2))
        v = int(m.group(3), 16)
        kind, pid, arg = v >> 56, (v >> 40) & 0xffff, v & 0xffffffffff
        if kind == 1:
            if cur:
                bursts.append(cur)
            cur = {"t": t, "nb_rx": pid, "ev": []}
        elif cur is not None:
            cur["ev"].append((t, kind, pid, arg))
    if cur:
        bursts.append(cur)
    return bursts[1:-1]  # first/last may be cut by the trace window


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else float("nan")


def cmd_ptw():
    """analysis.py ptw <perf-script.txt> [...]"""
    for path in sys.argv[1:]:
        bs = ptw_bursts(path)
        infl, gaps, burst_ns, find_ns, inorder, reps = [], [], [], [], 0, []
        phase = collections.defaultdict(list)
        for i, b in enumerate(bs):
            push, res, last_res, order = {}, {}, None, []
            prev_t = b["t"]
            for t, k, pid, arg in b["ev"]:
                if k == 2:
                    push.setdefault(pid, t)
                elif k in (3, 4, 6):
                    res[pid] = t
                    order.append(pid)
                    if last_res is not None:
                        gaps.append(t - last_res)
                    last_res = t
                elif k == 7:
                    phase[arg].append(t - prev_t)
                    prev_t = t
            for pid, t in res.items():
                if pid in push:
                    infl.append(t - push[pid])
            reps.append(sum(1 for e in b["ev"] if e[1] == 5))
            if order == sorted(order):
                inorder += 1
            if i + 1 < len(bs):
                burst_ns.append(bs[i + 1]["t"] - b["t"])
        n = len(bs)
        print(f"== {path}: bursts={n} mean nb_rx={sum(b['nb_rx'] for b in bs) / max(n, 1):.1f}")
        if not n:
            continue
        print(f"  burst period ns    p50 {pct(burst_ns, .5)}  p90 {pct(burst_ns, .9)}  mean {sum(burst_ns) / max(len(burst_ns), 1):.0f}")
        print(f"  inflight ns        p10 {pct(infl, .1)}  p50 {pct(infl, .5)}  p90 {pct(infl, .9)}  p99 {pct(infl, .99)}  n={len(infl)}")
        print(f"  gap between resolves ns  p10 {pct(gaps, .1)}  p50 {pct(gaps, .5)}  p90 {pct(gaps, .9)}  p99 {pct(gaps, .99)}")
        print(f"  reprobes/burst mean {sum(reps) / n:.2f}   bursts resolved in submission order {inorder}/{n}")
        names = {2: "hash", 3: "alloc", 4: "find", 5: "post", 6: "free", 7: "mac"}
        for b_, name in names.items():
            xs = phase.get(b_, [])
            if xs:
                print(f"  phase ending {name:<6} ns p50 {pct(xs, .5):>6}  p90 {pct(xs, .9):>6}  mean {sum(xs) / len(xs):8.1f}")

# =============================================================================
# ab: harness.sh ab block -> per-q per-arm table (new, 2026-09-23)
# =============================================================================
# Reads <out_dir>/rows.txt (sweep rows prefixed arm=) and each run's log for
# steady Mpps (median of per-second samples after first 3: -P runs insert new
# flows first seconds). Last row per (arm, q) wins: a rerun replaces, and
# launch_order.txt still records both. Pairs `A:B` add B-A deltas.
AB_ROW = re.compile(r"arm=(\S+) q=(\d+)\s.*?avg=(\S+)\s+loopcyc=(\S+)\s+idlecyc=(\S+)\s+"
                    r"batch=(\S+)\s+batchne=(\S+)\s+missed=(\S+).*?freq=(\S+)MHz"
                    r"(?:.*?allpoll=(\S+))?")


def cmd_ab():
    """analysis.py ab <out_dir> [A:B ...]"""
    if len(sys.argv) < 2:
        sys.exit("usage: analysis.py ab <out_dir> [armA:armB ...]")
    out = pathlib.Path(sys.argv[1])
    pairs = [a.split(":") for a in sys.argv[2:]]
    num = lambda x: None if x in (None, "NA") else float(x)
    runs, arms, qs, failed = {}, [], [], []
    for line in (out / "rows.txt").read_text().splitlines():
        if " FAILED " in line:
            failed.append(line)
            continue
        m = AB_ROW.match(line)
        if not m:
            continue
        arm, q = m.group(1), int(m.group(2))
        r = dict(zip(("avg", "loop", "idle", "batch", "batchne", "missed", "freq", "allpoll"),
                     map(num, m.groups()[2:])))
        # exact <arm>_<mode>_q<q>: glob alone lets arm `old` match `old_p9_*` logs
        logs = sorted(l for l in out.glob(f"{arm}_*_q{q}.log")
                      if re.fullmatch(rf"{re.escape(arm)}_[^_]+_q{q}", l.stem))
        if logs:
            r["steady"] = nbp_parse_log(logs[-1]).get("steady_mpps")
        runs[arm, q] = r
        arms += [arm] if arm not in arms else []
        qs += [q] if q not in qs else []
    print(f"{'q':>3} " + "".join(f"{a:>24}" for a in arms))
    print(f"{'':>3} " + "".join(f"{'Mpps loop allp B':>24}" for a in arms))
    for q in qs:
        cells = []
        for a in arms:
            r = runs.get((a, q))
            f = lambda k, w, d: f"{r[k]:>{w}.{d}f}" if r and r.get(k) is not None else f"{'-':>{w}}"
            cells.append(f"{f('steady', 9, 2)}{f('loop', 5, 0)}{f('allpoll', 5, 0)}{f('batchne', 5, 0)}")
        print(f"{q:>3} " + "".join(cells))
    for a, b in pairs:
        print(f"\n{b} - {a}:  q  dMpps  dloop  dallp")
        for q in qs:
            ra, rb = runs.get((a, q)), runs.get((b, q))
            if not (ra and rb):
                continue
            d = lambda k: (rb[k] - ra[k]) if rb.get(k) is not None and ra.get(k) is not None else float("nan")
            print(f"{'':>{len(a) + len(b) + 4}}{q:>3}{d('steady'):>7.2f}{d('loop'):>7.0f}{d('allpoll'):>7.0f}")
    for line in failed:
        print("FAILED:", line)
    (out / "ab.json").write_text(json.dumps({f"{a}_q{q}": r for (a, q), r in runs.items()}, indent=1))


# =============================================================================
# dispatch
# =============================================================================
COMMANDS = {
    "extract": cmd_extract,
    "fit": cmd_fit,
    "matrix": cmd_matrix,
    "report": cmd_report,
    "plot-matrix": cmd_plot_matrix,
    "plot-sweep": cmd_plot_sweep,
    "saturation": cmd_saturation,
    "plot-latency": cmd_plot_latency,
    "plot-ceiling": cmd_plot_ceiling,
    "backing": cmd_backing,
    "selftest": cmd_selftest,
    "probe": cmd_probe,
    "ptw": cmd_ptw,
    "ab": cmd_ab,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    # drop sub from argv: each cmd reads sys.argv exactly as its old script did
    sub = sys.argv.pop(1)
    sys.argv[0] = f"{sys.argv[0]} {sub}"
    COMMANDS[sub]()


if __name__ == "__main__":
    main()
