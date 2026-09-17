#!/usr/bin/env bash
# Build and run the dramblast lookup-path microbenchmark in several arms.
#
# The arms differ only in one-line edits to a copy of the real source, so each
# arm is the shipped code plus exactly one change. The copies live under a
# scratch directory; nothing here modifies the tree.
#
#   shipped        DRAMBLAST_SIMD_KEY_MASK 0b10101010, one alloc/free per burst
#   maskfix        mask 0b01010101 (compare against the key lanes, not the
#                  value lanes), everything else as shipped
#   maskfix+hoist  as maskfix, plus -A -1's per-lcore result buffer
#   +prefetchT1    as maskfix, with the prefetch hint constants corrected. The
#                  table in dramblast.c is the reverse of the ISA encoding
#                  (_MM_HINT_T0 is 3, not 0), so the shipped `PREFETCH_T1`
#                  emits `prefetcht2`; this arm emits the `prefetcht1` the
#                  source says it wants.
#   +prefetchT0    same, but the `prefetcht0` that fills L1 as well
#   +hoist         as maskfix, plus opt/dramblast-hoist.patch: the queue
#                  pointer, table pointer, table mask and the queue head and
#                  tail read once into locals instead of being re-derived
#                  through `ht` on every iteration
#   +novecspill    as maskfix, reading the hit value from the table line
#                  instead of subscripting the __m512i by a variable, which
#                  GCC implements by spilling all 64 bytes to the stack and
#                  reloading 8 of them (a store-forwarding stall on every hit)
#
# Usage: ./check_dramblast_arms.sh [scratch-dir]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-${TMPDIR:-/tmp}/dramblast_arms}"
DPDK_INC="$(sed -n 's/.*-I\(\/nix\/store\/[^ ]*dpdk[^ ]*\/include\).*/\1/p' \
             "$HERE/build/compile_commands.json" | head -1)"
CFLAGS="-O3 -g -mavx512f -mavx512dq -march=native -msse4.2
        -DALLOW_EXPERIMENTAL_API -D_GNU_SOURCE -include rte_config.h
        -I$DPDK_INC"
SRCS="dramblast.c backing.c conshash.c hash.c packettool.c"

build_arm() {                      # $1 arm name, $2 sed program for the header
  local arm="${1}" edit="${2:-}" d="$OUT/${1}"
  rm -rf "$d"; mkdir -p "$d"
  cp -r "$HERE/libsashstore" "$d/"
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

MASKFIX='s/#define DRAMBLAST_SIMD_KEY_MASK 0b10101010/#define DRAMBLAST_SIMD_KEY_MASK 0b01010101/'

build_arm shipped ""
build_arm maskfix "$MASKFIX"

# Arms that also edit dramblast.c. The header edit is the same mask fix in
# every case, so each of these is maskfix plus exactly one further change.
build_arm prefetchT1 "$MASKFIX"
sed -i 's/#define PREFETCH_T1 1/#define PREFETCH_T1 2/' "$OUT/prefetchT1/libsashstore/dramblast.c"

build_arm prefetchT0 "$MASKFIX"
sed -i 's/#define PREFETCH_T1 1/#define PREFETCH_T1 3/' "$OUT/prefetchT0/libsashstore/dramblast.c"

build_arm hoistloop "$MASKFIX"
patch -s -p3 -d "$OUT/hoistloop/libsashstore" < "$HERE/opt/dramblast-hoist.patch"

build_arm novecspill "$MASKFIX"
sed -i 's|result->v = cacheline\[(offset + 1)\];|result->v = ((const uint64_t *)bucket)[offset + 1];|' \
    "$OUT/novecspill/libsashstore/dramblast.c"

# Each of the three above needs recompiling after its .c edit.
for arm in prefetchT1 prefetchT0 hoistloop novecspill; do
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
for arm in shipped maskfix prefetchT1 prefetchT0 hoistloop novecspill; do
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
              "novecspill:novecspill:0"; do
    label="${spec%%:*}"; rest="${spec#*:}"; arm="${rest%%:*}"; pairs="${rest#*:}"
    t=$($PIN "$OUT/$arm/bench" "$pairs" | awk '/ticks/{print $3}')
    printf '%s\t%s\t%s\n' "$label" "$run" "$t" | tee -a "$RESULTS"
  done
done

python3 "$HERE/plot_dramblast_arms.py" "$RESULTS"
