
#include "dramblast.h"
#include "backing.h"
#include "conshash.h"
#include "nbprobe.h"
#include "packettool.h"
#include "rte_branch_prediction.h"
#include "rte_mbuf_core.h"
#include <immintrin.h>
#include <linux/limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <xmmintrin.h> // Header for _mm_prefetch
#define SSE42
#ifdef SSE42
#include <nmmintrin.h> // Header for CRC32 intrinsics
#endif

// lookup backend server
#define MAX_CPU 128
extern uint64_t CAPACITY;
static LookUpTable dramblast_backends;

dramblast_ht_t *dramblast_ht;

static inline uint32_t dramblast_get_queue_sz(dramblast_ht_t *ht, unsigned int id) {

  dramblast_queue_t *q = &ht->queues[id];
  return (q->find_queue_head - q->find_queue_tail) & (q->find_queue_size - 1);
}

static inline void dramblast_push_queue(dramblast_ht_t *ht, uint64_t idx, uint64_t k,
                                uint64_t visit_count,
                                 uint32_t item_id, unsigned int tid) {

  dramblast_queue_t *q = &ht->queues[tid];
  dramblast_queue_item_t *queue_head_slot = &q->find_queue[q->find_queue_head];
  queue_head_slot->idx = idx;
  queue_head_slot->k = k;
  queue_head_slot->id = item_id;
  queue_head_slot->visit_count = visit_count;
  q->find_queue_head++;
  q->find_queue_head = q->find_queue_head & (q->find_queue_size - 1);
}

static inline dramblast_queue_item_t *dramblast_pop_queue(dramblast_ht_t *ht,
                                                   unsigned int id) {

  dramblast_queue_t *q = &ht->queues[id];
  dramblast_queue_item_t *queue_tail_slot = &q->find_queue[q->find_queue_tail];
  q->find_queue_tail++;
  q->find_queue_tail = q->find_queue_tail & (q->find_queue_size - 1);

  return queue_tail_slot;
}

/* C11 requires aligned_alloc()'s size to be an integral multiple of the
 * alignment, so round up rather than passing a bare sizeof(). */
static inline void *dramblast_alloc64(size_t bytes) {
  return aligned_alloc(64, (bytes + 63) & ~(size_t)63);
}

// Define hint levels based on Intel/GCC standards
#define PREFETCH_T0 0 // Temporal: Load into all levels of cache (L1/L2/L3)
#define PREFETCH_T1 1 // Temporal: Load into L2/L3
#define PREFETCH_T2 2 // Temporal: Load into L3
#define PREFETCH_NTA                                                           \
  3 // Non-Temporal: Minimize cache pollution (e.g., streaming)

// Macro to encode the instruction
#define LX_PREFETCH(addr, level) _mm_prefetch((const char *)(addr), (level))

// Updated dramblast_prefetch function
static inline void dramblast_prefetch(dramblast_ht_t *ht, uint64_t idx) {
  // Using PREFETCH_T0 is standard for items you are about to access immediately
  LX_PREFETCH(&ht->table[idx], PREFETCH_T1);
}

static inline uint64_t dramblast_hash(dramblast_ht_t *ht, uint64_t k) {
  uint64_t hash;
#ifdef SSE42
  hash = _mm_crc32_u64(0, k);
#else
  // 64bit golden prime
  hash = k * 0x9E3779B97F4A7C15ULL;
#endif

  return (uint64_t)hash & (ht->len - 1) & DRAMBLAST_BUCKET_IDX_MASK;
}

int dramblast_insert_one(dramblast_ht_t *ht, uint64_t k, uint64_t v) {

  uint64_t idx = dramblast_hash(ht, k);
  dramblast_kv_t *kv;

  uint64_t count = 0;

  if (k == 0) {
    return -1;
  }

try_insert:
  kv = &ht->table[idx];
  count++;
  if (kv->k == 0)
  {
      dramblast_swap_kv_t swapped;
      swapped.pair.k = k;
      swapped.pair.v = v;
      if (__sync_bool_compare_and_swap((__int128 *)kv, (__int128)0, *(__int128 *)&swapped)) {
          NBP_ADD(ins_steps, count);
          return 0;
      }
  }

  if(kv->k == k) {
    kv->v = v;
    NBP_ADD(ins_steps, count);
    return 0;
  }

  idx++;
  idx = idx & (ht->len - 1);
  if (!(idx & 0x3)) {
    dramblast_prefetch(ht, idx);
  }

  if (count >= ht->len)
    return -1;

  goto try_insert;
}

