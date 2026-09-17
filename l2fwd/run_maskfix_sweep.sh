#!/usr/bin/env bash
#
# Does the find-mask fix hold on the rig?
#
# docs/INVESTIGATION.md section 5.26 establishes that dramblast's SIMD find path
# compares the search key against the value lanes and therefore misses on every
# packet, and measures the fix as worth 17.3 TSC ticks/packet in a userspace
# microbenchmark on a 256 MiB table. That benchmark has no NIC, a table a
# thirty-second the size, and 2 MiB pages instead of 1 GiB, so it can rank the
# arms but cannot give the rig's number. This runs the same comparison on the
# real forwarder.
#
# Two binaries, built beforehand and differing in exactly one constant (verified
# by disassembly: `mov $0xffffffaa,%eax` against `mov $0x55,%eax`), are swapped
# into build/l2fwd in turn. They are prebuilt rather than rebuilt between arms
# so that a build failure cannot leave one arm silently running the other's
# binary.
#
# BUILDING THE TWO ARMS (inverted 2026-09-17). The find-mask fix is now applied
# upstream -- master 2d8e93b carries DRAMBLAST_SIMD_KEY_MASK 0b01010101 -- so
# the tree is the *fixed* code and it is the `shipped` arm that must now be
# synthesised by editing the mask back. Before the rebase this was the other
# way round, and building `shipped` from an unmodified tree would now silently
# produce two identical binaries.
#
#   # maskfix: the unmodified tree
#   meson compile -C build && cp build/l2fwd <bindir>/l2fwd.maskfix
#
#   # shipped: revert the mask, build, then restore the tree
#   sed -i 's/DRAMBLAST_SIMD_KEY_MASK 0b01010101/DRAMBLAST_SIMD_KEY_MASK 0b10101010/' \
#       libsashstore/dramblast.h
#   meson compile -C build && cp build/l2fwd <bindir>/l2fwd.shipped
#   git checkout -- libsashstore/dramblast.h
#
# The objdump guard below is unchanged: it checks each binary for the mask it
# is supposed to carry, so it catches the mistake in either direction.
#
# The arms are interleaved within each repeat rather than run as two blocks, so
# that drift over the half hour lands on both equally -- the same reasoning as
# section 5.14, which had to redo a result taken in blocks.
#
# Queue counts are limited to 1..5 because the comparison is made at a matched
# RX burst of 64 and those are the counts that stay there; beyond that the
# forwarder stops being oversubscribed and the burst size becomes a second
# variable.
#
# Usage: ./run_maskfix_sweep.sh <bindir> <outdir> [repeats]
#   <bindir> must contain l2fwd.shipped and l2fwd.maskfix
set -euo pipefail

BINDIR="${1:?usage: $0 <bindir> <outdir> [repeats]}"
OUTDIR="${2:?usage: $0 <bindir> <outdir> [repeats]}"
REPEATS="${3:-3}"
HERE="$(cd "$(dirname "$0")" && pwd)"

for arm in shipped maskfix; do
  [ -x "$BINDIR/l2fwd.$arm" ] || { echo "FATAL: $BINDIR/l2fwd.$arm missing" >&2; exit 1; }
done
mkdir -p "$OUTDIR"

# Refuse to run if the binaries are not the two arms they claim to be. A sweep
# that silently compares a binary against itself would produce a clean null and
# look like a real result.
sh_mask=$(objdump -d --no-show-raw-insn "$BINDIR/l2fwd.shipped" | grep -c 'mov *\$0xffffffaa,%eax' || true)
mf_mask=$(objdump -d --no-show-raw-insn "$BINDIR/l2fwd.maskfix" | grep -c 'mov *\$0x55,%eax' || true)
if [ "$sh_mask" -lt 1 ] || [ "$mf_mask" -lt 1 ]; then
  echo "FATAL: binaries do not carry the expected masks (shipped=$sh_mask maskfix=$mf_mask)" >&2
  exit 1
fi

for r in $(seq 1 "$REPEATS"); do
  for arm in shipped maskfix; do
    echo "=== repeat $r, arm $arm ==="
    cp "$BINDIR/l2fwd.$arm" "$HERE/build/l2fwd"
    QUEUES="1 2 3 4 5" "$HERE/sweep.sh" "$OUTDIR" "r${r}_${arm}" dramblast \
      | tee -a "$OUTDIR/summary.txt"
  done
done

# Leave the shipped binary in place, so a later run that forgets to choose an
# arm gets the as-shipped one rather than whichever ran last.
cp "$BINDIR/l2fwd.shipped" "$HERE/build/l2fwd"
echo "ALL SWEEPS COMPLETE"
