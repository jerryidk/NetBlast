#!/bin/bash
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
# Usage: ./check_codegen.sh [binary]
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