// Note: args_len is also length of results array
uint32_t dramblast_find_batch_sync(dramblast_ht_t *ht, dramblast_arg_t *args,
                                   unsigned int args_len,
                                   dramblast_result_t *results,
                                   unsigned int id) {

  unsigned int args_head = 0;
  unsigned int result_head = 0;
#ifdef NB_PROBE
  /* locals, not nbp->cur: one store per burst instead of per pop */
  uint32_t nbp_pops = 0, nbp_reprobes = 0, nbp_occ_sum = 0, nbp_occ_max = 0;
#endif

  uint64_t idx, count;
  while (result_head < args_len) {

    // push as many as possible without stalling on LFB.
    while (args_head < args_len &&
           dramblast_get_queue_sz(ht, id) < ht->queues[id].find_queue_size - 1) {
      dramblast_arg_t *arg = &args[args_head];
      args_head++;
      idx = dramblast_hash(ht, arg->k);
      dramblast_prefetch(ht, idx);
      dramblast_push_queue(ht, idx, arg->k, 0, arg->id, id);
      NBW(NBW_PUSH, arg->id, idx);
    }

#ifdef NB_PROBE
    {
      uint32_t occ = dramblast_get_queue_sz(ht, id); /* in flight at this pop */
      nbp_pops++;
      nbp_occ_sum += occ;
      if (occ > nbp_occ_max) nbp_occ_max = occ;
    }
#endif
    // pop_find_queue
    dramblast_queue_item_t *queue_tail_slot = dramblast_pop_queue(ht, id);
    idx = queue_tail_slot->idx;
    count = queue_tail_slot->visit_count;
    uint64_t *bucket = (uint64_t *)&ht->table[idx];
    __m512i cacheline = _mm512_load_si512(bucket);
    __m512i key_vector = _mm512_set1_epi64(queue_tail_slot->k);
    __m512i zero_vector = _mm512_setzero_si512();
    __mmask8 key_cmp = _mm512_mask_cmpeq_epu64_mask(DRAMBLAST_SIMD_KEY_MASK,
                                                    cacheline, key_vector);
    if (key_cmp > 0) {
      /* key lanes are even, so the value is the next lane (cas_kht.hpp:611) */
      int offset = __builtin_ctz(key_cmp);
      dramblast_result_t *result = &results[result_head];
      result->v = bucket[offset + 1];
      result->id = queue_tail_slot->id;
      result->status = DRAMBLAST_FOUND;
      result_head++;
      NBW(NBW_FOUND, queue_tail_slot->id, count);
    } else {

      count += 4;
      if (unlikely(count >= ht->len)) {
        // probe bound reached; we cannot say whether the key is present
        dramblast_result_t *result = &results[result_head];
        result->v = 0;
        result->id = queue_tail_slot->id;
        result->status = DRAMBLAST_TABLE_FULL;
        result_head++;
        NBW(NBW_FULL, queue_tail_slot->id, count);
        continue;
      }

      __mmask8 ept_cmp = _mm512_mask_cmpeq_epu64_mask(DRAMBLAST_SIMD_KEY_MASK,
                                                      cacheline, zero_vector);
      if (ept_cmp == 0) {
        idx += 4;
        idx = idx & (ht->len - 1);
        idx = idx & DRAMBLAST_BUCKET_IDX_MASK;
        dramblast_prefetch(ht, idx);
        dramblast_push_queue(ht, idx, queue_tail_slot->k, count, queue_tail_slot->id,
                             id);
        NBW(NBW_REPROBE, queue_tail_slot->id, count);
#ifdef NB_PROBE
        nbp_reprobes++;
#endif
      } else {
        // an empty slot in this bucket proves the key is absent
        dramblast_result_t *result = &results[result_head];
        result->v = 0;
        result->id = queue_tail_slot->id;
        result->status = DRAMBLAST_ABSENT;
        result_head++;
        NBW(NBW_ABSENT, queue_tail_slot->id, count);
      }
    }
  }

  NBP_SET(pops, nbp_pops);
  NBP_SET(reprobes, nbp_reprobes);
  NBP_SET(occ_sum, nbp_occ_sum);
  NBP_SET(occ_max, nbp_occ_max);
  return result_head;
}

// return number of frames modified.
/*
 * How many aligned_alloc/free round trips each burst pays.
 *
 *   -1  hoisted: none at all, a per-lcore buffer allocated once at init
 *    0  as shipped: exactly one pair, the aligned_alloc/free below
 *    n  as shipped plus n extra pairs, to calibrate what a pair costs here
 *
 * This exists because the per-burst cost C is measurable (~645 cycles/burst for
 * dramblast, against ~0 for maglev) but its composition is not. A hot-tcache
 * pair is usually quoted at 20-40 ns, which would be at most ~11% of C -- but
 * that is a literature number, and the whole point of the pinned rig is to stop
 * relying on those. Sweeping n turns C into a line: the slope is what a pair
 * actually costs on this machine, and the intercept at n = -1 is whatever the
 * per-burst cost is that has nothing to do with the allocator.
 */
