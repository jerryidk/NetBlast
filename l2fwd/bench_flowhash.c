/*
 * What does the flow hash cost, and what would a different one cost?
 *
 * Every packet through either engine is hashed by `flowhash()`
 * (packettool.c:110), which calls `fnv_1_multi()` three times over 8, 1 and 4
 * bytes. `fnv_1_multi` (hash.c:12) is a byte-at-a-time loop whose state is
 * carried through an `imul`, so the thirteen bytes form a serial dependency
 * chain of thirteen 3-cycle multiplies. That is a latency cost, not a
 * throughput one, and it is paid before the table is ever touched -- so it sits
 * inside every per-packet number in docs/INVESTIGATION.md without ever having
 * been separated from the lookup it precedes.
 *
 * The comparison arm is not a suggestion plucked from the air: dramblast
 * already computes `_mm_crc32_u64` on the result of this hash
 * (dramblast.c:78), so the instruction is known to be available and already on
 * the per-packet path. The question is only whether the FNV pass in front of it
 * is buying anything.
 *
 * Measured two ways, because the two answer different questions and the first
 * version of this file only asked the wrong one.
 *
 *   throughput  successive hashes independent, as in the forwarder's loop
 *               (main.c:364-375 hashes every packet of a burst with no
 *               dependency between them, so the out-of-order engine overlaps
 *               them and the imul chains of different packets interleave)
 *   latency     each hash's input made to depend on the previous hash's output
 *
 * The forwarder's regime is the throughput one. The latency number is reported
 * only to show how far apart they are, because it is the number a naive
 * microbenchmark produces and it is roughly twice the honest one.
 */
#include "libsashstore/packettool.h"
#include <nmmintrin.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <x86intrin.h>

#define NFRAMES 4096
#define FRAME_LEN 110
#define ITERS 2000

static uint8_t frames[NFRAMES][FRAME_LEN] __attribute__((aligned(64)));

/* The same thirteen bytes flowhash() feeds to FNV -- src/dst IP, protocol,
   src/dst port -- run through CRC32C instead. Two crc32 on 8 and 4 bytes plus
   one on the protocol byte: three instructions of 3-cycle latency against
   thirteen. */
static inline uint64_t flowhash_crc(void *frame) {
  char *f = (char *)frame;
  if (f[14] >> 4 != 4)
    return 0;
  char proto = f[14 + 9];
  if (proto != 6 && proto != 17)
    return 0;
  size_t v4len = 4 * (f[14] & 0b1111);

  uint64_t h = _mm_crc32_u64(0, *(uint64_t *)(f + 14 + 12)); /* src+dst IP */
  h = _mm_crc32_u32((uint32_t)h, *(uint32_t *)(f + 14 + v4len)); /* ports */
  h = _mm_crc32_u8((uint32_t)h, (uint8_t)proto);
  /* flowhash() returns 0 for "unparseable", and the forwarder tests for it
     (main.c:371), so a real hash must never collide with it. */
  return h | 0x100000000ULL;
}

static void build_frames(void) {
  for (int i = 0; i < NFRAMES; i++) {
    uint8_t *f = frames[i];
    memset(f, 0, FRAME_LEN);
    f[14] = 0x45;          /* IPv4, IHL 5 */
    f[14 + 9] = 17;        /* UDP */
    /* src/dst IP and ports vary per frame, as the generator's flows do */
    uint32_t sip = 0x0a000001u + (uint32_t)i;
    uint32_t dip = 0x0b000001u + (uint32_t)(i * 7);
    memcpy(f + 14 + 12, &sip, 4);
    memcpy(f + 14 + 16, &dip, 4);
    uint16_t sp = (uint16_t)(1024 + i), dp = (uint16_t)(2048 + i * 3);
    memcpy(f + 14 + 20, &sp, 2);
    memcpy(f + 14 + 22, &dp, 2);
  }
}

int main(void) {
  build_frames();

  /* Warm the frames into L1/L2 so this measures the hash, not the frames. */
  uint64_t warm = 0;
  for (int i = 0; i < NFRAMES; i++)
    warm += flowhash(frames[i]);

  uint64_t acc = warm, t0;
  double n = (double)NFRAMES * ITERS;

  /* Throughput: independent hashes, the forwarder's regime. */
  t0 = __rdtsc();
  for (int it = 0; it < ITERS; it++)
    for (int i = 0; i < NFRAMES; i++)
      acc += flowhash(frames[i]);
  double thr_fnv = (__rdtsc() - t0) / n;

  t0 = __rdtsc();
  for (int it = 0; it < ITERS; it++)
    for (int i = 0; i < NFRAMES; i++)
      acc += flowhash_crc(frames[i]);
  double thr_crc = (__rdtsc() - t0) / n;

  /* Latency: each input depends on the previous output. */
  uint64_t a = warm;
  t0 = __rdtsc();
  for (int it = 0; it < ITERS; it++)
    for (int i = 0; i < NFRAMES; i++)
      a = flowhash(frames[(i ^ (a & 1)) & (NFRAMES - 1)]) + a;
  double lat_fnv = (__rdtsc() - t0) / n;

  uint64_t b = warm;
  t0 = __rdtsc();
  for (int it = 0; it < ITERS; it++)
    for (int i = 0; i < NFRAMES; i++)
      b = flowhash_crc(frames[(i ^ (b & 1)) & (NFRAMES - 1)]) + b;
  double lat_crc = (__rdtsc() - t0) / n;

  printf("                     throughput   latency   (TSC ticks/packet)\n");
  printf("flowhash FNV shipped  %8.2f  %8.2f\n", thr_fnv, lat_fnv);
  printf("flowhash CRC32C       %8.2f  %8.2f\n", thr_crc, lat_crc);
  printf("saving                %8.2f  %8.2f\n", thr_fnv - thr_crc,
         lat_fnv - lat_crc);
  printf("(checksums %016lx %016lx %016lx -- must be non-zero)\n", acc, a, b);
  return 0;
}
