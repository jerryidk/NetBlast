/*
 * Does dramblast's find path actually find anything?
 *
 * Every per-packet number in docs/INVESTIGATION.md is a cost measured on the
 * shipped dramblast forwarding path, but nothing in the investigation ever
 * checked the path's *result*: `dramblast_process_frames` writes a destination
 * MAC either from the table or from the backend LUT, and both are 0xff for
 * every backend (`conshash.c`, populate_lut fills the whole LUT with 0xff and
 * returns before the real Maglev code), so a lookup that always misses and a
 * lookup that always hits produce byte-identical forwarded packets. The
 * forwarder cannot tell them apart and neither can the generator.
 *
 * This harness asks the question the forwarder cannot: insert a set of keys,
 * look the same keys up, and count how many the find path returns a value for.
 * It links the real libsashstore, so it is testing the shipped code rather
 * than a copy of it.
 */
#include "libsashstore/dramblast.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <nmmintrin.h>

/* main.c owns this; the harness supplies its own so the table can be small
   enough to allocate without reserving 1 GiB hugepages. */
uint64_t CAPACITY = 1ULL << 16;

/* Neither is declared in dramblast.h, but both are external in dramblast.c. */
extern dramblast_ht_t *dramblast_ht;
int dramblast_insert_one(dramblast_ht_t *ht, uint64_t k, uint64_t v);
uint32_t dramblast_find_batch_sync(dramblast_ht_t *ht, dramblast_arg_t *args,
                                   unsigned int args_len,
                                   dramblast_result_t *results,
                                   unsigned int id);

#define N 64

int main(void) {
  dramblast_init();

  /* Keys shaped like the ones the forwarder uses: flowhash() output is a
     64-bit FNV hash, so the low and high halves are both populated. Values are
     distinct and non-zero so a hit is distinguishable from a miss AND from a
     hit on the wrong slot. */
  uint64_t keys[N], vals[N];
  for (int i = 0; i < N; i++) {
    keys[i] = 0x9E3779B97F4A7C15ULL * (uint64_t)(i + 1);
    vals[i] = 0x1000 + i;
    if (dramblast_insert_one(dramblast_ht, keys[i], vals[i]) < 0) {
      printf("FAIL: insert %d rejected\n", i);
      return 1;
    }
  }

  /* Read the table back by hand, without the SIMD path, to establish that the
     inserts are really there. If this disagrees with the find path, the find
     path is what is wrong. */
  int present = 0;
  for (int i = 0; i < N; i++) {
    for (uint64_t probe = 0; probe < CAPACITY; probe++) {
      uint64_t idx = (_mm_crc32_u64(0, keys[i]) & (CAPACITY - 1) & ~0x3ULL);
      idx = (idx + probe) & (CAPACITY - 1);
      if (dramblast_ht->table[idx].k == keys[i]) {
        if (dramblast_ht->table[idx].v == vals[i])
          present++;
        break;
      }
    }
  }

  dramblast_arg_t args[N];
  dramblast_result_t results[N];
  for (int i = 0; i < N; i++) {
    args[i].k = keys[i];
    args[i].id = (uint32_t)i;
  }
  memset(results, 0xAB, sizeof(results));

  uint32_t n = dramblast_find_batch_sync(dramblast_ht, args, N, results, 0);

  int hits = 0, correct = 0;
  for (uint32_t i = 0; i < n; i++) {
    if (results[i].v != 0) {
      hits++;
      if (results[i].id < N && results[i].v == vals[results[i].id])
        correct++;
    }
  }

  printf("inserted      %d\n", N);
  printf("present (scalar readback)  %d\n", present);
  printf("find returned %u results\n", n);
  printf("find hits     %d\n", hits);
  printf("hits correct  %d\n", correct);

  if (present == N && hits == N && correct == N) {
    printf("PASS\n");
    return 0;
  }
  printf("FAIL\n");
  return 1;
}
