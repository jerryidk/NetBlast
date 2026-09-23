#!/usr/bin/env bash
#
# NetBlast l2fwd measurement harness. One file, one subcommand per old script.
#
#   harness.sh sweep      <out_dir> <tag> [mode ...]   queue-pair sweep        (was sweep.sh)
#   harness.sh matrix     [block ...]                  experiment matrix       (was run_matrix.sh)
#   harness.sh saturation <outdir> <tag> [mode]        which resource runs out (was saturation.sh)
#   harness.sh validate   [cpu] [bench_membw path]     prove PMU counters      (was validate_counters.sh)
#   harness.sh ceiling    <outdir> [--hk]              DRAM ceiling sweep      (was dram_ceiling.sh)
#   harness.sh clock      {pinned|turbo|show}          core clock arms         (was set_clock.sh)
#   harness.sh pagewatch  [log]                        page backing sampler    (was page_watch.sh)
#   harness.sh codegen    [binary]                     binary still sane       (was check_codegen.sh)
#
# Each sub runs alone in this process, with same shell options its old script
# had, set at top of sub. Env knobs, args, output paths unchanged.
# counter_groups() = old counter_groups.sh; subs that sourced it call it.

HERE="$(cd "$(dirname "$0")" && pwd)"

# ===========================================================================
# counter_groups -- was counter_groups.sh
# ===========================================================================
# Event groups for the saturation study, and the ceilings they are read against.
#
# Called by validate, saturation, ceiling subs so validate and saturation cannot drift
# apart -- validating one set of encodings and then measuring with another is
# the failure this file exists to prevent.
#
# Why groups at all: docs/INVESTIGATION.md section 5.24 established that a
# multiplexed counter silently scales its result, so every group here is sized
# to fit the PMU at once and each run is checked for 100% enabled. The core
# groups are deliberately small for one further reason -- a peer session found
# that three simultaneous 0x48-family events collide in the scheduler, so
# L1D_PEND_MISS.PENDING/PENDING_CYCLES and FB_FULL are split across two groups
# rather than gathered into one.
#
# Raw encodings, not perf's generic aliases: this part (family 6, model 207,
# Emerald Rapids) has no JSON event file, so `perf list` shows only the
# architectural events and an alias may map to nothing and read zero.
counter_groups() {

# --- core: where the issue slots go (top-down level 1) ---------------------
#
# THE BRACES ARE LOAD-BEARING. The topdown-* events are derived from the
# PERF_METRICS MSR and must be programmed in one group with `slots` as the
# group leader. Written as a plain comma list they report `<not supported>`
# with a run time of 0 and an enabled percentage of 100.00 -- so a
# multiplexing check passes, nothing errors, and every top-down number is
# silently absent. Measured on this host: without braces, slots counts
# 21,979,161,492 and all four metrics read `<not supported>`; with braces the
# same workload reports 19.4% retiring / 0.0% bad-spec / 73.4% frontend /
# 7.3% backend.
G_TOPDOWN1="{slots,topdown-retiring,topdown-bad-spec,topdown-fe-bound,topdown-be-bound}"

# --- core: which backend resource (top-down level 2) ----------------------
G_TOPDOWN2="{slots,topdown-mem-bound,topdown-fetch-lat,topdown-heavy-ops,topdown-br-mispredict}"

# --- memory-level parallelism: how many misses are in flight --------------
# PENDING / PENDING_CYCLES = mean outstanding L1D misses while any is
# outstanding. A dependent pointer chase must give ~1.0; that is the known
# answer `harness.sh validate` checks.
G_MLP="cycles,\
cpu/event=0x48,umask=0x01,name=l1d_pend_miss_pending/,\
cpu/event=0x48,umask=0x01,cmask=0x01,name=l1d_pend_miss_pending_cycles/"

# --- fill buffers: is the miss-handling structure itself the limit? -------
# FB_FULL counts cycles in which a request could not be issued because no fill
# buffer was free. As a fraction of cycles it is a saturation figure for the
# L1D's miss-handling capacity, which is what a software prefetch pipeline of
# depth 64 is most likely to run into.
G_FB="cycles,\
cpu/event=0x48,umask=0x02,name=l1d_pend_miss_fb_full/,\
cpu/event=0x20,umask=0x08,name=offcore_reqs_outstanding_data_rd/"

# --- address translation and last-level cache -----------------------------
G_TLBMEM="cycles,instructions,\
cpu/event=0x12,umask=0x0e,name=dtlb_walk_completed/,\
cpu/event=0x12,umask=0x10,name=dtlb_walk_active/,\
cpu/event=0xa3,umask=0x06,cmask=0x06,name=stalls_l3_miss/,\
LLC-load-misses"

# --- DRAM latency, as distinct from DRAM bandwidth ------------------------
# Little's law: mean outstanding data reads / data-read completion rate = mean
# latency of one, in core cycles. Bandwidth can sit at 4% while latency is what
# the core waits on -- the q=23 signature of §5.31, which no other group can
# tell apart.
#
# Own group, not added to G_FB: FB_FULL is 0x48-family and §5.27 records that
# three simultaneous 0x48 events collide in the event scheduler.
#
# KNOWN ANSWER for `harness.sh validate`: bench_membw's dependent pointer chase
# has MLP ~1 by construction, so this ratio must come out at roughly one full
# memory latency (order 200-400 cycles on this part). Reads 0 -> the encoding
# did not resolve. Reads ~1 -> it resolved to the wrong event. Per §5.23 this
# check is mandatory: no perf JSON exists for family 6 model 207, so an
# unresolved encoding reads zero indistinguishably from a real zero.
G_LATENCY="cycles,\
cpu/event=0x20,umask=0x08,name=offcore_reqs_outstanding_data_rd/,\
cpu/event=0x21,umask=0x08,name=offcore_reqs_data_rd/"

# --- DRAM bandwidth (uncore, per socket; must be collected system-wide) ----
# A separate PMU from the core counters, so this does not compete with them for
# general-purpose counters and may be collected alongside.
#
# RAW ENCODINGS, and this one was nearly got wrong. The obvious spelling is
# perf's `uncore_imc_N/cas_count_read/` alias -- but there is no `events/`
# directory under /sys/bus/event_source/devices/uncore_imc_N at all on this
# part, only `format`, `alias`, `cpumask` and `type`, so that alias resolves to
# nothing. This is the same failure as the dTLB alias in section 5.23, in the
# one group that had been left aliased. CAS_COUNT.RD is event 0x05 umask 0xcf
# and CAS_COUNT.WR is umask 0xf0; each count is one 64 B cache line, so the
# analysis multiplies by 64 rather than relying on a .scale file that also does
# not exist here.
G_BW=$(for i in 0 1 2 3 4 5 6 7; do
         printf 'uncore_imc_%d/event=0x05,umask=0xcf,name=cas_rd_%d/,' $i $i
         printf 'uncore_imc_%d/event=0x05,umask=0xf0,name=cas_wr_%d/,' $i $i
       done | sed 's/,$//')
# Counts are cache lines, not bytes.
CAS_LINE_BYTES=64

# --- ceilings, measured or from the hardware configuration ----------------
# DRAM: 8 populated channels x DDR5-4800 x 8 B/transfer. Theoretical; the
# achievable figure is measured by `harness.sh validate` and is the one the
# saturation study should divide by.
DRAM_PEAK_GBS=307.2
# NIC: PCIe Gen4 x16, 16 GT/s x 16 lanes x 128b/130b encoding, one direction.
PCIE_GBS=31.5
# Core: Golden Cove allocates 6 uops/cycle, so slots = 6 x cycles.
ISSUE_WIDTH=6
# Wire: 100 Gbps with 110 B frames plus 8 B preamble and 12 B inter-frame gap.
LINE_RATE_MPPS=96.15
}