int dramblast_alloc_pairs = 0;

/*
 * Depth of the software prefetch pipeline, DRAMBLAST_FIND_QUEUE_SIZE as shipped.
 *
 * This is the second of the two candidates for the per-burst cost C. The find
 * loop issues a prefetch and queues an item, then pops and processes one, so a
 * deep queue keeps many cache lines in flight and hides DRAM latency -- but a
 * burst of B packets can only ever fill min(B, depth) slots, so a short burst
 * runs a pipeline that never reaches steady state. If that ramp is what C is
 * made of, C must fall when the depth is cut (there is less pipeline to fill)
 * while P rises (less latency hidden in steady state). If C is the allocator
 * instead, depth changes nothing about C. The two knobs therefore separate the
 * two hypotheses without either being able to mimic the other.
 *
 * Must stay a power of two: the head and tail wrap with & (size - 1).
 */
int dramblast_queue_depth = DRAMBLAST_FIND_QUEUE_SIZE;
static dramblast_result_t *dramblast_hoisted[MAX_CPU];

void dramblast_process_frames(dramblast_arg_t *args, unsigned int args_len,
                              uint64_t *ret, unsigned int id) {
  dramblast_result_t *results;

  if (dramblast_alloc_pairs < 0) {
    results = dramblast_hoisted[id];
  } else {
    results = dramblast_alloc64(sizeof(dramblast_result_t) * args_len);
    /* Extra pairs are the same size and the same call as the real one, so they
       exercise the identical tcache path rather than a cheaper one. Touch each
       one so the compiler cannot discard the round trip. */
    for (int e = 0; e < dramblast_alloc_pairs; e++) {
      dramblast_result_t *scratch =
          dramblast_alloc64(sizeof(dramblast_result_t) * args_len);
      if (scratch == NULL) {
        printf("dramblast: amplification alloc failed, measurement invalid\n");
        exit(-1);
      }
      /* The obvious guard here -- a store into scratch -- does not work, and
         silently did not work: GCC 11 deletes a store to an object that is
         about to be freed, and disassembly of the shipped binary showed the
         store gone while the alloc/free pair survived only because the
         compiler happened to be conservative. -fallocation-dce (on by default
         at -O2 since GCC 11) is one toolchain bump from removing the pair
         outright and reporting that an allocator round trip costs nothing --
         which is exactly the wrong answer this arm exists to rule out, with no
         symptom. Making the pointer escape into an opaque asm prevents the
         allocation from being elided at all. No "memory" clobber: that would
         force spills around the loop and change the cost being measured. */
      __asm__ volatile("" :: "r"(scratch));
      free(scratch);
    }
  }

  NBP_MARK(NBP_B_ALLOC);
  NBW(NBW_PHASE, 0, NBP_B_ALLOC);
  unsigned int len =
      dramblast_find_batch_sync(dramblast_ht, args, args_len, results, id);
  NBP_MARK(NBP_B_FIND);
  NBW(NBW_PHASE, 0, NBP_B_FIND);

  if (len != args_len) {
    printf("dramblast sync is not correct ");
    exit(-1);
  }

#ifdef NB_PROBE
  /* locals: NBP_ADD per packet was a store->load chain through one TLS word,
     ~5 cyc/pkt inside `post` -- the probe timing itself. One store per burst. */
  uint32_t nbp_found = 0, nbp_absent = 0, nbp_full = 0;
#endif
  for (unsigned int i = 0; i < len; i++) {
    int64_t backend_mac_addr;
    dramblast_result_t *result = &results[i];

    if (result->status == DRAMBLAST_ABSENT) {
#ifdef NB_PROBE
      nbp_absent++;
#endif
      uint64_t client_hash = args[result->id].k;
      backend_mac_addr = dramblast_backends[client_hash % TABLE_SIZE];
      if (dramblast_insert_one(dramblast_ht, client_hash, backend_mac_addr) < 0)
        backend_mac_addr = 0; // insertion failed
    } else if (result->status == DRAMBLAST_TABLE_FULL) {
#ifdef NB_PROBE
      nbp_full++;
#endif
      /* Presence is unknown, so re-probing the whole table on the insert path
       * would only repeat the scan that just gave up. Report no mapping. */
      backend_mac_addr = 0;
    } else {
#ifdef NB_PROBE
      nbp_found++;
#endif
      backend_mac_addr = result->v;
    }

    ret[result->id] = backend_mac_addr;
  }

  NBP_SET(found, nbp_found);
  NBP_SET(absent, nbp_absent);
  NBP_SET(inserts, nbp_absent); /* every ABSENT inserts */
  NBP_SET(full, nbp_full);
  NBP_MARK(NBP_B_POST);
  NBW(NBW_PHASE, 0, NBP_B_POST);
  if (dramblast_alloc_pairs >= 0)
    free(results);
  NBP_MARK(NBP_B_FREE);
  NBW(NBW_PHASE, 0, NBP_B_FREE);
}

