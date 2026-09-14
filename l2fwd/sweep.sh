#!/bin/bash
#
# Queue-pair sweep driver.
#
# This is run.sh generalised over queue count, with ONE correction:
#
#   run.sh:   MAX_CORE=$(( (NUM_CORES - 1) * 2 ))   ->  N lcores for N queues
#   here:     MAX_CORE=$(( NUM_CORES * 2 ))         ->  N+1 lcores for N queues
#
# Why: main.c:513-538 assigns one lcore per queue but skips the main lcore
# (main.c:523), and main.c:668 launches workers with SKIP_MAIN. So serving N
# queues needs N worker lcores *plus* the main lcore. Supplying only N makes the
# assignment loop run off the end of the enabled set and call
# rte_exit("Not enough cores") (main.c:528) -- which run.sh does at every queue
# count. See docs/INVESTIGATION.md section 2.
#
# Usage: ./sweep.sh <out_dir> <tag> [mode ...]
#   e.g. ./sweep.sh /tmp/sweep linerate dramblast maglev
#
# Generator (node1), matching docs/data.txt's 8-core / 16M-flow configuration:
#   sudo ./my_pktgen -l 0-16 -a 17:00.0 -- -r 2097152 -p 68
# -r is per TX core (pktgen.c:419), so 8 TX cores x 2^21 = (1<<20)*16 flows.

set -u

OUT_DIR="${1:?usage: $0 <out_dir> <tag> [mode ...]}"
TAG="${2:?usage: $0 <out_dir> <tag> [mode ...]}"
shift 2
MODES=("${@:-dramblast maglev}")
[ "$#" -eq 0 ] && MODES=(dramblast maglev)

# CPUs the benchmark is allowed to use. system.slice / user.slice / init.scope
# are confined to a disjoint housekeeping set (docs/INVESTIGATION.md 3.4c), so
# l2fwd must be launched into its own scope to reach these at all -- a plain
# `sudo ./build/l2fwd` inherits the shell's restricted cpuset and would be
# silently confined to the housekeeping cores.
#
# bench.slice is TOP-LEVEL, not under system.slice, deliberately: cgroup v2
# cpusets are hierarchical, so a scope under a restricted system.slice could
# never exceed system.slice's cpuset however AllowedCPUs was set on it.
BENCH_CPUS="${BENCH_CPUS:-0-23}"

MAX_QUEUES="${MAX_QUEUES:-10}"
# Explicit queue-count list. Defaults to 1..MAX_QUEUES; set it to sweep a subset
# when only the spread of RX burst sizes matters (fitting the per-burst model
# needs a range of bursts, not every queue count), e.g. QUEUES="1 4 7 8 9 10".
QUEUES="${QUEUES:-$(seq 1 "$MAX_QUEUES")}"

# Extra arguments appended to l2fwd's own (post `--`) argument list, e.g.
# "-B 4k" or "-A 8". They go in argv rather than the environment on purpose:
# these runs are launched through `sudo systemd-run`, which strips the
# environment, so an env-var knob would silently fall back to its default and
# report a plausible wrong number. A peer session lost a whole dataset to
# exactly that. Anything in argv also lands in the run's log, so each log says
# what produced it.
L2FWD_EXTRA="${L2FWD_EXTRA:-}"

# Counters sampled per run. cycles gives the delivered clock, instructions the
# equal-work control. The extras are what decide where the per-burst cost goes:
# dTLB-load-misses separates address translation from data access (the two modes
# ship on different page sizes), and stalls_l3_miss is the cycles actually spent
# waiting on memory rather than inferred from a clock ratio. Verified to fit the
# PMU without multiplexing on this part: all six report 100.00% enabled.
PERF_EVENTS="${PERF_EVENTS:-cycles,instructions,dTLB-load-misses,LLC-load-misses,cpu/event=0xa3,umask=0x06,cmask=0x06,name=stalls_l3_miss/}"
CAPACITY="${CAPACITY:-536870912}"   # 2^29 entries x 16 B = 8 GiB table
DPDK_MEM="${DPDK_MEM:-2000}"

mkdir -p "$OUT_DIR"