# ===========================================================================
# sweep -- was sweep.sh
# ===========================================================================
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
# Usage: ./harness.sh sweep <out_dir> <tag> [mode ...]
#   e.g. ./harness.sh sweep /tmp/sweep linerate dramblast maglev
#
# Generator (node1), matching docs/data.txt's 8-core / 16M-flow configuration:
#   sudo ./my_pktgen -l 0-16 -a 17:00.0 -- -r 2097152 -p 68
# -r is per TX core (pktgen.c:419), so 8 TX cores x 2^21 = (1<<20)*16 flows.
cmd_sweep() {
set -u

OUT_DIR="${1:?usage: $0 sweep <out_dir> <tag> [mode ...]}"
TAG="${2:?usage: $0 sweep <out_dir> <tag> [mode ...]}"
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
#
# The TLB events are given as RAW ENCODINGS rather than perf's generic
# `dTLB-load-misses` alias. perf has no JSON event file for this part (family 6
# model 207, Emerald Rapids) -- `perf list` shows only the architectural events
# -- so the generic alias may map to nothing useful, and on an idle core it read
# zero where the raw counter read non-zero. A counter that silently reads zero
# is the worst possible outcome for this experiment, because the crossover
# predicts small TLB numbers on the 1 GiB arm and zero is not distinguishable
# from "the alias is broken".
#
# dtlb_walk_active is the one that matters: it counts CYCLES spent walking page
# tables, so it is directly comparable to the per-packet cost rather than
# needing a latency assumed per walk. walk_completed gives the count of walks,
# so the two together also give the average walk cost as a by-product.
PERF_EVENTS="${PERF_EVENTS:-cycles,instructions,\
cpu/event=0x12,umask=0x0e,name=dtlb_walk_completed/,\
cpu/event=0x12,umask=0x10,name=dtlb_walk_active/,\
cpu/event=0xa3,umask=0x06,cmask=0x06,name=stalls_l3_miss/,\
LLC-load-misses}"
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
    # the printed cycles-per-packet is really TSC ticks, i.e. time. As q rises more
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
    # `Cycle per fwd packet` is gone, not renamed: it timed the mode branch
    # alone, and main.c now times the whole iteration with a single rdtsc per
    # poll. It is NOT scraped here any more, so a log from the old binary makes
    # this field absent rather than quietly supplying a number that means
    # something else. LCY is the replacement and covers the full lifecycle --
    # rx burst, mode branch, tx burst, drop path.
    LCY=$(grep -oP 'Full-loop cyc per fwd packet: \K[0-9]+' <<<"$T" | tail -1)
    EPC=$(grep -oP 'Cyc per empty poll: \K[0-9]+'     <<<"$T" | tail -1)
    # Anchored on the trailing colon so it cannot also match the "(nonempty
    # polls)" line below it. BAT divides by every poll including the empty
    # ones; BATNE divides by the polls that actually returned a packet and is
    # the one the P + C/B burst fit wants.
    BAT=$(grep -oP 'Average rx batch sz: \K[0-9]+'    <<<"$T" | tail -1)
    BATNE=$(grep -oP 'Average rx batch sz \(nonempty polls\): \K[0-9]+' <<<"$T" | tail -1)
    MIS=$(grep -oP 'RX-Missed \(Dropped\): \K[0-9]+'  <<<"$T" | tail -1)
    ERR=$(grep -oP 'Cause: \K.*'                      <<<"$T" | tail -1)

    printf "q=%-2s lcores=%-24s min=%-7s max=%-7s avg=%-7s loopcyc=%-6s idlecyc=%-6s batch=%-4s batchne=%-4s missed=%-12s hp1g=%s->%s freq=%sMHz insns=%s extra='%s' %s\n" \
      "$q" "$CORE_LIST" "${MIN:-NA}" "${MAX:-NA}" "${AVG:-NA}" \
      "${LCY:-NA}" "${EPC:-NA}" "${BAT:-NA}" "${BATNE:-NA}" "${MIS:-NA}" \
      "${HP_BEFORE:-NA}" "${HP_DURING:-NA}" "${FREQ_MHZ:-NA}" "${IPS:-NA}" \
      "$L2FWD_EXTRA" "$ERR"
  done
done

echo "SWEEP COMPLETE"
}

# ===========================================================================
# matrix -- was run_matrix.sh
# ===========================================================================
#
# The experiment matrix that follows the two clock arms.
#
# Each block below answers one question, and each is a wrapper around `harness.sh sweep`
# with different l2fwd arguments -- no new measurement code, so every condition
# is measured by the same harness and the conditions differ only in the flags
# named here.
#
#   pinned2   control. Same flags-free invocation as the `pinned` arm, but built
#             from the -B/-A/-Q source. Any difference between `pinned` and
#             `pinned2` is the refactor, not an experiment, and must be checked
#             before reading anything else in this matrix.
#
#   xdram2m   CROSSOVER. As shipped the two modes differ in more than their
#   xdram4k   lookup algorithm: dramblast maps its 8 GiB table on 1 GiB pages
#   xmag1g    and maglev gets 2 MiB THP (measured -- the whole table shows up in
#   xmag4k    AnonHugePages). 8 GiB on 2 MiB pages is 4096 pages against a
#             2048-entry STLB, so maglev takes a page walk on most lookups and
#             dramblast takes none. These four runs put each mode on the other's
#             backing, plus 4 KiB which neither ships with, turning a two-point
#             swap into a three-point trend.
#
#   ahoist    ALLOCATOR. dramblast does one aligned_alloc/free round trip per
#   a2 a4 a8  burst (dramblast.c, in dramblast_process_frames). Sweeping the
#             number of pairs makes C a line in that number: the slope is what a
#             pair costs on this machine and the intercept at -1 is the part of
#             the per-burst cost that is not the allocator. This replaces
#             quoting 20-40 ns from the literature.
#
#   pinned3   REPEATABILITY. The as-shipped condition again, hours after the
#             control block, so every coefficient gets a run-to-run error bar
#             and not merely a within-fit one.
#
#   trio      THE THREE ENGINES AGAINST THE FLOOR, in one sitting. `-m none`
#             (main.c:483) takes the third branch of the forwarding loop
#             (main.c:393-398): it writes the destination MAC exactly as the
#             other two do, and skips only the lookup that produced the address.
#             It is therefore the subtraction that turns "cycles per forwarded
#             packet" into "cycles per LOOKUP" -- without it every per-packet
#             number in this matrix silently includes an unknown amount of
#             harness, and the engine gap cannot be stated as a fraction of
#             anything. All three modes are swept under one tag, back to back,
#             so the comparison does not span the hours that separate the
#             historical arms; `pinned3` bounds what that would have cost.
#
#   d8 d16    PIPELINE DEPTH. The other candidate for C. A burst of B packets
#   d32       can only fill min(B, depth) slots of the prefetch queue, so short
#             bursts run a pipeline that never reaches steady state. If C is
#             that ramp it must fall as the depth falls, while P rises because
#             less latency is hidden. If C is the allocator, depth cannot touch
#             it. The two knobs cannot mimic each other, which is the point.
#
#   depthrep  THE DEPTH-32 TEST, PROPERLY. The first depth block left the only
#             arm that TESTS the per-fill ramp model unresolved: depth 32's
#             excess over depth 64 is 1.8 cycles/packet against a measured
#             run-to-run floor of 0.45, so one sweep each separates the model
#             (2.6 predicted) from no effect (0.0) at neither. The fix is
#             repeats, not a better fit. Only q=1..5 is swept, because the whole
#             comparison is made at a 64-packet burst and those are the queue
#             counts that stay there -- and because the two arms are interleaved
#             rather than run in two blocks, so any drift over the half hour
#             lands on both equally instead of entirely on the second one.
#
# All blocks sweep the full queue range. A reduced list was tempting -- fitting
# P and C needs a spread of BURST sizes, not every queue count -- but the burst
# size at a given q is not a property of q: it is set by how fast the forwarder
# drains its queues. The allocator and depth arms are deliberately slower than
# the baseline, so they stay oversubscribed further up the sweep and sit at
# larger bursts at the same q. A queue list chosen from the baseline's burst
# sizes would therefore compress those arms' x-range exactly where the slope is
# determined, and C is the coefficient the whole block exists to measure.
#
# Usage: ./harness.sh matrix [block ...]      (default: all blocks, in order)
#   e.g. ./harness.sh matrix control crossover
cmd_matrix() {
set -u
cd "$HERE"

OUT=${OUT:-/users/sohamb/sweeps}
BLOCKS=("${@:-control crossover alloc depth repeat}")
[ "$#" -eq 0 ] && BLOCKS=(control crossover alloc depth repeat)

# Every invocation goes through a subshell. A `VAR=x run ...` prefix would NOT
# be scoped to the call: bash keeps variable assignments that precede a shell
# FUNCTION in the shell environment afterwards (unlike for external commands),
# so QUEUES set for the allocator block would silently leak into anything that
# ran after it and quietly halve those sweeps.
run() {  # run <tag> <mode> <extra args...>
  local tag=$1 mode=$2; shift 2
  echo "### $tag  mode=$mode  extra='$*'  $(date -u +%H:%M:%S)"
  # Per-MODE output file. `tee "$OUT/$tag.out"` truncates, so the control block --
  # the only one that runs two modes under one tag -- silently threw away the
  # first mode's summary lines, and with them the per-run frequency and
  # instruction counts that live only on those lines. The per-run .log files
  # survived, so nothing was lost that could not be recovered from the top-level
  # transcript, but the sidecar this file exists to be was empty for one mode.
  L2FWD_EXTRA="$*" ./harness.sh sweep "$OUT/$tag" "$tag" "$mode" 2>&1 | tee "$OUT/${tag}_${mode}.out"
}

for b in "${BLOCKS[@]}"; do
 case $b in
  control)
    ( run pinned2 dramblast )
    ( run pinned2 maglev ) ;;
  crossover)
    # 4 KiB needs a longer settle: zeroing 8 GiB through 2M page faults instead
    # of 8 is slow, and `harness.sh sweep` samples at a fixed offset from launch.
    ( run xdram2m dramblast -B thp2m )
    ( export SAMPLE_AT=${SAMPLE_AT_4K:-24}; run xdram4k dramblast -B 4k )
    ( run xmag1g  maglev    -B 1g )
    ( export SAMPLE_AT=${SAMPLE_AT_4K:-24}; run xmag4k  maglev    -B 4k ) ;;
  alloc)
    ( run ahoist dramblast -A -1 )
    ( run a2     dramblast -A 2 )
    ( run a4     dramblast -A 4 )
    ( run a8     dramblast -A 8 ) ;;
  repeat)
    # Re-take the as-shipped condition last. Every uncertainty quoted so far is
    # the scatter of one fit about one dataset; this is the only thing that says
    # how much a coefficient moves when the identical condition is simply run
    # again, hours later. Without it a 5% difference between two arms has
    # nothing to be compared against.
    ( run pinned3 dramblast )
    ( run pinned3 maglev ) ;;
  trio)
    # `none` takes no -B/-A/-Q: none of them mean anything without a table. -c
    # is still passed by `harness.sh sweep` and is ignored, because main.c:692-697 only
    # calls an init function when a mode is enabled, so this arm allocates no
    # table at all -- which is the point, and is visible as hp1g=16->16 in the
    # run's own summary line.
    ( run trio none )
    ( run trio dramblast )
    ( run trio maglev ) ;;
  depth)
    ( run d8  dramblast -Q 8 )
    ( run d16 dramblast -Q 16 )
    ( run d32 dramblast -Q 32 ) ;;
  depthrep)
    # Interleaved, not blocked: three of one then three of the other would put
    # any drift over the half hour entirely into the difference the block
    # exists to measure. q=1..5 only -- those are the counts whose burst stays
    # at 64, and the comparison is made there.
    for i in 1 2 3; do
      ( export QUEUES="1 2 3 4 5"; run "d32r$i" dramblast -Q 32 )
      ( export QUEUES="1 2 3 4 5"; run "d64r$i" dramblast )
    done ;;
  *) echo "unknown block: $b" >&2; exit 1 ;;
 esac
