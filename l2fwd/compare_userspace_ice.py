#!/usr/bin/env python3
"""Put NetBlast's forward path and the userspace-ice reflectors on one axis.

The two projects both report a "cycles per packet", and the two numbers are not
the same quantity.  NetBlast's is an rdtsc bracket around a sub-region of the
forwarding loop (l2fwd/main.c:309-359), which excludes the PMD entirely.
userspace-ice's is whole-process `cycles:u` divided by packets forwarded
(userspace-ice/analysis/scripts/refpool_symmetric_tables.py:101), which includes
everything the core does.

This script derives NetBlast's *whole-loop* cost from its measured throughput and
pinned clock, so that it can be compared against the userspace-ice figures on the
same footing, and reads the userspace-ice figures straight out of that project's
campaign CSVs rather than restating them.

Usage:
    python3 l2fwd/compare_userspace_ice.py [--ice-root ../userspace-ice]
"""

import argparse
import collections
import csv
import pathlib
import sys

# NetBlast's floor arm, from docs/INVESTIGATION.md 5.20.  `-m none` at q=1: one
# worker, core-limited (65.85 well under the 93.28 Mpps the generator offers), so
# this is a single-core cost and not a generator ceiling.
NETBLAST = {
    "mpps_q1": 65.85,          # INVESTIGATION.md:2308
    "offered_mpps": 93.28,     # INVESTIGATION.md:2307
    "clock_ghz": 2.100,        # l2fwd/set_clock.sh pinned
    "bracketed_cyc_pkt": 5,    # INVESTIGATION.md 5.20 table, and 5.29
    "burst": 64,               # MAX_PKT_BURST, l2fwd/main.c:51
}

CAMPAIGN = "20260813-190253-all11-fp-off-10s-3rep"


def load_points(root: pathlib.Path):
    """Reproduce refpool_symmetric_tables.Point for the fields we need.

    cyc/pkt there is cycles:u over the whole run divided by packets forwarded
    over the whole run -- deliberately not a bracketed region.
    """
    metrics = root / "results" / CAMPAIGN / "metrics"
    if not metrics.is_dir():
        sys.exit(f"no campaign at {metrics}")

    counters = collections.defaultdict(dict)
    for r in csv.DictReader((metrics / "combined_perf.csv").open()):
        counters[r["point_id"]][r["event"]] = (
            float(r["value"]), float(r["counter_runtime"]))

    points = collections.defaultdict(list)
    for r in csv.DictReader((metrics / "combined_runs.csv").open()):
        if r["status"] != "ok" or int(r["queue_count"]) != 1:
            continue
        c = counters.get(r["point_id"])
        if not c or "cycles:u" not in c:
            continue
        wall = float(r["active_seconds"])
        pkts = float(r["sw_rx_mpps"]) * 1e6 * wall
        cycles, runtime_ns = c["cycles:u"]
        if pkts <= 0 or runtime_ns <= 0:
            continue
        label = r["variant"] or f"{r['impl']}-baseline"
        points[(label, int(r["batch_size"]))].append({
            "cyc_pkt": cycles / pkts,
            "mpps": float(r["sw_tx_mpps"]),
            # delivered clock, measured rather than assumed: cycles retired
            # divided by the time the counter was actually scheduled.
            "ghz": cycles / runtime_ns,
        })
    return points


def mean(xs):
    return sum(xs) / len(xs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ice-root", default="../userspace-ice", type=pathlib.Path)
    ap.add_argument("--batches", default="64,256")
    args = ap.parse_args()

    whole_loop = NETBLAST["clock_ghz"] * 1e3 / NETBLAST["mpps_q1"]

    print("NetBlast l2fwd, -m none, q=1")
    print(f"  throughput           {NETBLAST['mpps_q1']:.2f} Mpps "
          f"(core-limited; generator offers {NETBLAST['offered_mpps']:.2f})")
    print(f"  clock                {NETBLAST['clock_ghz']:.3f} GHz pinned")
    print(f"  bracketed cyc/pkt    {NETBLAST['bracketed_cyc_pkt']} "
          f"(main.c:309-359, excludes the PMD)")
    print(f"  whole-loop cyc/pkt   {whole_loop:.1f}  "
          f"= {NETBLAST['clock_ghz'] * 1e3:.0f} / {NETBLAST['mpps_q1']:.2f}")
    print(f"  PMD + poll share     {whole_loop - NETBLAST['bracketed_cyc_pkt']:.1f}"
          f" cyc/pkt "
          f"({100 * (1 - NETBLAST['bracketed_cyc_pkt'] / whole_loop):.0f}%)")
    print()

    points = load_points(args.ice_root)
    if not points:
        sys.exit("no usable userspace-ice points")

    batches = [int(b) for b in args.batches.split(",")]
    print(f"userspace-ice, {CAMPAIGN}, q=1 "
          f"(whole-process cycles:u / packets)")
    header = f"  {'target':<28}" + "".join(f"{f'b={b}':>10}" for b in batches)
    print(header + f"{'GHz':>8}")
    print("  " + "-" * (len(header) + 6))

    rows = []
    for label in sorted({k[0] for k in points}):
        cells = []
        ghz = []
        for b in batches:
            got = points.get((label, b))
            cells.append(mean([p["cyc_pkt"] for p in got]) if got else None)
            if got:
                ghz += [p["ghz"] for p in got]
        if any(c is not None for c in cells):
            rows.append((cells[0] if cells[0] else 1e9, label, cells, ghz))

    for _, label, cells, ghz in sorted(rows):
        line = f"  {label:<28}"
        for c in cells:
            line += f"{c:>10.1f}" if c is not None else f"{'-':>10}"
        line += f"{mean(ghz):>8.2f}" if ghz else f"{'-':>8}"
        print(line)

    best = min(r[0] for r in rows)
    worst = max(c for _, _, cells, _ in rows for c in cells if c is not None)
    print()
    print(f"  range across all targets and batches: "
          f"{best:.1f} - {worst:.1f} cyc/pkt")
    print(f"  NetBlast whole-loop, same axis:       {whole_loop:.1f} cyc/pkt")


if __name__ == "__main__":
    main()