for MODE in "${MODES[@]}"; do
  echo "=== mode=$MODE  tag=$TAG ==="
  for q in $QUEUES; do
    # N+1 lcores: lcore 0 is the main/stats core, 2..2q are the N queue workers.
    CORE_LIST=$(seq -s, 0 2 $(( q * 2 )))
    LOG="$OUT_DIR/${TAG}_${MODE}_q${q}.log"

    # Fail loudly rather than silently reporting a stale or missing result:
    # delete the log first so a crashed run cannot leave the previous run's
    # numbers in place, and refuse to proceed without the binary.
    [ -x ./build/l2fwd ] || { echo "FATAL: ./build/l2fwd missing or not executable" >&2; exit 1; }
    rm -f "$LOG"

    HP1G=/sys/kernel/mm/hugepages/hugepages-1048576kB/free_hugepages
    HP_BEFORE=$(cat $HP1G)

    sudo systemd-run --scope --quiet --collect \
        --slice=bench.slice -p AllowedCPUs="$BENCH_CPUS" \
        ./build/l2fwd \
        --in-memory \
        -l "$CORE_LIST" \
        -m "$DPDK_MEM" \
        -b 0000:00:05.0 \
        -- \
        -p 1 \
        -q "$q" \
        --no-mac-updating \
        -m "$MODE" \
        -c "$CAPACITY" $L2FWD_EXTRA > "$LOG" 2>&1 &
    RUN_PID=$!

    # Sample mid-run, past main.c's 5 s settle and the 8 GiB table allocation.
    #
    # HP1G: does dramblast actually get 1 GiB pages? It maps its table with
    # MAP_HUGETLB|MAP_HUGE_1GB (dramblast.c:253); an anonymous MAP_HUGETLB draws
    # from the pool without needing a mount, and there is no 1G hugetlbfs mount
    # on either node, so this is untested either way. 8 GiB must take the free
    # count from 16 to 8. maglev uses plain aligned_alloc (maglev.c:43) and must
    # leave it at 16 -- so the two modes are each other's control in one sweep.
    #
    # FREQ: the TSC here is invariant at 2.1 GHz while cores boost to ~3.7, so
    # "Cycle per fwd packet" is really TSC ticks, i.e. time. As q rises more
    # cores go busy and all-core turbo drops, which inflates ticks/packet
    # independently of any real per-packet work.
    #
    # Measured with perf, NOT sysfs. `scaling_cur_freq` under intel_pstate is
    # useless here: sampled while this sweep was busy-polling CPUs 0-14, an IDLE
    # core read 3.63-3.70 GHz -- indistinguishable from the busy ones -- while
    # perf counted 506k cycles/s on that same core, i.e. halted. It reports the
    # P-state request, not per-core delivery. (cpuinfo_cur_freq does not exist
    # under intel_pstate in active mode, so there is no sysfs fallback.)
    #
    # cycles/wall-time per core IS the delivered frequency here, with no
    # task-clock ratio needed, because DPDK busy-polls: the workers sit at 100%
    # with no populate phase or idle time to contaminate the average.
    # Worker cores only -- lcore 0 runs the stats loop, not forwarding.
    sleep "${SAMPLE_AT:-12}"
    HP_DURING=$(cat $HP1G)

    WORKER_CPUS=$(echo "$CORE_LIST" | cut -d, -f2-)
    NWORKERS=$(echo "$WORKER_CPUS" | tr ',' '\n' | grep -c .)
    PERF_WINDOW=${PERF_WINDOW:-8}
    # INSTRUCTIONS are counted alongside cycles for an equal-work control. The
    # collapse test reads any vertical gap between the two clock arms as the
    # memory-latency fraction, which is only valid if both arms execute the SAME
    # work. A peer session checked that assumption on its own two arms and found
    # a real, reproducible 0.19 instructions/key asymmetry between machine
    # states -- the same order as the effect it was measuring. So it must be
    # measured here rather than assumed. Compare arms only at equal burst size:
    # instructions per packet legitimately depend on burst size.
    PERF=$(sudo perf stat -e "$PERF_EVENTS" -C "$WORKER_CPUS" -x, -- \
                 sleep "$PERF_WINDOW" 2>&1)
    # Keep the raw counter output next to the run's log. The summary line below
    # can only carry a couple of numbers, and which counters matter changes as
    # the investigation moves; a sidecar means a later question can be answered
    # from data already taken rather than by re-running the sweep.
    printf '%s\n' "$PERF" > "${LOG%.log}.perf"
    CYCLES=$(awk -F, '/cycles/{print $1; exit}'       <<<"$PERF")
    INSNS=$(awk  -F, '/instructions/{print $1; exit}' <<<"$PERF")
    if [ -n "${CYCLES:-}" ] && [ "$CYCLES" -gt 0 ] 2>/dev/null; then
        FREQ_MHZ=$(( CYCLES / PERF_WINDOW / NWORKERS / 1000000 ))
    else
        FREQ_MHZ=NA
    fi
    IPS=${INSNS:-NA}

    wait $RUN_PID

    T=$(tr -d '\033' < "$LOG")
    MIN=$(grep -oP 'Minimum: \K[0-9.]+'                <<<"$T" | tail -1)
    MAX=$(grep -oP 'Maximum: \K[0-9.]+'                <<<"$T" | tail -1)
    AVG=$(grep -oP 'Average: \K[0-9.]+'               <<<"$T" | tail -1)
    CYC=$(grep -oP 'Cycle per fwd packet: \K[0-9]+'   <<<"$T" | tail -1)
    BAT=$(grep -oP 'Average rx batch sz: \K[0-9]+'    <<<"$T" | tail -1)
    MIS=$(grep -oP 'RX-Missed \(Dropped\): \K[0-9]+'  <<<"$T" | tail -1)
    ERR=$(grep -oP 'Cause: \K.*'                      <<<"$T" | tail -1)

    printf "q=%-2s lcores=%-24s min=%-7s max=%-7s avg=%-7s cyc=%-6s batch=%-4s missed=%-12s hp1g=%s->%s freq=%sMHz insns=%s extra='%s' %s\n" \
      "$q" "$CORE_LIST" "${MIN:-NA}" "${MAX:-NA}" "${AVG:-NA}" \
      "${CYC:-NA}" "${BAT:-NA}" "${MIS:-NA}" \
      "${HP_BEFORE:-NA}" "${HP_DURING:-NA}" "${FREQ_MHZ:-NA}" "${IPS:-NA}" \
      "$L2FWD_EXTRA" "$ERR"
  done
done

echo "SWEEP COMPLETE"