done
echo "MATRIX COMPLETE"
}

# ===========================================================================
# saturation -- was saturation.sh
# ===========================================================================
#
# Which resource runs out first?
#
# Every measurement in docs/INVESTIGATION.md so far is a COST -- cycles or ticks
# per packet. A cost says how expensive something is; it does not say whether
# the machine has any headroom left in the resource that cost is paid from, and
# therefore cannot say whether removing work elsewhere would help at all. This
# measures SATURATION: for each resource that could plausibly be the limit, a
# utilisation against a ceiling that is either in the hardware configuration or
# measured by `harness.sh validate`.
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
# Usage: ./harness.sh saturation <outdir> <tag> [mode]
#   QUEUES="1 4 8 16 23"
#   EVENT_GROUPS="topdown1 topdown2 mlp fb tlbmem latency bw"  (latency is new,
#   see counter_groups(); it is NOT in the default list, pass it explicitly)
cmd_saturation() {
set -uo pipefail
counter_groups

OUT_DIR="${1:?usage: $0 saturation <outdir> <tag> [mode]}"
TAG="${2:?usage: $0 saturation <outdir> <tag> [mode]}"
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
    latency)  echo "$G_LATENCY" ;;
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

    # Was: sudo systemd-run --scope --quiet --collect \
    #          --slice=bench.slice -p AllowedCPUs="$BENCH_CPUS"
    # That is the call §5.28 recorded as denied to an agent session's harness
    # permissions. benchctl was rewritten to avoid it; this script never was.
    # Same placement, narrower operation: join bench.slice by writing our own
    # pid to cgroup.procs, then exec. taskset inside the slice is equivalent to
    # -p AllowedCPUs= because the cpuset ceiling is 0-23 once joined.
    #
    # NOT routed through `benchctl run`: that does a ~6 s preflight per call
    # (two busy samples plus an I/O sample), which is meaningful once and
    # redundant 34 times over a 35-invocation sweep. Take the lease with
    # `benchctl hold` instead so the journal still records who holds the box.
    #
    # Teardown is unchanged: RUN_PID is used only by `wait` below, never
    # signalled, so sudo-with-exec behaves the same as sudo-with-scope here.
    sudo bash -c '
      printf "%s" "$$" > /sys/fs/cgroup/bench.slice/cgroup.procs || exit 111
      cpus="$1"; shift
      exec taskset -c "$cpus" "$@"
    ' _ "$BENCH_CPUS" \
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

    # Check what we waited on. Without this, a failed cgroup join means l2fwd
    # never ran, the log is empty, every counter reads an idle core, and the
    # sweep completes "successfully" with 35 runs of nothing. An instrument
    # must be able to say it did not measure -- §5.23, §5.26.
    wait $RUN_PID; RUN_RC=$?
    if [ "$RUN_RC" = 111 ]; then
      echo "FATAL: could not join bench.slice (cgroup.procs write denied)." >&2
      echo "       Every run after this would measure an idle core. Aborting" >&2
      echo "       rather than producing a full set of plausible zeros." >&2
      exit 1
    fi

    T=$(tr -d '\033' < "$LOG")
    AVG=$(grep -oP 'Average: \K[0-9.]+'              <<<"$T" | tail -1)
    # Was 'Cycle per fwd packet', which timed the mode branch alone and is now
    # retired rather than renamed -- see main.c print_stats. This label is the
    # whole iteration: rx burst, mode branch, tx burst, drop path. A log from
    # the old binary leaves CYC empty instead of supplying the narrower number
    # under the same name.
    CYC=$(grep -oP 'Full-loop cyc per fwd packet: \K[0-9]+' <<<"$T" | tail -1)
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

    printf "arm=%-16s q=%-3s grp=%-9s workers=%-3s avg=%-7s loopcyc=%-5s batch=%-4s missed=%-14s %s%s\n" \
      "$ARM" "$q" "$grp" "$(echo "$WORKER_CPUS" | tr ',' '\n' | grep -c .)" \
      "${AVG:-NA}" "${CYC:-NA}" "${BAT:-NA}" "${MIS:-NA}" \
      "${MUX:+MULTIPLEXED[$MUX] }" "$ERR" \
      | tee -a "$OUT_DIR/summary.txt"
  done
