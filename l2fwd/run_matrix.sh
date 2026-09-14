#!/bin/bash
#
# The experiment matrix that follows the two clock arms.
#
# Each block below answers one question, and each is a wrapper around sweep.sh
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
#   d8 d16    PIPELINE DEPTH. The other candidate for C. A burst of B packets
#   d32       can only fill min(B, depth) slots of the prefetch queue, so short
#             bursts run a pipeline that never reaches steady state. If C is
#             that ramp it must fall as the depth falls, while P rises because
#             less latency is hidden. If C is the allocator, depth cannot touch
#             it. The two knobs cannot mimic each other, which is the point.
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
# Usage: ./run_matrix.sh [block ...]      (default: all blocks, in order)
#   e.g. ./run_matrix.sh control crossover
set -u
cd "$(dirname "$0")"

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
  L2FWD_EXTRA="$*" ./sweep.sh "$OUT/$tag" "$tag" "$mode" 2>&1 | tee "$OUT/${tag}_${mode}.out"
}

for b in "${BLOCKS[@]}"; do
 case $b in
  control)
    ( run pinned2 dramblast )
    ( run pinned2 maglev ) ;;
  crossover)
    # 4 KiB needs a longer settle: zeroing 8 GiB through 2M page faults instead
    # of 8 is slow, and sweep.sh samples at a fixed offset from launch.
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
  depth)
    ( run d8  dramblast -Q 8 )
    ( run d16 dramblast -Q 16 )
    ( run d32 dramblast -Q 32 ) ;;
  *) echo "unknown block: $b" >&2; exit 1 ;;
 esac
done
echo "MATRIX COMPLETE"
