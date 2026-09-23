# Hashtable functional tests

Functional correctness only. **Nothing here measures performance** — no timing,
no counters, no throughput. Performance claims live in
`docs/dramblast_analysis.md` and are analytical, not measured.

## Running

```
cd tests && make          # build and run
make check-c5             # demonstrate the inline-linkage defect (C5)
make clean
```

No DPDK, no hugepages and no NIC are required.

## How it builds

`l2fwd/libsashstore/dramblast.c` is compiled **unmodified**, together with
`conshash.c` and `hash.c`. Two things make that possible without DPDK:

- `shims/` provides minimal stand-ins for the only two DPDK headers
  `dramblast.c` includes (`rte_branch_prediction.h`, `rte_mbuf_core.h`).
  Nothing from `rte_mbuf_core.h` is actually referenced.
- The harness does **not** call `dramblast_init()`, which `mmap`s hugepages and
  would fail on a machine with none reserved. It builds a `dramblast_ht_t` over
  an `aligned_alloc`'d table instead.

Requires AVX-512F/DQ, SSE4.2 (for `_mm_crc32_u64`) and `cmpxchg16b`; the
Makefile passes `-march=native`, matching `l2fwd/meson.build`.

## What the tests establish

`T1` shows where keys land in a 64-byte bucket. `T2`–`T5` run the same four
scenarios twice: once through the shipped `dramblast_find_batch_sync`, once
through `find_batch_fixed` — a transcription of that kernel in this file with
the SIMD key mask corrected to `0b01010101`. `T6` builds a bucket by hand from
colliding keys to show the shipped mask returning a wrong value rather than
merely missing. `T7`–`T9` cover the result-id contract and two sentinel limits. `T11`:
`flowhash` (static inline in `packettool.h`, unrolled) vs transcription of old
loop version (HEAD 57bf217): all 16,777,216 pktgen tuples + 10M random frames,
0 mismatches required, 16,777,216 distinct keys required (REFLECT_PATH §7).

Every test is now a regression guard for a fixed defect in
`docs/dramblast_analysis.md`. Before the fixes the suite reported
`pass 18 / fail 0 / xfail 4`, the four XFAILs all being finding C1: 0 of 512
inserted keys were found.

## Status

```
pass 41   fail 0
```

Also clean under sanitizers, which finding C5 previously made impossible:

```
gcc -O1 -g -march=native -fsanitize=address,undefined \
    -I../l2fwd/libsashstore -Ishims -o /tmp/t test_dramblast.c \
    ../l2fwd/libsashstore/{dramblast,conshash,hash}.c && /tmp/t
```