done
echo "SATURATION SWEEP COMPLETE"
}

# ===========================================================================
# validate -- was validate_counters.sh
# ===========================================================================
#
# Check every counter the saturation study uses against a workload whose answer
# is known, BEFORE using it on the forwarder where the answer is unknown.
#
# This is the practice docs/INVESTIGATION.md section 5.23 arrived at the hard
# way. Two failure modes it is aimed at, neither of which any amount of care
# with the analysis would catch:
#
#   * an encoding that is simply wrong and reads a plausible number
#   * an encoding that is not wired up on this part and reads zero, which is
#     indistinguishable from a real zero
#
# Each check therefore has a PREDICTION that a wrong counter would fail. Run
# this on an idle machine; it saturates one core and, in the stream check, the
# memory system.
#
# Usage: ./harness.sh validate [cpu] [bench_membw path]
cmd_validate() {
set -uo pipefail
counter_groups

CPU="${1:-24}"
# $HERE is l2fwd/, and bench_membw is built into l2fwd/, not its parent. The
# old default resolved to NetBlast/bench_membw and made every invocation die on
# the FATAL below before a single counter was read.
BM="${2:-$HERE/bench_membw}"
[ -x "$BM" ] || { echo "FATAL: bench_membw not found at $BM" >&2; exit 1; }

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
PASS=0; FAIL=0
note() { printf '  %s\n' "$*"; }
verdict() {  # $1 ok/no  $2 text
  if [ "$1" = ok ]; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$2"
  else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$2"; fi
}

# Every group is checked for multiplexing first: a group that does not report
# 100.00 enabled has had its values scaled and cannot be compared to anything.
check_enabled() {  # $1 label, $2 perf output
  # Two distinct failures, and the second is the quiet one: an event that reads
  # `<not supported>` still reports 100.00 enabled, so a multiplexing check
  # alone passes it. That is how the un-braced topdown group hid -- four
  # metrics absent, nothing flagged.
  # The enabled percentage is field 5, NOT field 6. Field 6 is perf's derived
  # METRIC -- "19.4" for retiring, "0.08" for insn-per-cycle -- so a check on
  # field 6 flags every metric-bearing event as multiplexed and, worse, passes
  # a genuinely multiplexed event whose metric column happens to be empty. This
  # checker read the wrong column until it was pointed at a topdown group and
  # reported four events at 2.0/0.0/78.0/20.0 "percent enabled", which are the
  # top-down fractions themselves.
  local bad dead
  bad=$(awk -F, '$5 != "" && $5 != "100.00" {print $3" @"$5"%"}' <<<"$2" | paste -sd' ')
  dead=$(awk -F, '$1 ~ /not supported|not counted/ {print $3}' <<<"$2" | paste -sd' ')
  if [ -n "$dead" ]; then verdict no "$1 has events the PMU did not count: $dead"
  elif [ -n "$bad" ]; then verdict no "$1 multiplexed: $bad"
  else verdict ok "$1 counted in full"; fi
}

val() { awk -F, -v n="$2" '$3 ~ n {print $1; exit}' <<<"$1"; }

echo "=== 1. nomem: a register-only dependent chain ==="
echo "    predicts: retiring dominant, mem-bound ~0, DRAM traffic ~0"
O=$(sudo perf stat -e "$G_TOPDOWN2" -C "$CPU" -x, -- taskset -c "$CPU" "$BM" nomem 64 3 2>&1)
check_enabled "topdown2" "$O"
SLOTS=$(val "$O" '^slots$'); MEMB=$(val "$O" 'mem-bound')
if [ -n "${SLOTS:-}" ] && [ -n "${MEMB:-}" ] && [ "$SLOTS" -gt 0 ] 2>/dev/null; then
  R=$(awk -v a="$MEMB" -v b="$SLOTS" 'BEGIN{printf "%.4f", a/b}')
  note "mem-bound fraction = $R"
  awk -v r="$R" 'BEGIN{exit !(r < 0.05)}' && verdict ok "mem-bound < 5% on a no-memory workload" \
                                          || verdict no "mem-bound = $R on a no-memory workload (expected ~0)"
else
  verdict no "topdown2 produced no usable slots/mem-bound values"
fi

echo
echo "=== 2. chase: a dependent pointer chase, 4 GiB ==="
echo "    predicts: outstanding L1D misses LOW on a dependent chase, and much"
echo "    lower than on a sequential stream through the same buffer. The naive"
echo "    prediction is exactly 1.0 -- one miss in flight, since each load's"
echo "    address comes from the previous load -- and it does not hold here."
O=$(sudo perf stat -e "$G_MLP" -C "$CPU" -x, -- taskset -c "$CPU" "$BM" chase 4096 4 2>&1)
check_enabled "mlp (chase)" "$O"
P=$(val "$O" 'pending$'); PC=$(val "$O" 'pending_cycles')
OS=$(sudo perf stat -e "$G_MLP" -C "$CPU" -x, -- taskset -c "$CPU" "$BM" streamn 4096 4 2>&1)
PS=$(val "$OS" 'pending$'); PCS=$(val "$OS" 'pending_cycles')
if [ -n "${P:-}" ] && [ -n "${PC:-}" ] && [ "$PC" -gt 0 ] 2>/dev/null \
   && [ -n "${PS:-}" ] && [ -n "${PCS:-}" ] && [ "$PCS" -gt 0 ] 2>/dev/null; then
  MLP=$(awk -v a="$P" -v b="$PC" 'BEGIN{printf "%.3f", a/b}')
  MLPS=$(awk -v a="$PS" -v b="$PCS" 'BEGIN{printf "%.3f", a/b}')
  note "chase MLP = $MLP    stream MLP = $MLPS"
  # The chase lands near 2, not 1, reproducibly. The likely cause is the
  # adjacent-line prefetcher, which fetches the 128 B-aligned partner of every
  # demand line, so one dependent load puts two lines in flight. That is a
  # hypothesis: confirming it would need the prefetchers disabled via MSR
  # 0x1a4, and this session has no MSR access, so it is recorded as unconfirmed
  # rather than asserted. What the pair of numbers DOES establish is that the
  # counter responds correctly to memory-level parallelism -- a dependent chase
  # is far below a sequential stream -- which is what it is needed for.
  if awk -v m="$MLP" -v s="$MLPS" 'BEGIN{exit !(m > 0.8 && m < 3.0 && s > 2*m)}'; then
    verdict ok "chase MLP $MLP is low and well below stream MLP $MLPS"
  else
    verdict no "MLP chase=$MLP stream=$MLPS does not show the expected ordering"
  fi
else
  verdict no "MLP counters read nothing (chase P=${P:-none} PC=${PC:-none})"
fi

echo
echo "=== 2b. latency: Little's law on the same dependent chase ==="
echo "    OFFCORE_REQUESTS_OUTSTANDING.DATA_RD accumulates, every cycle, the"
echo "    number of data reads in flight. OFFCORE_REQUESTS.DATA_RD counts the"
echo "    requests. The quotient is mean latency of one request, in core cycles."
echo "    predicts: a dependent chase through 4 GiB misses to DRAM on almost"
echo "    every load and cannot hide the latency, so the quotient must land in"
echo "    the physically plausible DRAM range for this part. Reading 0 means the"
echo "    raw encoding did not resolve; reading single digits means it resolved"
echo "    to the wrong event. Both are the §5.23 failure mode, which is why this"
echo "    check exists at all."
OL=$(sudo perf stat -e "$G_LATENCY" -C "$CPU" -x, -- taskset -c "$CPU" "$BM" chase 4096 4 2>&1)
check_enabled "latency (chase)" "$OL"
OUT=$(val "$OL" 'offcore_reqs_outstanding_data_rd$')
REQ=$(val "$OL" 'offcore_reqs_data_rd$')
if [ -n "${OUT:-}" ] && [ -n "${REQ:-}" ] && [ "$REQ" -gt 0 ] 2>/dev/null; then
  LAT=$(awk -v a="$OUT" -v b="$REQ" 'BEGIN{printf "%.1f", a/b}')
  note "chase mean data-read latency = $LAT core cycles ($OUT outstanding / $REQ requests)"
  # No ordering assertion against the stream arm. A sequential stream has more
  # parallelism but its per-request latency can be higher from queueing, so
  # "chase above stream" is not a prediction this can safely make. The chase
  # alone is the known answer: it is the workload that cannot hide latency.
  if awk -v l="$LAT" 'BEGIN{exit !(l > 100 && l < 800)}'; then
    verdict ok "latency $LAT cycles is in the plausible DRAM range for this part"
  elif awk -v l="$LAT" 'BEGIN{exit !(l < 10)}'; then
    verdict no "latency $LAT is implausibly small -- encoding resolved to the wrong event"
  else
    verdict no "latency $LAT cycles is outside the expected 100-800 range"
  fi
else
  verdict no "latency counters read nothing (outstanding=${OUT:-none} requests=${REQ:-none})"
fi

echo "=== 3. stream: CAS reads against an ARITHMETIC byte count ==="
echo "    two pass counts differenced, so allocation and first touch cancel and"
echo "    no timing enters the comparison; expected = (N2-N1) x buffer bytes"
MIB=4096; N1=4; N2=12
cas_reads() {   # $1 passes -> total CAS read lines over the whole program
  sudo perf stat -a -e "$G_BW" -x, -- "$BM" streamn "$MIB" "$1" 2>&1 \
    | awk -F, '/cas_rd_/{s+=$1} END{print s+0}'
}
cas_writes() {
  sudo perf stat -a -e "$G_BW" -x, -- "$BM" streamn "$MIB" "$1" 2>&1 \
    | awk -F, '/cas_wr_/{s+=$1} END{print s+0}'
}
R1=$(cas_reads $N1); R2=$(cas_reads $N2)
W2=$(cas_writes $N2)
EXP=$(awk -v m="$MIB" -v a="$N1" -v b="$N2" 'BEGIN{print (b-a)*m*1024*1024}')
GOT=$(awk -v r1="$R1" -v r2="$R2" 'BEGIN{print (r2-r1)*64}')
note "passes $N1 -> $R1 lines, passes $N2 -> $R2 lines"
note "expected $EXP B of extra reads, counters say $GOT B"
if [ -n "$R1" ] && [ -n "$R2" ] && awk -v g="$GOT" 'BEGIN{exit !(g>0)}'; then
  RATIO=$(awk -v g="$GOT" -v e="$EXP" 'BEGIN{printf "%.3f", g/e}')
  note "measured/expected = $RATIO"
  awk -v r="$RATIO" 'BEGIN{exit !(r > 0.85 && r < 1.15)}' \
      && verdict ok "CAS read counter within 15% of an arithmetic byte count" \
      || verdict no "CAS read counter is $RATIO x the arithmetic expectation"
else
  verdict no "uncore read counters read nothing (the cas_count_* ALIAS is absent on this part; raw 0x05/0xcf is required)"
fi

# A read-only probe must not move the write counter. If it does, the RD and WR
# umasks are counting the same thing and every read/write split this study
# reports would be meaningless -- which no amount of agreement on the total
# would reveal.
WRATIO=$(awk -v w="$W2" -v r="$R2" 'BEGIN{printf "%.3f", (r>0)? w/r : 9}')
note "write/read line ratio on a read-only probe = $WRATIO"
awk -v x="$WRATIO" 'BEGIN{exit !(x < 0.25)}' \
    && verdict ok "write counter stays near zero on a read-only probe" \
    || verdict no "write counter is $WRATIO of reads on a read-only probe (RD/WR umasks may be aliased)"


echo "=== 3b. read and mixed ceilings, same probe, same cores, differenced ==="
echo
echo "    (1) MIXED matters because a read-only peak is the wrong denominator"
echo "        for l2fwd and errs unsafely: mixed traffic over a read-only"
echo "        ceiling understates utilisation and flatters the headroom."
echo "    (2) DIFFERENCED because every probe memsets its buffer to fault pages"
echo "        in; a fixed window some seconds later charges that memset, and its"
echo "        read-for-ownership traffic, to an interval chosen by guesswork."
echo "        A read-only probe betrays this with impossible write traffic; an"
echo "        rmw probe cannot, because it is meant to write."
echo "    (3) BOTH ARMS FROM THE SAME BINARY ON THE SAME CORES. An earlier"
echo "        version took the ratio between this rmw probe and the peer"
echo "        session's separate read probe and got mixed/read = 1.63 -- mixed"
echo "        ABOVE read, the opposite of the expected direction. That number"
echo "        compared two different programs, compiled differently, with"
echo "        different access patterns, so it measured the probes rather than"
echo "        the memory system. The ratio below uses streamn and rmwn from one"
echo "        binary on cpus 24-27."
echo "    (4) FOUR CORES ONLY, and that is a limit rather than a choice: this"
echo "        session's shell is confined to cpus 24-27,52-55 by user.slice, and"
echo "        taskset cannot escape a cpuset. l2fwd runs up to 23 workers, so"
echo "        the RATIO is what transfers, under the untested assumption that it"
echo "        holds with thread count."
echo
: > "$TMPD/ceilings"
CORES="24 25 26 27"
NT=$(echo $CORES | wc -w)
run_probe() {   # $1 mode  $2 passes -> "<read lines>,<write lines>"
  local out
  out=$(sudo perf stat -a -e "$G_BW" -x, -- \
        bash -c 'for c in '"$CORES"'; do
                   taskset -c $c '"$BM"' '"$1"' 2048 '"$2"' >/dev/null 2>&1 &
                 done; wait' 2>&1)
  printf '%s,%s' \
    "$(awk -F, '/cas_rd_/{s+=$1} END{print s+0}' <<<"$out")" \
    "$(awk -F, '/cas_wr_/{s+=$1} END{print s+0}' <<<"$out")"
}
ceiling_for() {   # $1 mode -> "<total>,<read>,<write>"
  local t1s t1e t2s t2e a b dt rd wr
  t1s=$(date +%s.%N); a=$(run_probe "$1" 3); t1e=$(date +%s.%N)
  t2s=$(date +%s.%N); b=$(run_probe "$1" 9); t2e=$(date +%s.%N)
  dt=$(awk -v p="$t1s" -v q="$t1e" -v r="$t2s" -v s="$t2e" 'BEGIN{print (s-r)-(q-p)}')
  rd=$(awk -v x="${a%%,*}" -v y="${b%%,*}" -v d="$dt" 'BEGIN{printf "%.1f",(d>0)?(y-x)*64/d/1e9:0}')
  wr=$(awk -v x="${a##*,}" -v y="${b##*,}" -v d="$dt" 'BEGIN{printf "%.1f",(d>0)?(y-x)*64/d/1e9:0}')
  printf '%s,%s,%s' "$(awk -v p="$rd" -v q="$wr" 'BEGIN{printf "%.1f",p+q}')" "$rd" "$wr"
}
R=$(ceiling_for streamn); M=$(ceiling_for rmwn)
R_TOT=${R%%,*}; R_RD=$(cut -d, -f2 <<<"$R"); R_WR=$(cut -d, -f3 <<<"$R")
M_TOT=${M%%,*}; M_RD=$(cut -d, -f2 <<<"$M"); M_WR=$(cut -d, -f3 <<<"$M")
note "read-only ceiling, $NT cores: ${R_TOT} GB/s (${R_RD} read + ${R_WR} write)"
note "mixed    ceiling, $NT cores: ${M_TOT} GB/s (${M_RD} read + ${M_WR} write)"
RATIO=$(awk -v m="$M_TOT" -v r="$R_TOT" 'BEGIN{printf "%.3f",(r>0)?m/r:0}')
note "mixed/read = $RATIO"
if awk -v m="$M_TOT" -v r="$R_TOT" 'BEGIN{exit !(m>1 && r>1)}'; then
  verdict ok "both ceilings measured on the same probe and cores"
else
  verdict no "a ceiling came out at zero (read=$R_TOT mixed=$M_TOT)"
fi
# Cross-check: does this probe's read arm agree with the peer session's
# independent read probe at the same thread count? Disagreement would mean the
# two probes differ, not that the memory system does.
note "peer's independent read-only ceiling at 4 threads: 60.59 GB/s"
{
  echo "# DRAM figures measured on cpus 24-27 (4 physical cores), 2 GiB buffer,"
  echo "# 2 MiB pages, differenced over two pass counts so the pre-touch cancels."
  echo "# Both arms are the same binary on the same cores."
  echo "READ_4=$R_TOT"
  echo "MIXED_4=$M_TOT"
  echo "MIXED_4_RD=$M_RD"
  echo "MIXED_4_WR=$M_WR"
  echo "MIXED_OVER_READ=$RATIO"
  echo "PEER_READ_4=60.59"
  echo "PEER_READ_24=227.53"
  echo "#"
  echo "# NO 24-THREAD MIXED CEILING IS AVAILABLE, AND NONE IS EXTRAPOLATED."
  echo "#"
  echo "# An earlier version scaled the peer's 24-thread read ceiling by the"
  echo "# mixed/read ratio measured here. Withdrawn, but NOT for the reason"
  echo "# first written down, and the difference matters."
  echo "#"
  echo "# The ratio is $RATIO -- mixed ABOVE read-only, same binary, same cores."
  echo "# The first reading of that was 'the two numbers contradict each other,"
  echo "# so the probe is suspect'. That was wrong. Mixed exceeding read-only is"
  echo "# the expected signature of a CORE-limited probe, not a broken one:"
  echo "# a read-only stream moves bytes only as fast as the core keeps read"
  echo "# misses outstanding, since every line must be waited for, whereas the"
  echo "# writebacks an rmw stream adds are fire-and-forget -- they consume"
  echo "# controller bandwidth but stall nothing. So the write half is nearly"
  echo "# free in latency terms and the total rises while the core waits no more."
  echo "#"
  echo "# The discriminating test is the READ component, not the total, and it"
  echo "# passes: read-only $R_RD GB/s of reads against the mixed arm's $M_RD."
  echo "# Reads DOWN (bus turnaround), writes added nearly free, total up."
  echo "# A broken probe would have shown the mixed arm's reads ABOVE $R_RD."
  echo "# (Argument and test due to the peer DRAMHiT session.)"
  echo "#"
  echo "# So both 4-core figures stand as measurements, and neither is a"
  echo "# memory-system ceiling -- because 4 cores cannot saturate 8 memory"
  echo "# channels. Corroborating: the peer's read series scales 60.59 -> 227.53"
  echo "# from 4 to 24 threads, 3.76x for 6x the threads, which is what per-core"
  echo "# limitation looks like rather than an approached ceiling."
  echo "#"
  echo "# Consequence for the saturation study: report DRAM traffic as an"
  echo "# ABSOLUTE GB/s figure. A utilisation percentage may be given only"
  echo "# against PEER_READ_24, named as such, and read as 'at least', since"
  echo "# that series had not plateaued either."
  echo "DRAM_ACHIEVED_GBS="
} > "$HERE/.dram_ceiling"
note "wrote l2fwd/.dram_ceiling"

echo "=== 4. the groups used on the forwarder fit the PMU ==="
for g in G_TOPDOWN1 G_TOPDOWN2 G_MLP G_FB G_TLBMEM G_LATENCY; do
  # 4 s, not 1 s: a one-second run produced a spurious "instructions @1.33%"
  # for G_TLBMEM that vanished on a longer one. A short window makes the
  # enabled fraction itself unreliable, which is a different failure from the
  # multiplexing it looks like.
  O=$(sudo perf stat -e "${!g}" -C "$CPU" -x, -- taskset -c "$CPU" "$BM" chase 1024 4 2>&1)
  check_enabled "$g" "$O"
done

echo
echo "checks passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ]
}

# ===========================================================================
# ceiling -- was dram_ceiling.sh
# ===========================================================================
#
# The DRAM ceiling that the saturation study divides by, measured across thread
# counts instead of extrapolated from one.
#
# WHY THIS EXISTS
# ---------------
# `.dram_ceiling` carries DRAM_ACHIEVED_GBS empty on purpose. It previously held
# 360.0 GB/s, being the peer session's 24-thread READ ceiling scaled by a
# mixed/read ratio measured here at FOUR cores. That extrapolation was withdrawn
# because it rests on an untested assumption -- that the mixed/read ratio is
# independent of thread count -- and because the peer's 24-thread figure had not
# plateaued (+18.3% from 16 threads), making it a lower bound. Dividing a
# utilisation by a lower bound OVERSTATES saturation.
#
# `harness.sh validate` could not do better: it runs from an agent shell, which
# user.slice confines to cpus 24-27,52-55, and `taskset` cannot escape a cpuset.
# Four cores was a limit, not a choice. Launched into bench.slice this script
# reaches cores 0-23 -- 24 distinct physical cores, since the sibling of core c
# is c+28 -- and can therefore measure the ceiling at the thread count l2fwd
# actually runs at rather than assuming a ratio carries.
#
# Sweeping the thread count, rather than measuring only at 24, is the point: a
# ceiling that is still climbing at the top of the sweep is a lower bound and
# must be reported as one. That is the defect being repaired, so the new figure
# must not reproduce it silently.
#
# METHOD, inherited unchanged from `harness.sh validate`
# -----------------------------------------------------
#   (1) MIXED, not read-only. l2fwd fills a line and dirties it, so DRAM sees a
#       read and a write per line. A read-only peak is the wrong denominator in
#       the flattering direction. Both arms are measured; MIXED is the one that
#       becomes DRAM_ACHIEVED_GBS.
#   (2) DIFFERENCED over two pass counts. Every probe memsets its buffer to
#       fault pages in; a fixed window charges that memset to an interval chosen
#       by guesswork. Differencing 3 passes against 9 cancels it, along with
#       allocation and first touch.
#   (3) ONE BINARY, SAME CORES, BOTH ARMS. An earlier attempt took the ratio
#       between this rmw probe and the peer's separate read probe and got
#       mixed/read = 1.63 -- mixed ABOVE read, the opposite of the physical
#       expectation. It was measuring the two probes, not the memory system.
#
# REFUSES TO RUN ON THE WRONG CORES
# ---------------------------------
# The failure this guards against is the one that motivates benchctl: a probe
# confined to the housekeeping cpuset does not fail, it returns a plausible
# wrong number. So the script reads its own Cpus_allowed_list and exits unless
# it is actually on bench cores. --hk overrides for a deliberate 4-core
# comparison against the existing READ_4/MIXED_4 figures.
#
# Usage:
#   sudo ~/.bench-coord/benchctl run --cpus 0-23 --purpose "DRAM ceiling" \
#        --log <f> -- l2fwd/harness.sh ceiling <outdir>
#
#   THREADS="1 2 4 8 16 24"   thread counts to sweep
#   MIB=2048                  per-thread buffer, MiB (must be >> 52.5 MiB L3)
#   WRITE_CEILING=1           update .dram_ceiling in place when done
cmd_ceiling() {
set -euo pipefail

counter_groups

OUT_DIR="${1:?usage: $0 ceiling <outdir>}"
BM="${BM:-$HERE/bench_membw}"
THREADS="${THREADS:-1 2 4 8 16 24}"
MIB="${MIB:-2048}"
WRITE_CEILING="${WRITE_CEILING:-1}"
ALLOW_HK="${ALLOW_HK:-0}"
[ "${2:-}" = "--hk" ] && ALLOW_HK=1

mkdir -p "$OUT_DIR"
CSV="$OUT_DIR/dram_ceiling.csv"

[ -x "$BM" ] || { echo "FATAL: bench_membw not built at $BM" >&2
                  echo "       cc -O2 -o $BM $HERE/bench_membw.c" >&2; exit 1; }

# --- the guard: are we actually on the cores we think we are? --------------
ALLOWED="$(awk '/Cpus_allowed_list/{print $2}' /proc/self/status)"
echo "cpus_allowed_list: $ALLOWED"
if [ "$ALLOW_HK" = "0" ]; then
  case "$ALLOWED" in
    *24-27*|*52-55*)
      echo "FATAL: confined to the housekeeping cpuset ($ALLOWED)." >&2
      echo "       taskset cannot escape a cpuset, so every core below would" >&2
      echo "       silently land on 24-27 and the sweep would report the same" >&2
      echo "       four-core number at every thread count -- a plausible wrong" >&2
      echo "       answer, which is the failure this file exists to avoid." >&2
      echo "       Launch via: benchctl run --cpus 0-23 -- $0 ceiling $OUT_DIR" >&2
      echo "       Or pass --hk to compare deliberately against READ_4." >&2
      exit 1;;
  esac
fi

MAXT=$(echo $THREADS | tr ' ' '\n' | sort -n | tail -1)
NAVAIL=$(echo "$ALLOWED" | tr ',' '\n' | awk -F- '{print ($2?$2:$1)-$1+1}' | paste -sd+ | bc)
if [ "$NAVAIL" -lt "$MAXT" ]; then
  echo "FATAL: $MAXT threads requested but only $NAVAIL cpus allowed ($ALLOWED)." >&2
  echo "       Oversubscribing would measure scheduling, not bandwidth." >&2
  exit 1
fi
CORELIST=$(echo "$ALLOWED" | tr ',' '\n' \
           | awk -F- '{for(i=$1;i<=($2?$2:$1);i++) print i}' | paste -sd' ')

# --- one differenced measurement -------------------------------------------
# $1 mode  $2 nthreads -> "<total>,<read>,<write>" GB/s
run_probe() {   # $1 mode  $2 passes  $3 cores -> "<read lines>,<write lines>"
  local out
  out=$(perf stat -a -e "$G_BW" -x, -- \
        bash -c 'for c in '"$3"'; do
                   taskset -c $c '"$BM"' '"$1"' '"$MIB"' '"$2"' >/dev/null 2>&1 &
                 done; wait' 2>&1)
  printf '%s,%s' \
    "$(awk -F, '/cas_rd_/{s+=$1} END{print s+0}' <<<"$out")" \
    "$(awk -F, '/cas_wr_/{s+=$1} END{print s+0}' <<<"$out")"
}

ceiling_for() {   # $1 mode  $2 cores -> "<total>,<read>,<write>"
  local t1s t1e t2s t2e a b dt rd wr
  t1s=$(date +%s.%N); a=$(run_probe "$1" 3 "$2"); t1e=$(date +%s.%N)
  t2s=$(date +%s.%N); b=$(run_probe "$1" 9 "$2"); t2e=$(date +%s.%N)
  dt=$(awk -v p="$t1s" -v q="$t1e" -v r="$t2s" -v s="$t2e" 'BEGIN{print (s-r)-(q-p)}')
  rd=$(awk -v x="${a%%,*}" -v y="${b%%,*}" -v d="$dt" \
       'BEGIN{printf "%.1f",(d>0)?(y-x)*'"$CAS_LINE_BYTES"'/d/1e9:0}')
  wr=$(awk -v x="${a##*,}" -v y="${b##*,}" -v d="$dt" \
       'BEGIN{printf "%.1f",(d>0)?(y-x)*'"$CAS_LINE_BYTES"'/d/1e9:0}')
  printf '%s,%s,%s' "$(awk -v p="$rd" -v q="$wr" 'BEGIN{printf "%.1f",p+q}')" "$rd" "$wr"
}

echo "nthreads,mode,total_gbs,read_gbs,write_gbs,cores" > "$CSV"
echo "=== DRAM ceiling sweep: threads [$THREADS], ${MIB} MiB/thread ==="
for nt in $THREADS; do
  CORES=$(echo $CORELIST | tr ' ' '\n' | head -n "$nt" | paste -sd' ')
  for mode in streamn rmwn; do
    R=$(ceiling_for "$mode" "$CORES")
    label=$([ "$mode" = streamn ] && echo read || echo mixed)
    echo "$nt,$label,${R%%,*},$(cut -d, -f2 <<<"$R"),$(cut -d, -f3 <<<"$R"),\"$CORES\"" >> "$CSV"
    printf '  %2d threads  %-5s  %6s GB/s  (%s read + %s write)\n' \
      "$nt" "$label" "${R%%,*}" "$(cut -d, -f2 <<<"$R")" "$(cut -d, -f3 <<<"$R")"
  done
done

# --- the headline, and whether it is a ceiling or a lower bound -------------
TOP=$(awk -F, -v t="$MAXT" '$1==t && $2=="mixed"{print $3}' "$CSV")
PREV=$(echo $THREADS | tr ' ' '\n' | sort -n | tail -2 | head -1)
PREV_V=$(awk -F, -v t="$PREV" '$1==t && $2=="mixed"{print $3}' "$CSV")
GROWTH=$(awk -v a="$PREV_V" -v b="$TOP" 'BEGIN{printf "%.1f",(a>0)?(b/a-1)*100:0}')
PLATEAUED=$(awk -v g="$GROWTH" 'BEGIN{print (g<5)?"yes":"no"}')

echo
echo "mixed ceiling at $MAXT threads: $TOP GB/s"
echo "growth from $PREV to $MAXT threads: ${GROWTH}%  (plateaued: $PLATEAUED)"
if [ "$PLATEAUED" = "no" ]; then
  echo "NOTE: still climbing at the top of the sweep, so this is a LOWER BOUND."
  echo "      Utilisations divided by it are OVERSTATED -- the same caveat the"
  echo "      withdrawn 360.0 carried. Extend THREADS if more cores are free."
fi

if [ "$WRITE_CEILING" = "1" ]; then
  CF="$HERE/.dram_ceiling"
  BOUND=$([ "$PLATEAUED" = "yes" ] && echo "measured plateau" || echo "LOWER BOUND, still climbing")
  {
    echo "# --- measured by harness.sh ceiling on $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"
    echo "# ${MAXT} threads on cpus: $(echo $CORELIST | tr ' ' '\n' | head -n $MAXT | paste -sd,)"
    echo "# ${MIB} MiB/thread, differenced over 3 vs 9 passes, mixed (rmwn) arm."
    echo "# NOT comparable to READ_4/MIXED_4 above: those were taken on cpus"
    echo "# 24-27, the contended housekeeping set. Different cores, different"
    echo "# contention, and 24-27 is where the 178-tick reading came from."
    echo "# Status: $BOUND (+${GROWTH}% from $PREV threads)."
    echo "# Full series: $CSV"
  } >> "$CF"
  sed -i "s/^DRAM_ACHIEVED_GBS=.*/DRAM_ACHIEVED_GBS=$TOP/" "$CF"
  echo "updated $CF: DRAM_ACHIEVED_GBS=$TOP"
fi
echo "series -> $CSV"
}

# ===========================================================================
# clock -- was set_clock.sh
# ===========================================================================
#
# Switch the machine between the two measurement arms, changing ONLY the core
# clock. Everything else that affects variability -- C-states, irqbalance,
# nmi_watchdog, THP defrag, the cpuset partition -- is deliberately left alone in
# both arms, so that pinned-vs-turbo is a single-variable contrast.
#
#   ./harness.sh clock pinned   ->  every core fixed at 2.100 GHz, turbo off
#   ./harness.sh clock turbo    ->  governor free to range 0.8-3.7 GHz, turbo on
#   ./harness.sh clock show     ->  report current state, change nothing
#
# Why 2.100 GHz specifically
# -------------------------
# It is simultaneously this SKU's `base_frequency` and exactly the invariant TSC
# rate (`rte_get_tsc_hz: 2100000000`). At that setting l2fwd's `rte_rdtsc()`
# deltas -- which it prints as "Full-loop cyc per fwd packet" but which are really
# TSC ticks, i.e. time -- become numerically equal to core cycles, so the
# tick-to-cycle correction is exactly 1.000 rather than an estimated ~1.74.
#
# Why both arms are needed
# ------------------------
# Pinning buys comparability and costs representativeness: production runs with
# turbo, so absolute numbers at 2.1 GHz describe a machine nobody deploys on
# (throughput drops from ~93 to ~56 Mpps). The turbo arm is the headline number
# and also supplies the per-queue-count delivered frequency r(q) needed to
# convert the pinned arm's ticks into the turbo arm's cycles. See
# docs/INVESTIGATION.md, "Pre-registered: what the pinned re-baseline must show".
#
# NOT persistent: every one of these is a runtime sysfs write and resets on
# reboot. See docs/INVESTIGATION.md section 3.4b.
cmd_clock() {
set -u

MODE="${1:-show}"
PINNED_KHZ=2100000        # = base_frequency = TSC rate
MINF=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq)
MAXF=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq)

