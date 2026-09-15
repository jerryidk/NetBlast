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
    every raw spec prevents it -- which sweep.sh does, and which is why the
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
        if len(f) > PERF_ENABLED:
            raw = f[PERF_ENABLED].strip()
            try:
                pct = float(raw)
            except ValueError:
                pct = None
            if pct is not None:
                # An enabled percentage outside 0-100 is not a percentage, so
                # this is a shifted column that the check above did not catch.
                if not 0.0 <= pct <= 100.0:
                    malformed.append((line, f"enabled reads {raw}, not a percentage"))
                    continue
                if pct < PERF_MIN_ENABLED:
                    dropped.append((event, pct))
                    continue
        got[event] = int(f[PERF_VALUE])
    return got, dropped, malformed