/* The page-size choice and the round-up that has to agree with it both moved
   into backing.c, so that maglev can reach the same code. Only the threshold
   stays here, because it is dramblast's own as-shipped policy. */
#define PAGE_SIZE_1GB (1024ULL * 1024 * 1024)

void *allocate_dramblast_table(size_t bytes) {
  /* As shipped this was an unconditional MAP_HUGETLB|MAP_HUGE_1GB mmap (2 MiB
     below 1 GiB). It now goes through backing_alloc so -B can put dramblast on
     maglev's page size and vice versa; with no -B the behaviour is unchanged. */
  return backing_alloc(bytes, bytes > PAGE_SIZE_1GB ? BACKING_1G : BACKING_THP2M);
}

void dramblast_init(void) {
  dramblast_ht = dramblast_alloc64(sizeof(dramblast_ht_t));

  if (!dramblast_ht) {
    printf("Aligned alloc failed!\n");
    exit(1);
  }

  dramblast_ht->queues = (dramblast_queue_t *)dramblast_alloc64(
      sizeof(dramblast_queue_t) * MAX_CPU); // 128 max cpu
  for (uint8_t i = 0; i < MAX_CPU; i++) {
    dramblast_queue_t *q = &dramblast_ht->queues[i];
    q->find_queue_head = 0;
    q->find_queue_tail = 0;
    q->find_queue_size = dramblast_queue_depth;
    q->find_queue =
        dramblast_alloc64(sizeof(dramblast_queue_item_t) * q->find_queue_size);
    /* MAX_PKT_BURST in main.c is 64 and args_len can never exceed it, so one
       burst-sized buffer per lcore is enough for the hoisted arm. Allocated
       unconditionally: it costs 1 KiB per lcore and keeps the two arms'
       initialisation identical. */
    dramblast_hoisted[i] =
        dramblast_alloc64(sizeof(dramblast_result_t) * DRAMBLAST_MAX_BURST);
    if (dramblast_hoisted[i] == NULL) {
      printf("Aligned alloc failed!\n");
      exit(1);
    }
  }

  dramblast_ht->len = CAPACITY;
  // using hugepages 2mb or 1gb for hsahtbale base on table capacity.
  uint64_t bytes = dramblast_ht->len * sizeof(dramblast_kv_t);
  dramblast_ht->table = allocate_dramblast_table(bytes);

  if (!dramblast_ht->table) {
    printf("dramblast->table alloc failed!\n");
    exit(1);
  }

  populate_lut(dramblast_backends);

  printf("dramblast initialized!\n");
}

void dramblast_destroy() {
  if (dramblast_ht == NULL) {
    return;
  }

  /* 1. Unmap the hugepage table allocated via mmap */
  if (dramblast_ht->table != NULL) {
    uint64_t bytes = (uint64_t)dramblast_ht->len * sizeof(dramblast_kv_t);

    // FIX: Calculate the exact aligned size that was mapped
    size_t aligned_bytes =
        backing_alloc_size(bytes, bytes > PAGE_SIZE_1GB ? BACKING_1G : BACKING_THP2M);

    if (munmap(dramblast_ht->table, aligned_bytes) != 0) {
      perror("munmap failed during dramblast_destroy");
    }
    dramblast_ht->table = NULL;
  }

  /* 2. Free the find queue buffer allocated via aligned_alloc */
  if (dramblast_ht->queues != NULL) {
    for (uint8_t i = 0; i < MAX_CPU; i++) {
      dramblast_queue_t *q = &dramblast_ht->queues[i];
      if (q->find_queue != NULL) {
        free(q->find_queue);
        q->find_queue = NULL;
      }
    }

    free(dramblast_ht->queues);
    dramblast_ht->queues = NULL;
  }

  /* 3. Free the root state structure */
  free(dramblast_ht);
  dramblast_ht = NULL;

  printf("dramblast destroyed and memory cleaned up successfully.\n");
}