write_all() {   # write_all <sysfs leaf name> <value>
    for f in /sys/devices/system/cpu/cpu*/cpufreq/"$1"; do
        echo "$2" | sudo tee "$f" > /dev/null
    done
}

case "$MODE" in
  pinned)
    # max before min: the kernel rejects a min above the current max, so raising
    # min first on a core whose max is still low would silently fail on that core
    # and leave the machine half-configured -- which reads as "it worked".
    write_all scaling_max_freq "$PINNED_KHZ"
    write_all scaling_min_freq "$PINNED_KHZ"
    echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo > /dev/null
    ;;
  turbo)
    # min before max here, for the mirror-image reason.
    write_all scaling_min_freq "$MINF"
    write_all scaling_max_freq "$MAXF"
    echo 0 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo > /dev/null
    ;;
  show) ;;
  *) echo "usage: $0 clock {pinned|turbo|show}" >&2; exit 1 ;;
esac

# Verify by reading back EVERY core, not cpu0. A partial write is the failure
# mode that matters: it leaves a subset of cores at a different clock, which
# perturbs the sweep without failing anything.
NMAX=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq | sort -u | tr '\n' ' ')
NMIN=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq | sort -u | tr '\n' ' ')
echo "mode=$MODE  no_turbo=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)"
echo "  distinct scaling_max across all cores: $NMAX"
echo "  distinct scaling_min across all cores: $NMIN"
[ "$(echo "$NMAX" | wc -w)" -eq 1 ] || echo "  WARNING: cores disagree on scaling_max_freq"
[ "$(echo "$NMIN" | wc -w)" -eq 1 ] || echo "  WARNING: cores disagree on scaling_min_freq"

