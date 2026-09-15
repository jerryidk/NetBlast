#!/usr/bin/env python3
"""Re-derive every number the report states as a literal, and fail on drift.

Most of docs/report.html is generated from the data, so it cannot go stale.
But a dozen measured values are written into the prose as literals -- an IPC
here, a page-walk occupancy there -- because they read better mid-sentence than
a format specifier. Those CAN go stale, silently, and nothing else would notice:
the figure beside them regenerates, the sentence does not, and no layer produces
an error. A peer session hit exactly this shape in a report whose figures are
generated and whose prose is hand-written, and observed that its newest material
was its least guarded -- which was true here too, since section 2 went in on the
same night as these checks.

This is the guard for the literals. Run it after any re-extraction.

    python3 check_report_numbers.py
"""
import json
import pathlib
import re
import sys

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
PW = 8.0
ALL = json.loads((DOCS / "results_reproduced.json").read_text())
# Whitespace-normalised, because the generated HTML wraps prose at ~78
# columns and a literal that lands on a line break would otherwise read as
# "no longer on the page" -- a stale checker reporting stale prose.
HTML = re.sub(r"\s+", " ", (DOCS / "report.html").read_text())
FAILED = []


# EVERY CLAIM NAMES ITS DENOMINATOR. The first version of this file checked
# "27% of every core cycle" against the share of the TIMED REGION's cycles and
# reported 35.7%, i.e. drift. The literal was right and the checker was wrong:
# "every core cycle" includes the RX/TX path outside the rdtsc pair, so the
# denominator is pmu_cycles, not cycles_per_pkt -- and against that the page's
# 27% is 26.9%. A checker that encodes a different claim than the prose does
# will send someone to "fix" a correct number, which is worse than no checker
# at all. So the labels below state the denominator, and the two share-of
# quantities are computed side by side where they are both plausible.
def claim(label, stated, derived, tol, unit=""):
    ok = abs(stated - derived) <= tol
    if not ok:
        FAILED.append(label)
    print(f"  {'ok  ' if ok else 'DRIFT'}  {label:52} page {stated:>8.3g}{unit}"
          f"   data {derived:>8.3g}{unit}   (tol {tol:g})")


def rec(cond, mode, q):
    return ALL[cond][mode][str(q)]


def pkts(r):
    return r["steady_mpps"] * 1e6 * PW


def present(text):
    """The literal must still appear on the page, or the check is vacuous."""
    if text not in HTML:
        FAILED.append(f"text missing: {text!r}")
        print(f"  GONE   the page no longer contains {text!r} -- this check is stale")
        return False
    return True


print("literals in the report prose, re-derived from the data:\n")

# --- section 2, the floor arm (newest material)
n1, d1, m1 = (rec("engine_trio", m, 1) for m in ("none", "dramblast", "maglev"))
if present("95&ndash;97%"):
    claim("lookup share of per-packet cost, dramblast", 95,
          100 * (1 - n1["cycles_per_pkt"] / d1["cycles_per_pkt"]), 0.5, "%")
    claim("lookup share of per-packet cost, maglev", 97,
          100 * (1 - n1["cycles_per_pkt"] / m1["cycles_per_pkt"]), 0.5, "%")

# the hit rate dramblast would need for 0.007 misses/packet to be real
if present("99.3%"):
    ll = d1["pmu_LLC-load-misses"] / pkts(d1)
    ref = m1["pmu_LLC-load-misses"] / pkts(m1)
    claim("implied hit rate if 0.007 were a miss rate", 99.3,
          100 * (1 - ll / ref), 0.3, "%")

# table geometry: 2^29 entries x 16 B, against 16.8M flows
if present("3% occupied") and present("268&nbsp;MB"):
    claim("table occupancy at 16.8M flows", 3.0, 100 * 16.8e6 / 2 ** 29, 0.2, "%")
    claim("live set, MB", 268, 16.8e6 * 16 / 1e6, 2.0, " MB")

# --- section 3, the page-walk numbers
def walk(cond, mode, q):
    r = rec(cond, mode, q)
    return (r["pmu_dtlb_walk_active"] / pkts(r),
            r["pmu_dtlb_walk_completed"] / pkts(r),
            r["cycles_per_pkt"], r)

if present("spends 25.6"):
    occ, _, _, _ = walk("pinned2_asshipped", "maglev", 1)
    claim("maglev 2 MiB walk occupancy per packet", 25.6, occ, 0.3, " cyc")

if present("spends 27%"):
    r = rec("xover_dram_thp2m", "dramblast", 1)
    # "of every core cycle" -> ALL core cycles, including the RX/TX path
    # outside the timed region. The timed-region share is 35.7% and is NOT
    # what this sentence says; both are printed so the distinction stays
    # visible to whoever reads this next.
    occ = r["pmu_dtlb_walk_active"] / pkts(r)
    print(f"         (timed-region share, NOT what the page claims: "
          f"{100 * occ / r['cycles_per_pkt']:.1f}%)")
    claim("dramblast 2 MiB walk occupancy / ALL core cycles", 27,
          100 * r["pmu_dtlb_walk_active"] / r["pmu_cycles"], 1.0, "%")

if present("flat at 0.99"):
    ws = [walk(c, m, q)[1]
          for c, m in (("xover_dram_4k", "dramblast"), ("xover_mag_4k", "maglev"))
          for q in range(1, 6) if str(q) in ALL[c][m]]
    claim("page walks per packet on 4 KiB, mean", 0.99,
          sum(ws) / len(ws), 0.02, "/pkt")

# --- section 5, the ramp
if present("worth about 165 cycles"):
    at64 = {}
    for Q, c in ((8, "depth_8"), (16, "depth_16"), (64, "pinned2_asshipped")):
        runs = [r for r in ALL[c]["dramblast"].values() if r.get("rx_batch") == 64]
        at64[Q] = sum(r["cycles_per_pkt"] for r in runs) / len(runs)
    xs = [(1.0 / q - 1.0 / 64.0) for q in (8, 16)]
    ys = [at64[q] - at64[64] for q in (8, 16)]
    claim("pipeline ramp, cycles per fill", 165,
          sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs), 10, " cyc")

# --- section 5, the IPC quoted mid-sentence
if present("an IPC of 3.03"):
    r = rec("pinned2_asshipped", "dramblast", 1)
    claim("dramblast IPC at q=1", 3.03,
          r["insns"] / (r["freq_mhz"] * 1e6 * PW), 0.02)

print()
if FAILED:
    print(f"{len(FAILED)} DRIFTED: " + "; ".join(FAILED))
    print("A literal in the prose no longer matches the data it describes.")
    sys.exit(1)
print("every literal in the report still matches the data")
