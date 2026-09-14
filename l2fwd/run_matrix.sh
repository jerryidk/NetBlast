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
#   d8 d16    PIPELINE DEPTH. The other candidate for C. A burst of B packets
#   d32       can only fill min(B, depth) slots of the prefetch queue, so short
#             bursts run a pipeline that never reaches steady state. If C is
#             that ramp it must fall as the depth falls, while P rises because
#             less latency is hidden. If C is the allocator, depth cannot touch
#             it. The two knobs cannot mimic each other, which is the point.
#
# The allocator and depth blocks use a reduced queue list: fitting P and C needs
# a spread of burst sizes, not every queue count, and q in {1,4,7,8,9,10} spans
# batch 64 down to 8. That is 6 runs per condition instead of 10.
#
# Usage: ./run_matrix.sh [block ...]      (default: all blocks, in order)
#   e.g. ./run_matrix.sh control crossover
set -u
cd "$(dirname "$0")"

OUT=${OUT:-/users/sohamb/sweeps}
BLOCKS=("${@:-control crossover alloc depth}")
[ "$#" -eq 0 ] && BLOCKS=(control crossover alloc depth)

# Every invocation goes through a subshell. A `VAR=x run ...` prefix would NOT
# be scoped to the call: bash keeps variable assignments that precede a shell
# FUNCTION in the shell environment afterwards (unlike for external commands),
# so QUEUES set for the allocator block would silently leak into anything that
# ran after it and quietly halve those sweeps.
run() {  # run <tag> <mode> <extra args...>
  local tag=$1 mode=$2; shift 2
  echo "### $tag  mode=$mode  extra='$*'  $(date -u +%H:%M:%S)"
  L2FWD_EXTRA="$*" ./sweep.sh "$OUT/$tag" "$tag" "$mode" 2>&1 | tee "$OUT/$tag.out"
}

FEW="1 4 7 8 9 10"

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
    ( export QUEUES="$FEW"; run ahoist dramblast -A -1 )
    ( export QUEUES="$FEW"; run a2     dramblast -A 2 )
    ( export QUEUES="$FEW"; run a4     dramblast -A 4 )
    ( export QUEUES="$FEW"; run a8     dramblast -A 8 ) ;;
  depth)
    ( export QUEUES="$FEW"; run d8  dramblast -Q 8 )
    ( export QUEUES="$FEW"; run d16 dramblast -Q 16 )
    ( export QUEUES="$FEW"; run d32 dramblast -Q 32 ) ;;
  *) echo "unknown block: $b" >&2; exit 1 ;;
 esac
done
echo "MATRIX COMPLETE"