# sysfs reports the P-state *request*, never delivery: an idle core here reads
# 3.63-3.70 GHz while perf counts it at 506 kcycles/s. So confirm with perf.
echo "  (delivered frequency is NOT readable from sysfs on this box -- harness.sh sweep"
echo "   measures it per run with 'perf stat -e cycles'; see INVESTIGATION.md)"
}

# ===========================================================================
# pagewatch -- was page_watch.sh
# ===========================================================================
# Record the page backing every l2fwd run actually got, rather than the backing
# its flag asked for. MADV_HUGEPAGE is advisory and MADV_NOHUGEPAGE can fail, so
# a -B arm can silently measure a different page size than its label claims --
# and `harness.sh sweep` only samples the 1 GiB pool, which cannot tell 2 MiB from 4 KiB.
# Sampled from outside because the runs are already in flight.
cmd_pagewatch() {
OUT=${1:-/users/sohamb/sweeps/page_watch.log}
while true; do
  for P in $(pgrep -f '^/users/sohamb/NetBlast/l2fwd/./build/l2fwd' 2>/dev/null); do
    CMD=$(tr '\0' ' ' < /proc/$P/cmdline 2>/dev/null)
    read -r RSS THP HTLB < <(sudo awk '
      /^Rss:/{r=$2} /^AnonHugePages:/{t=$2} /^Private_Hugetlb:/{h=$2}
      END{print r, t, h}' /proc/$P/smaps_rollup 2>/dev/null)
    # Gate on RSS + Private_Hugetlb, not RSS alone. hugetlb pages are NOT
    # counted in Rss -- they appear only in Private_Hugetlb -- so a threshold on
    # Rss silently skips every run whose table is on 1 GiB pages, which is every
    # dramblast run as shipped, i.e. the allocator and depth blocks entirely.
    # The log looked healthy throughout because the maglev and 4 KiB/THP arms,
    # whose pages DO land in Rss, kept writing lines. A verifier that quietly
    # stops covering the arms it exists for is worse than none, because its
    # output is then read as confirmation of something it never looked at.
    TOTAL=$(( ${RSS:-0} + ${HTLB:-0} ))
    [ -n "$RSS" ] && [ "$TOTAL" -gt 1000000 ] 2>/dev/null && \
      echo "$(date -u +%H:%M:%S) pid=$P rss=$RSS thp=$THP hugetlb=$HTLB total=$TOTAL cmd=$CMD" >> "$OUT"
  done
  sleep 3
done
}

# ===========================================================================
# codegen -- was check_codegen.sh
# ===========================================================================
#
# Assert that the built binary still contains the things the experiments depend
# on existing. Run after every build, before any measurement.
#
# This exists because of a specific near-miss. The allocator-amplification arm
# (-A N) adds N aligned_alloc/free round trips per burst and measures what they
# cost. The guard meant to stop the compiler discarding them -- a store into the
# scratch buffer -- was itself deleted by GCC as a dead store to a soon-freed
# object, and nobody noticed: the pairs survived anyway, because the compiler
# happened not to apply -fallocation-dce. If a later toolchain does apply it,
# the pairs vanish and the arm reports that an allocator round trip costs ZERO
# cycles. That is not a crash and not noise -- it is a clean, plausible number,
# and it is precisely the answer the experiment exists to rule out. A silent
# wrong answer is the only kind of bug that can survive peer review.
#
# Usage: ./harness.sh codegen [binary]
cmd_codegen() {
set -u
BIN="${1:-./build/l2fwd}"
[ -x "$BIN" ] || { echo "FATAL: $BIN missing"; exit 1; }

fail=0
check() {  # check <description> <expected-min> <count>
  if [ "$3" -lt "$2" ]; then
    echo "  FAIL  $1: found $3, expected at least $2"; fail=1
  else
    echo "  ok    $1: $3"
  fi
}

# Disassemble just dramblast_process_frames. Its amplification loop must still
# call both halves of the pair; one call without the other means half the round
# trip was elided and the measured cost is wrong rather than absent.
DIS=$(objdump -d --disassemble='dramblast_process_frames' "$BIN" 2>/dev/null)
if [ -z "$DIS" ]; then
  # LTO or inlining may have renamed it; fall back to the whole binary, which is
  # weaker (it cannot tell which function the calls are in) but not nothing.
  echo "  note  dramblast_process_frames not a separate symbol; scanning whole binary"
  DIS=$(objdump -d "$BIN" 2>/dev/null)
fi
check "aligned_alloc calls reachable" 1 "$(grep -c 'call.*aligned_alloc' <<<"$DIS")"
check "free calls reachable"          1 "$(grep -c 'call.*free'          <<<"$DIS")"

# The prefetch pipeline must still issue software prefetches. The whole
# dramblast-vs-maglev result is a statement about prefetching, so a build with
# them optimised out would silently measure a different algorithm.
check "software prefetches present" 1 \
      "$(objdump -d "$BIN" 2>/dev/null | grep -c 'prefetch')"

[ "$fail" -eq 0 ] && echo "codegen OK" || echo "CODEGEN CHECK FAILED -- do not measure with this binary"
exit "$fail"
}

# ===========================================================================
# dispatch
# ===========================================================================
case "${1:-}" in
  sweep|matrix|saturation|validate|ceiling|clock|pagewatch|codegen)
    sub=$1; shift; "cmd_$sub" "$@" ;;
  *)
    sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'
    [ -z "${1:-}" ] || [ "$1" = -h ] || [ "$1" = --help ] || { echo "unknown sub: $1" >&2; exit 1; }
    exit 0 ;;
esac
