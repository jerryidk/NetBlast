#!/usr/bin/env bash
#
# Which resource runs out first?
#
# Every measurement in docs/INVESTIGATION.md so far is a COST -- cycles or ticks
# per packet. A cost says how expensive something is; it does not say whether
# the machine has any headroom left in the resource that cost is paid from, and
# therefore cannot say whether removing work elsewhere would help at all. This
# measures SATURATION: for each resource that could plausibly be the limit, a
# utilisation against a ceiling that is either in the hardware configuration or
# measured by validate_counters.sh.
#
# The axis is core count, because that is the axis on which a shared resource
# gives itself away: a per-core resource (issue slots, fill buffers) holds its
# utilisation as cores are added, while a shared one (DRAM bandwidth, the
# memory controller, the NIC) rises towards its ceiling and then flattens the
# throughput curve.
#
# Resources covered, and the instrument for each:
#
#   offered load        l2fwd's own RX and RX-Missed: if the NIC is not dropping,
#                       the forwarder is keeping up and nothing else is the limit
#   NIC / PCIe          forwarded bytes/s against Gen4 x16 one-way
#   DRAM bandwidth      uncore_imc cas_count_read+write x 64 B against 8-channel
#                       DDR5-4800, and against the measured streaming ceiling
#   miss parallelism    L1D_PEND_MISS.PENDING / PENDING_CYCLES -- how many misses
#                       the prefetch pipeline actually keeps in flight
#   fill buffers        L1D_PEND_MISS.FB_FULL / cycles -- the saturation of the
#                       structure that limits that parallelism
#   address translation dtlb_walk_active / cycles
#   last-level cache    stalls_l3_miss / cycles, LLC-load-misses (with the
#                       caveat from section 5.22 that on a software-prefetched
#                       path these measure exposure, not traffic)
#   core issue width    top-down level 1 and 2: where the 6 slots/cycle go
#
# Each event group gets its OWN l2fwd run rather than sharing a window, because
# a group large enough to hold all of them would multiplex and silently scale
# every value (section 5.24). That makes this expensive -- one run per group per
# queue count -- and it is the reason the queue list is short by default.
#
# Usage: ./saturation.sh <outdir> <tag> [mode]
#   QUEUES="1 4 8 16 23"  EVENT_GROUPS="topdown1 topdown2 mlp fb tlbmem bw"
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/counter_groups.sh"

OUT_DIR="${1:?usage: $0 <outdir> <tag> [mode]}"
TAG="${2:?usage: $0 <outdir> <tag> [mode]}"
MODE="${3:-dramblast}"

BENCH_CPUS="${BENCH_CPUS:-0-23}"
QUEUES="${QUEUES:-1 4 8 16 23}"
# NOT `GROUPS`: that is a special bash array holding the caller's group IDs.
# Assigning to it from the environment is silently ignored and
# `${GROUPS:-default}` expands to ${GROUPS[0]}, the primary GID -- which on this
# host is 4941, so every run died with "unknown group 4941" and no indication
# that the variable had been overridden by the shell rather than by the caller.
EVENT_GROUPS="${EVENT_GROUPS:-topdown1 topdown2 mlp fb tlbmem bw}"
CAPACITY="${CAPACITY:-536870912}"
DPDK_MEM="${DPDK_MEM:-2000}"
SAMPLE_AT="${SAMPLE_AT:-12}"
PERF_WINDOW="${PERF_WINDOW:-8}"
L2FWD_EXTRA="${L2FWD_EXTRA:-}"

# Which binary. Defaults to the built tree, but the find-mask comparison needs
# the bandwidth group taken on BOTH arms -- section 5.26 predicts that the
# shipped build, which inserts on every packet, carries a write component the
# fixed build does not, and that is a claim about DRAM traffic rather than about
# cycles. The arm is named in every summary line so a log cannot be read as the
# wrong build.
L2FWD_BIN="${L2FWD_BIN:-$HERE/build/l2fwd}"
ARM="${ARM:-$(basename "$L2FWD_BIN")}"

mkdir -p "$OUT_DIR"
[ -x "$L2FWD_BIN" ] || { echo "FATAL: $L2FWD_BIN missing or not executable" >&2; exit 1; }

events_for() {
  case "$1" in
    topdown1) echo "$G_TOPDOWN1" ;;
    topdown2) echo "$G_TOPDOWN2" ;;
    mlp)      echo "$G_MLP" ;;
    fb)       echo "$G_FB" ;;
    tlbmem)   echo "$G_TLBMEM" ;;
    bw)       echo "$G_BW" ;;
    *) echo "FATAL: unknown group $1" >&2; exit 1 ;;
  esac
}