/*
 * -P <alpha>: prefill table to load factor alpha with filler keys before
 * traffic starts.
 *
 * Why a filler, not TOTAL_FLOWS: generator flow count moves load factor AND
 * touched working set together (16M flows = 1 GiB of lines touched, past L3).
 * Filler holds traffic fixed at 16M flows and moves only load factor: probe
 * length, empty-slot odds, insert walk. Occupancy = load factor here, by the
 * user's definition.
 *
 * Filler keys: xorshift64* per part, fixed seeds, 0 skipped (0 = empty slot).
 * Collision with a traffic key needs 64-bit equality: ~16M x 5e8 / 2^64, nil.
 * Value 0xff, same as dramblast_backends, so a filler hit is indistinguishable
 * from a traffic hit anyway.
 *
 * Runs on every lcore via rte_eal_mp_remote_launch (main.c) before forwarding:
 * 0.9 x 2^29 serial inserts at ~100 ns DRAM each would be ~48 s on one core.
 * Batches of 16 prefetch the home bucket first so each lcore keeps 16 misses
 * in flight. insert_one is CAS-safe, so parts may race on a slot.
 */
double dramblast_prefill_alpha = 0;
static uint64_t dramblast_prefill_failed;

void dramblast_prefill_part(unsigned part, unsigned nparts) {
  dramblast_ht_t *ht = dramblast_ht;
  uint64_t target = (uint64_t)(dramblast_prefill_alpha * (double)ht->len);
  uint64_t mine = target / nparts + (part == 0 ? target % nparts : 0);
  uint64_t x = 0x9E3779B97F4A7C15ULL * (part + 1), keys[16];
  uint64_t failed = 0;

  for (uint64_t done = 0; done < mine;) {
    unsigned b = 0;
    for (; b < 16 && done + b < mine; b++) {
      do {
        x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
        keys[b] = x * 0x2545F4914F6CDD1DULL;
      } while (keys[b] == 0);
      dramblast_prefetch(ht, dramblast_hash(ht, keys[b]));
    }
    for (unsigned i = 0; i < b; i++)
      if (dramblast_insert_one(ht, keys[i], 0xff) < 0) failed++;
    done += b;
  }
  __sync_fetch_and_add(&dramblast_prefill_failed, failed);
}

uint64_t dramblast_prefill_failures(void) { return dramblast_prefill_failed; }

/*
 * Table state, measured not assumed. One pass over every slot.
 *
 *   alpha         occupied slots / slots
 *   disp          buckets between key's home bucket and its slot. A hit on
 *                 that key loads disp+1 buckets. Histogram bins
 *                 0,1,2,3,4-7,8-15,16-63,64+.
 *   miss_buckets  buckets loaded before an empty slot proves absence, from
 *                 2^20 random home buckets: cost of every ABSENT (new flow).
 *
 * Printed after prefill and at exit (exit includes traffic's inserts).
 */
void dramblast_table_stats(const char *when) {
  dramblast_ht_t *ht = dramblast_ht;
  uint64_t len = ht->len, occ = 0, dsum = 0, hist[8] = {0};
  for (uint64_t i = 0; i < len; i++) {
    uint64_t k = ht->table[i].k;
    if (!k) continue;
    occ++;
    uint64_t d = (((i & DRAMBLAST_BUCKET_IDX_MASK) - dramblast_hash(ht, k)) & (len - 1)) >> 2;
    dsum += d;
    hist[d < 4 ? d : d < 8 ? 4 : d < 16 ? 5 : d < 64 ? 6 : 7]++;
  }
  uint64_t x = 88172645463325252ULL, msum = 0, mmax = 0, nm = 1u << 20;
  for (uint64_t r = 0; r < nm; r++) {
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    uint64_t idx = x & (len - 1) & DRAMBLAST_BUCKET_IDX_MASK, n = 1;
    for (;;) {
      dramblast_kv_t *bk = &ht->table[idx];
      if (!bk[0].k || !bk[1].k || !bk[2].k || !bk[3].k) break;
      if (++n > len / 4) break;
      idx = (idx + 4) & (len - 1);
    }
    msum += n;
    if (n > mmax) mmax = n;
  }
  printf("dramblast table %s slots=%lu occupied=%lu alpha=%.4f disp_mean=%.3f "
         "hit_buckets_mean=%.3f miss_buckets_mean=%.3f miss_buckets_max=%lu "
         "disp_hist=%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu\n",
         when, len, occ, (double)occ / len, occ ? (double)dsum / occ : 0,
         occ ? (double)dsum / occ + 1 : 0, (double)msum / nm, mmax, hist[0], hist[1],
         hist[2], hist[3], hist[4], hist[5], hist[6], hist[7]);
}
