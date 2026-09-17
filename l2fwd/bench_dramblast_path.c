/*
 * The dramblast lookup path, per packet, with the find bug present and absent.
 *
 * `test_dramblast_find.c` shows that the shipped find path returns a hit for
 * none of the keys it was just given. This measures what that costs. The two
 * are different questions: the find path returning zero hits is a correctness
 * fact, but it also silently changes the workload, because
 * `dramblast_process_frames` reacts to a miss by consulting the backend LUT and
 * calling `dramblast_insert_one` (dramblast.c:282-291). A lookup that always
 * misses therefore performs an insert for every packet, and the insert writes
 * to the same table line the find just read -- turning a clean read into a
 * read-for-ownership and a writeback.
 *
 * This drives the real `dramblast_process_frames` in bursts of 64, as
 * `main.c:337` does, over a table sized to the same 3% occupancy as the
 * generator's 16.8M flows in 2^29 slots, and scaled down only so that it fits
 * without reserving 1 GiB hugepages. The table is still several times L3
 * (52.5 MiB on this part), so the access is still a DRAM access and the
 * comparison is still between a read and a read-modify-write of a DRAM line.
 *
 * Build one binary per arm; the arm is a compile-time constant in the header,
 * so it cannot be switched at run time. See check_dramblast_arms.sh.
 */
#include "libsashstore/dramblast.h"
#include <nmmintrin.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <x86intrin.h>

uint64_t CAPACITY = 1ULL << 24; /* 256 MiB of table, ~5x this part's L3 */

#define NFLOWS (503316) /* 3% of 2^24, matching the rig's occupancy */
#define BURST 64
#define BURSTS 200000

extern dramblast_ht_t *dramblast_ht;
int dramblast_insert_one(dramblast_ht_t *ht, uint64_t k, uint64_t v);

static uint64_t keys[NFLOWS];

/* splitmix64: flow keys that look like hash output, which is what
   flowhash() hands dramblast. */
static uint64_t sm64(uint64_t *s) {
  uint64_t z = (*s += 0x9E3779B97F4A7C15ULL);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
  return z ^ (z >> 31);
}

int main(int argc, char **argv) {
  if (argc > 1)
    dramblast_alloc_pairs = atoi(argv[1]);

  dramblast_init();

  uint64_t s = 12345;
  for (int i = 0; i < NFLOWS; i++) {
    keys[i] = sm64(&s) | 1; /* never 0: insert rejects key 0 */
    if (dramblast_insert_one(dramblast_ht, keys[i], 0xff) < 0) {
      printf("FAIL: insert %d rejected\n", i);
      return 1;
    }
  }

  dramblast_arg_t args[BURST];
  uint64_t ret[BURST];
  uint64_t r = 999;

  /* Warm: the same traffic pattern, untimed, so the timed region measures
     steady state rather than the first touch of every line. */
  for (int b = 0; b < 2000; b++) {
    for (int j = 0; j < BURST; j++) {
      args[j].k = keys[sm64(&r) % NFLOWS];
      args[j].id = (uint32_t)j;
    }
    dramblast_process_frames(args, BURST, ret, 0);
  }

  uint64_t t0 = __rdtsc();
  for (int b = 0; b < BURSTS; b++) {
    for (int j = 0; j < BURST; j++) {
      args[j].k = keys[sm64(&r) % NFLOWS];
      args[j].id = (uint32_t)j;
    }
    dramblast_process_frames(args, BURST, ret, 0);
  }
  uint64_t t = __rdtsc() - t0;

  /* Every returned MAC must be the backend value; a zero would mean the path
     failed rather than merely took the slow route. */
  for (int j = 0; j < BURST; j++)
    if (ret[j] == 0) {
      printf("FAIL: zero MAC at %d\n", j);
      return 1;
    }

  printf("mask=0x%02x alloc_pairs=%d  %7.2f TSC ticks/packet  (%d bursts x %d)\n",
         DRAMBLAST_SIMD_KEY_MASK, dramblast_alloc_pairs,
         (double)t / ((double)BURSTS * BURST), BURSTS, BURST);
  return 0;
}
