#!/usr/bin/env bash
# Build and run the dramblast lookup-path microbenchmark in several arms.
#
# The arms differ only in one-line edits to a copy of the real source, so each
# arm is the tree plus exactly one change. The copies live under a scratch
# directory; nothing here modifies the tree.
#
# NOTE ON DIRECTION (inverted 2026-09-17): the find-mask fix is now applied
# upstream -- master 2d8e93b carries DRAMBLAST_SIMD_KEY_MASK 0b01010101 -- so
# the tree is the *fixed* code and it is the `shipped` arm that is now
# synthesised by editing the mask back to 0b10101010. Before the rebase this
# was the other way round. Every arm below that used to be "maskfix plus one
# change" is therefore the unmodified tree plus one change.
#
#   shipped        mask edited back to 0b10101010 (the defect as originally
#                  shipped), one alloc/free per burst
#   maskfix        the unmodified tree: mask 0b01010101, comparing against the
#                  key lanes rather than the value lanes
#   maskfix+hoist  as maskfix, plus -A -1's per-lcore result buffer
#   +prefetchT1    as the tree, with the prefetch hint constants corrected. The
#                  table in dramblast.c is the reverse of the ISA encoding
#                  (_MM_HINT_T0 is 3, not 0), so the shipped `PREFETCH_T1`
#                  emits `prefetcht2`; this arm emits the `prefetcht1` the
#                  source says it wants.
#   +prefetchT0    same, but the `prefetcht0` that fills L1 as well
#   +hoist         as the tree, plus opt/dramblast-hoist.patch: the queue
#                  pointer, table pointer, table mask and the queue head and
#                  tail read once into locals instead of being re-derived
#                  through `ht` on every iteration
#   +vecspill      the inverse of the old `novecspill` arm. Reading the hit
#                  value from the table line is now what the tree does (master
#                  2d8e93b applied it), so this arm re-introduces the
#                  `cacheline[(offset + 1)]` subscript of the __m512i, which
#                  GCC implements by spilling all 64 bytes to the stack and
#                  reloading 8 of them (a store-forwarding stall on every hit).
#                  The measured direction is unchanged -- the spill is worse --
#                  it is now the arm rather than the baseline that carries it.
#
# Usage: ./check_dramblast_arms.sh [scratch-dir]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
L2="$(dirname "$HERE")"   # archive/ -> l2fwd/
OUT="${1:-${TMPDIR:-/tmp}/dramblast_arms}"
DPDK_INC="$(sed -n 's/.*-I\(\/nix\/store\/[^ ]*dpdk[^ ]*\/include\).*/\1/p' \
             "$L2/build/compile_commands.json" | head -1)"
CFLAGS="-O3 -g -mavx512f -mavx512dq -march=native -msse4.2
        -DALLOW_EXPERIMENTAL_API -D_GNU_SOURCE -include rte_config.h
        -I$DPDK_INC"
SRCS="dramblast.c backing.c conshash.c hash.c packettool.c"

build_arm() {                      # $1 arm name, $2 sed program for the header
  local arm="${1}" edit="${2:-}" d="$OUT/${1}"
  rm -rf "$d"; mkdir -p "$d"
  cp -r "$L2/libsashstore" "$d/"
  # The drivers are copied in too, not compiled out of the tree. A `#include
  # "libsashstore/dramblast.h"` is resolved relative to the *including file*
  # first, so a driver left in $HERE would read the unpatched header while the
  # library sources beside it read the patched one -- which does not change the
  # code under test, but did make the maskfix arm print `mask=0xaa` and label
  # itself as the arm it was not.
  cp "$HERE/bench_dramblast_path.c" "$HERE/test_dramblast_find.c" "$d/"
  [ -n "$edit" ] && sed -i "$edit" "$d/libsashstore/dramblast.h"
  # shellcheck disable=SC2086
  gcc $CFLAGS -I"$d" -I"$d/libsashstore" -o "$d/bench" \
      "$d/bench_dramblast_path.c" \
      $(for f in $SRCS; do echo "$d/libsashstore/$f"; done)
  # shellcheck disable=SC2086
  gcc $CFLAGS -I"$d" -I"$d/libsashstore" -o "$d/test" \
      "$d/test_dramblast_find.c" \
      $(for f in $SRCS; do echo "$d/libsashstore/$f"; done)
}

