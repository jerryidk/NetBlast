"""Parsing for `perf stat -x,` output, split out so it can be tested.

The guard lives here rather than inline in extract_results.py because
extract_results.py does its work at import time and therefore cannot be
imported by test_perf_guard.py, which is what fires the guard for the
reason it was written. See docs/INVESTIGATION.md 5.24 and 5.25.
"""

# `perf stat -x,` field layout, confirmed against the sidecars this parses:
#   0 value  1 unit  2 event  3 run_time_ns  4 enabled_pct  5 metric  6 metric_unit
#
# Field 5 is the DERIVED METRIC, not the enabled percentage: on the
# `instructions` row it holds the IPC, so a guard reading f[5] reads a healthy
# 1.47 IPC as "1.47% enabled" and throws away a good run. A peer session hit
# exactly that and passed it on; test_perf_guard.py pins it as a named case.
PERF_VALUE, PERF_EVENT, PERF_ENABLED = 0, 2, 4
PERF_MIN_ENABLED = 99.99


def parse_perf(text):
    """({event: count}, [(event, enabled_pct) dropped]) from `perf stat -x,`.

    A multiplexed counter is scaled up to a full-window estimate before perf
    prints it, so it is numerically indistinguishable from a measured one and
    nothing downstream could tell. Such a reading is refused here rather than
    stored. The event set this harness uses sits exactly at the ceiling -- with
    SMT a logical CPU has four general-purpose counters, `cycles` and
    `instructions` take fixed-function ones, and the four raw events fill the
    rest -- so one more raw event would silently turn every PMU number in docs/
    into an extrapolation. See docs/INVESTIGATION.md 5.24.
    """
    got, dropped = {}, []
    for line in text.splitlines():
        f = line.split(",")
        if len(f) <= PERF_EVENT or not f[PERF_VALUE].strip().isdigit():
            continue          # "<not counted>", headers, blank lines
        event = f[PERF_EVENT].strip()
        if not event:
            continue
        if len(f) > PERF_ENABLED:
            try:
                pct = float(f[PERF_ENABLED])
            except ValueError:
                pct = None
            if pct is not None and pct < PERF_MIN_ENABLED:
                dropped.append((event, pct))
                continue
        got[event] = int(f[PERF_VALUE])
    return got, dropped
