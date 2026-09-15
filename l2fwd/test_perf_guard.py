#!/usr/bin/env python3
"""Check that the PMU multiplexing guard in extract_results.py actually bites.

Why this file exists
--------------------
docs/INVESTIGATION.md 5.24 records that `sweep.sh`'s six-event set sits exactly
at this part's counter ceiling, and that a multiplexed reading is scaled to a
full-window estimate before perf prints it -- so it is numerically
indistinguishable from a measured one, and nothing downstream can tell. The
guard refuses such readings. A guard that has never been shown to fire for the
reason it was written is not evidence of anything, so this fires it.

Two halves, and the second is the one that matters.

  SYNTHETIC   feed the parser hand-built perf output. Cheap, runs anywhere,
              and pins the field-index regression: an `instructions` row
              carrying an IPC in field 5 must not be read as an enabled
              percentage.

  REAL        (--real) oversubscribe the PMU on purpose -- ask for more raw
              events than this part can count at once -- and assert the guard
              rejects what comes back. The multiplexing is produced by the
              hardware, so this exercises the actual failure mode rather than a
              reconstruction of it, and it cannot rot: if a future part changes
              its counter budget this test changes its answer, where a
              hand-edited percentage would keep passing forever.

The real half is a peer session's idea, arrived at while closing the same gap
on its own harness; credited in docs/INVESTIGATION.md 5.25.

    python3 test_perf_guard.py            # synthetic only, no PMU needed
    python3 test_perf_guard.py --real     # adds the hardware half (needs perf)
"""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from perf_csv import parse_perf, PERF_MIN_ENABLED          # noqa: E402

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
    got, dropped = parse_perf(clean)
    check("a clean sidecar is accepted whole", len(got) == 3 and not dropped,
          f"{len(got)} readings, {len(dropped)} dropped")

    # THE REGRESSION THAT BROKE A PEER'S GUARD. Field 5 on the `instructions`
    # row is the IPC. A guard reading it as the enabled percentage sees 1.47
    # and throws away a perfectly healthy run.
    got, dropped = parse_perf(
        "24627492628,,instructions,8001019154,100.00,1.47,insn per cycle\n")
    check("an IPC in field 5 is not read as an enabled percentage",
          got.get("instructions") == 24627492628 and not dropped,
          "field 4 is enabled, field 5 is the derived metric")

    got, dropped = parse_perf(
        "16764707551,,cycles,8001021078,41.63,,\n")
    check("a 41.63% reading is refused", not got and dropped == [("cycles", 41.63)],
          "and the rejection names the event and the percentage")

    # Both sides of the threshold, so the boundary is a decision and not a
    # coincidence of the numbers that happen to be in the data.
    hi, _ = parse_perf("1,,e,1,100.00,,\n")
    on, _ = parse_perf(f"1,,e,1,{PERF_MIN_ENABLED:.2f},,\n")
    _, lo = parse_perf("1,,e,1,99.98,,\n")
    check("the 99.99% boundary holds on both sides",
          hi and on and lo, f"100.00 and {PERF_MIN_ENABLED} accepted, 99.98 refused")

    got, dropped = parse_perf("<not counted>,,dtlb_walk_active,8001017468,0.00,,\n")
    check("'<not counted>' is neither stored nor mistaken for zero",
          not got and not dropped)

    # A reading with no enabled column at all must still parse: the historical
    # sidecars predate nothing here, but a perf version that omits it would
    # otherwise silently drop every counter.
    got, dropped = parse_perf("123,,cycles\n")
    check("a row with no enabled column still parses", got == {"cycles": 123})

    got, dropped = parse_perf("500,,a,1,100.00,,\n600,,b,1,60.00,,\n700,,c,1,100.00,,\n")
    check("one bad reading does not take the good ones with it",
          got == {"a": 500, "c": 700} and dropped == [("b", 60.0)])


# -------------------------------------------------------------------- real
# sweep.sh's own set: two fixed-function events plus four raw ones.
SHIPPED = ("cycles,instructions,"
           "cpu/event=0x12,umask=0x0e,name=dtlb_walk_completed/,"
           "cpu/event=0x12,umask=0x10,name=dtlb_walk_active/,"
           "cpu/event=0xa3,umask=0x06,cmask=0x06,name=stalls_l3_miss/,"
           "LLC-load-misses")

# Enough extra raw events to go over the edge. How many that takes is NOT a
# fixed number -- it depends on the event set, because individual events carry
# counter restrictions -- so it was measured here rather than reasoned about:
# with this set, five raw events report 100.00% and six multiplex. See
# docs/INVESTIGATION.md 5.24, which previously said four and was wrong.
#
# The names are x5/x6 and not r5/r6: perf reads a bare `rNNN` as its raw-event
# syntax, so `name=r5` is a parser error rather than a name. That cost a
# debugging round here and the test failed loudly rather than passing, which is
# the behaviour wanted.
OVER = (SHIPPED
        + ",cpu/event=0xd1,umask=0x01,name=x5/"
        + ",cpu/event=0xd1,umask=0x02,name=x6/")


# Counted per-CPU, the way sweep.sh does it, and NOT per-task. A per-task count
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
    got, dropped = parse_perf(at_limit)
    pcts = [float(l.split(",")[4]) for l in at_limit.splitlines()
            if len(l.split(",")) > 4 and l.split(",")[0].strip().isdigit()]
    check("the shipped event set is counted and accepted",
          len(got) == 6 and not dropped,
          f"{len(got)} readings, enabled {min(pcts):.2f}%-{max(pcts):.2f}%"
          if pcts else "no readings")

    over = run_perf(OVER)
    got, dropped = parse_perf(over)
    pcts = [float(l.split(",")[4]) for l in over.splitlines()
            if len(l.split(",")) > 4 and l.split(",")[0].strip().isdigit()]
    check("an oversubscribed PMU multiplexes, and every reading is refused",
          len(dropped) >= 6 and not got,
          f"{len(dropped)} dropped, enabled {min(pcts):.2f}%-{max(pcts):.2f}%"
          if pcts else "no readings")


if __name__ == "__main__":
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