# Reverts the upstream fix, to synthesise the defect the measurements were
# originally taken under. Note the direction: 01010101 -> 10101010.
SHIPPED='s/#define DRAMBLAST_SIMD_KEY_MASK 0b01010101/#define DRAMBLAST_SIMD_KEY_MASK 0b10101010/'

build_arm shipped "$SHIPPED"
build_arm maskfix ""

# Arms that also edit dramblast.c. None of them edits the header any more: the
# mask fix is in the tree, so each of these is the unmodified tree plus exactly
# one further change.
build_arm prefetchT1 ""
sed -i 's/#define PREFETCH_T1 1/#define PREFETCH_T1 2/' "$OUT/prefetchT1/libsashstore/dramblast.c"

build_arm prefetchT0 ""
sed -i 's/#define PREFETCH_T1 1/#define PREFETCH_T1 3/' "$OUT/prefetchT0/libsashstore/dramblast.c"

build_arm hoistloop ""
patch -s -p3 -d "$OUT/hoistloop/libsashstore" < "$L2/opt/dramblast-hoist.patch"

build_arm vecspill ""
# Inverted: the tree now reads the value from the table line, so this puts the
# __m512i subscript (and its 64-byte stack spill) back to measure its cost.
sed -i 's|result->v = bucket\[offset + 1\];|result->v = cacheline[(offset + 1)];|' \
    "$OUT/vecspill/libsashstore/dramblast.c"

# Each of the three above needs recompiling after its .c edit.
for arm in prefetchT1 prefetchT0 hoistloop vecspill; do
  d="$OUT/$arm"
  # shellcheck disable=SC2086
  gcc $CFLAGS -I"$d" -I"$d/libsashstore" -o "$d/bench" "$d/bench_dramblast_path.c" \
      $(for f in $SRCS; do echo "$d/libsashstore/$f"; done)
  # shellcheck disable=SC2086
  gcc $CFLAGS -I"$d" -I"$d/libsashstore" -o "$d/test" "$d/test_dramblast_find.c" \
      $(for f in $SRCS; do echo "$d/libsashstore/$f"; done)
done

# Pin to a housekeeping core: cores 0-23 belong to bench.slice and are reserved
# for the DPDK forwarder (docs/INVESTIGATION.md section 3.4c).
PIN="taskset -c 24"

echo "=== does the find path find what was just inserted? ==="
for arm in shipped maskfix prefetchT1 prefetchT0 hoistloop vecspill; do
  printf '%-16s ' "$arm"
  { $PIN "$OUT/$arm/test" 2>/dev/null || true; } | tr '\n' ' ' | sed 's/.*find hits *\([0-9]*\).*hits correct *\([0-9]*\).*\(PASS\|FAIL\).*/hits=\1 correct=\2 \3/'
  echo
done

echo
echo "=== lookup path cost ==="
# Arms are interleaved rather than run in blocks, so that any drift over the
# few minutes this takes lands on every arm equally instead of entirely on the
# last one. Same reasoning as docs/INVESTIGATION.md section 5.14.
REPEATS="${REPEATS:-6}"
RESULTS="$OUT/arms.tsv"
: > "$RESULTS"
for run in $(seq 1 "$REPEATS"); do
  for spec in "shipped:shipped:0" "maskfix:maskfix:0" "maskfix+alloc:maskfix:-1" \
              "prefetchT1:prefetchT1:0" "prefetchT0:prefetchT0:0" \
              "hoistloop:hoistloop:0" "hoistloop+alloc:hoistloop:-1" \
              "vecspill:vecspill:0"; do
    label="${spec%%:*}"; rest="${spec#*:}"; arm="${rest%%:*}"; pairs="${rest#*:}"
    t=$($PIN "$OUT/$arm/bench" "$pairs" | awk '/ticks/{print $3}')
    printf '%s\t%s\t%s\n' "$label" "$run" "$t" | tee -a "$RESULTS"
  done
done

python3 "$HERE/plot_dramblast_arms.py" "$RESULTS"
