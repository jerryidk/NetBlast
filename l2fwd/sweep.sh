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

MAX_QUEUES="${MAX_QUEUES:-10}"
CAPACITY="${CAPACITY:-536870912}"   # 2^29 entries x 16 B = 8 GiB table
DPDK_MEM="${DPDK_MEM:-2000}"

mkdir -p "$OUT_DIR"

for MODE in "${MODES[@]}"; do
  echo "=== mode=$MODE  tag=$TAG ==="
  for q in $(seq 1 "$MAX_QUEUES"); do
    # N+1 lcores: lcore 0 is the main/stats core, 2..2q are the N queue workers.
    CORE_LIST=$(seq -s, 0 2 $(( q * 2 )))
    LOG="$OUT_DIR/${TAG}_${MODE}_q${q}.log"

    HP1G=/sys/kernel/mm/hugepages/hugepages-1048576kB/free_hugepages
    HP_BEFORE=$(cat $HP1G)

    sudo ./build/l2fwd \
        --in-memory \
        -l "$CORE_LIST" \
        -m "$DPDK_MEM" \
        -b 0000:00:05.0 \
        -- \
        -p 1 \
        -q "$q" \
        --no-mac-updating \
        -m "$MODE" \
        -c "$CAPACITY" > "$LOG" 2>&1 &
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
    # FREQ: the TSC here is invariant at 2.1 GHz while cores boost to 3.7, so
    # "Cycle per fwd packet" is really TSC ticks, i.e. time. As q rises more
    # cores go busy and all-core turbo drops, which inflates ticks/packet
    # independently of any real per-packet work. Median over the ACTIVE cores,
    # not one core: turbo bins can differ per core under all-core load.
    sleep "${SAMPLE_AT:-15}"
    HP_DURING=$(cat $HP1G)
    FREQ_KHZ=$(for c in $(echo "$CORE_LIST" | tr ',' ' '); do
                   cat /sys/devices/system/cpu/cpu$c/cpufreq/scaling_cur_freq 2>/dev/null
               done | sort -n | awk '{a[NR]=$1} END{if(NR)print a[int((NR+1)/2)]}')

    wait $RUN_PID

    T=$(tr -d '\033' < "$LOG")
    MIN=$(grep -oP 'Minimum: \K[0-9.]+'                <<<"$T" | tail -1)
    MAX=$(grep -oP 'Maximum: \K[0-9.]+'                <<<"$T" | tail -1)
    AVG=$(grep -oP 'Average: \K[0-9.]+'               <<<"$T" | tail -1)
    CYC=$(grep -oP 'Cycle per fwd packet: \K[0-9]+'   <<<"$T" | tail -1)
    BAT=$(grep -oP 'Average rx batch sz: \K[0-9]+'    <<<"$T" | tail -1)
    MIS=$(grep -oP 'RX-Missed \(Dropped\): \K[0-9]+'  <<<"$T" | tail -1)
    ERR=$(grep -oP 'Cause: \K.*'                      <<<"$T" | tail -1)

    printf "q=%-2s lcores=%-24s min=%-7s max=%-7s avg=%-7s cyc=%-6s batch=%-4s missed=%-12s hp1g=%s->%s freq=%sMHz %s\n" \
      "$q" "$CORE_LIST" "${MIN:-NA}" "${MAX:-NA}" "${AVG:-NA}" \
      "${CYC:-NA}" "${BAT:-NA}" "${MIS:-NA}" \
      "${HP_BEFORE:-NA}" "${HP_DURING:-NA}" "$(( ${FREQ_KHZ:-0} / 1000 ))" "$ERR"
  done
done

echo "SWEEP COMPLETE"