for q in $QUEUES; do
  CORE_LIST=$(seq -s, 0 1 "$q")     # lcore 0 is stats; 1..q are the workers
  WORKER_CPUS=$(echo "$CORE_LIST" | cut -d, -f2-)
  for grp in $EVENT_GROUPS; do
    LOG="$OUT_DIR/${TAG}_${ARM}_${MODE}_q${q}_${grp}.log"
    PERFOUT="${LOG%.log}.perf"
    rm -f "$LOG" "$PERFOUT"

    sudo systemd-run --scope --quiet --collect \
        --slice=bench.slice -p AllowedCPUs="$BENCH_CPUS" \
        "$L2FWD_BIN" \
        --in-memory -l "$CORE_LIST" -m "$DPDK_MEM" -b 0000:00:05.0 \
        -- -p 1 -q "$q" --no-mac-updating -m "$MODE" \
        -c "$CAPACITY" $L2FWD_EXTRA > "$LOG" 2>&1 &
    RUN_PID=$!

    sleep "$SAMPLE_AT"

    # The uncore group counts the whole socket and must be collected -a; the
    # core groups are restricted to the worker CPUs so that lcore 0's stats
    # loop does not dilute them.
    if [ "$grp" = bw ]; then
      sudo perf stat -a -e "$(events_for "$grp")" -x, -- sleep "$PERF_WINDOW" \
        > "$PERFOUT" 2>&1
    else
      sudo perf stat -e "$(events_for "$grp")" -C "$WORKER_CPUS" -x, -- \
        sleep "$PERF_WINDOW" > "$PERFOUT" 2>&1
    fi

    wait $RUN_PID

    T=$(tr -d '\033' < "$LOG")
    AVG=$(grep -oP 'Average: \K[0-9.]+'              <<<"$T" | tail -1)
    CYC=$(grep -oP 'Cycle per fwd packet: \K[0-9]+'  <<<"$T" | tail -1)
    BAT=$(grep -oP 'Average rx batch sz: \K[0-9]+'   <<<"$T" | tail -1)
    MIS=$(grep -oP 'RX-Missed \(Dropped\): \K[0-9]+' <<<"$T" | tail -1)
    ERR=$(grep -oP 'Cause: \K.*'                     <<<"$T" | tail -1)

    # Flag multiplexing here rather than leaving it to the analysis: a scaled
    # value that reaches a plot is a wrong number that looks like a right one.
    # Field 5 is the enabled percentage. Field 6 is perf's derived METRIC --
    # the top-down fractions, insn-per-cycle -- so checking field 6 labels every
    # metric-bearing group MULTIPLEXED and, worse, passes a truly multiplexed
    # event whose metric column is empty. Also catch events the PMU refused
    # outright: those report 100.00 enabled and a value of `<not supported>`,
    # which no percentage check can see.
    # ADVISORY ONLY. perf_csv.parse_perf is the authoritative guard and runs at
    # analysis time; this marker exists so a bad run is visible in the console
    # as it happens rather than half an hour later.
    #
    # THE STRING COMPARISON IS DELIBERATE -- DO NOT "TIDY" IT INTO
    # `$5 + 0 < 99.99`. An unnamed raw event spec contains commas, so under
    # `-x,` it splits across fields and shifts run_time_ns into the enabled
    # column. A run time in nanoseconds is a huge number, so a lower-bound-only
    # test PASSES a shifted row silently; `!= "100.00"` rejects it, because a
    # nanosecond count is not that literal string. If this is ever made
    # numeric, the range check `0 <= x <= 100` must go in first, in the same
    # change, because only the upper bound catches a shift.
    #
    # An EMPTY enabled column is now a failure too. It previously read as
    # healthy, which is the one shape a threshold can never see: not a low
    # percentage, but no percentage at all.
    MUX=$(awk -F, '
        $1 ~ /not supported|not counted/ { print $3"=uncounted"; next }
        $1 ~ /^[0-9]+$/ && $5 == ""       { print $3"=NO-ENABLED-COLUMN"; next }
        $1 ~ /^[0-9]+$/ && $5 != "100.00" { print $3"@"$5"%" }' \
              "$PERFOUT" | paste -sd' ')

    printf "arm=%-16s q=%-3s grp=%-9s workers=%-3s avg=%-7s cyc=%-5s batch=%-4s missed=%-14s %s%s\n" \
      "$ARM" "$q" "$grp" "$(echo "$WORKER_CPUS" | tr ',' '\n' | grep -c .)" \
      "${AVG:-NA}" "${CYC:-NA}" "${BAT:-NA}" "${MIS:-NA}" \
      "${MUX:+MULTIPLEXED[$MUX] }" "$ERR" \
      | tee -a "$OUT_DIR/summary.txt"
  done
done
echo "SATURATION SWEEP COMPLETE"
