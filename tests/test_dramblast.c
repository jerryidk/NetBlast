/*
 * Functional test harness for l2fwd/libsashstore/dramblast.c
 *
 * Scope: FUNCTIONAL CORRECTNESS ONLY. Nothing here measures performance.
 *
 * The harness links the real dramblast.c unmodified. It does not call
 * dramblast_init(), because that mmap()s hugepages and calls populate_lut();
 * instead it builds a dramblast_ht_t by hand over an aligned_alloc'd table so
 * the tests are hermetic and run on a machine with no hugepages reserved.
 *
 * Reference for expected behaviour: docs/cas_kht.hpp (KEYMSK at :61,
 * bucket[offset+1] at :611, _bit_scan_forward(ept_cmp) >> 1 at :1038).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <immintrin.h>
#include <sys/mman.h>
#include <unistd.h>
#include <nmmintrin.h>

#include "dramblast.h"
#include "conshash.h"
#include "packettool.h"

/* dramblast.c declares this extern and main.c defines it. */
uint64_t CAPACITY = 0;

/* Symbols with external linkage in dramblast.c but absent from dramblast.h.
 * Note: dramblast_hash/push_queue/pop_queue/get_queue_sz are `inline` WITHOUT
 * `static` (finding C5), so they have no external definition to link against.
 * The harness therefore re-implements them locally rather than calling them. */
extern dramblast_ht_t *dramblast_ht;
extern int dramblast_insert_one(dramblast_ht_t *ht, uint64_t k, uint64_t v);
extern uint32_t dramblast_find_batch_sync(dramblast_ht_t *ht,
                                          dramblast_arg_t *args,
                                          unsigned int args_len,
                                          dramblast_result_t *results,
                                          unsigned int id);

/* ------------------------------------------------------------------ */
/* harness plumbing                                                     */
/* ------------------------------------------------------------------ */

static int g_pass, g_fail;

#define CHECK(cond, fmt, ...)                                                  \
  do {                                                                         \
    if (cond) {                                                                \
      g_pass++;                                                                \
    } else {                                                                   \
      g_fail++;                                                                \
      printf("    FAIL: " fmt "\n", ##__VA_ARGS__);                            \
    }                                                                          \
  } while (0)

#define HDR(name) printf("\n[%s]\n", name)

/* Recorded behaviour that is undefined, so it cannot be asserted on. */
#define OBSERVE(fmt, ...) printf("    OBSERVE: " fmt "\n", ##__VA_ARGS__)

/* Local copy of dramblast_hash() (cannot link to the inline original). */
#define TEST_TABLE_LEN (1ULL << 16)
static uint64_t ref_hash(uint64_t k, uint64_t len) {
  uint64_t h = _mm_crc32_u64(0, k);
  return h & (len - 1) & (uint64_t)DRAMBLAST_BUCKET_IDX_MASK;
}

/* C11: aligned_alloc()'s size must be a multiple of the alignment. */
static void *alloc64(size_t bytes) {
  return aligned_alloc(64, (bytes + 63) & ~(size_t)63);
}

static dramblast_ht_t *make_table(uint64_t len) {
  dramblast_ht_t *ht = alloc64(sizeof(*ht));
  ht->len = len;
  ht->table = alloc64(len * sizeof(dramblast_kv_t));
  memset(ht->table, 0, len * sizeof(dramblast_kv_t));
  ht->queues = alloc64(sizeof(dramblast_queue_t) * 8);
  for (int i = 0; i < 8; i++) {
    ht->queues[i].find_queue_head = 0;
    ht->queues[i].find_queue_tail = 0;
    ht->queues[i].find_queue_size = DRAMBLAST_FIND_QUEUE_SIZE;
    ht->queues[i].find_queue =
        alloc64(sizeof(dramblast_queue_item_t) *
                DRAMBLAST_FIND_QUEUE_SIZE);
  }
  return ht;
}

static void free_table(dramblast_ht_t *ht) {
  for (int i = 0; i < 8; i++) free(ht->queues[i].find_queue);
  free(ht->queues);
  free(ht->table);
  free(ht);
}

/* ------------------------------------------------------------------ */
/* An independent transcription of the find kernel, used as a           */
/* cross-check: both implementations must agree on every test.          */
/* ------------------------------------------------------------------ */
#define FIXED_KEY_MASK 0b01010101

static uint32_t find_batch_fixed(dramblast_ht_t *ht, dramblast_arg_t *args,
                                 unsigned int args_len,
                                 dramblast_result_t *results, unsigned int id) {
  dramblast_queue_t *q = &ht->queues[id];
  unsigned int args_head = 0, result_head = 0;
  uint64_t idx, count;

  while (result_head < args_len) {
    while (args_head < args_len &&
           (((q->find_queue_head - q->find_queue_tail) &
             (q->find_queue_size - 1)) < DRAMBLAST_FIND_QUEUE_SIZE - 1)) {
      dramblast_arg_t *a = &args[args_head++];
      idx = ref_hash(a->k, ht->len);
      _mm_prefetch((const char *)&ht->table[idx], 1);
      dramblast_queue_item_t *s = &q->find_queue[q->find_queue_head];
      s->idx = idx; s->k = a->k; s->id = a->id; s->visit_count = 0;
      q->find_queue_head = (q->find_queue_head + 1) & (q->find_queue_size - 1);
    }

    dramblast_queue_item_t *t = &q->find_queue[q->find_queue_tail];
    q->find_queue_tail = (q->find_queue_tail + 1) & (q->find_queue_size - 1);

    idx = t->idx;
    count = t->visit_count;
    uint64_t *bucket = (uint64_t *)&ht->table[idx];
    __m512i cacheline = _mm512_load_si512(bucket);
    __m512i key_vector = _mm512_set1_epi64(t->k);
    __m512i zero_vector = _mm512_setzero_si512();
    __mmask8 key_cmp =
        _mm512_mask_cmpeq_epu64_mask(FIXED_KEY_MASK, cacheline, key_vector);

    if (key_cmp > 0) {
      int offset = __builtin_ctz(key_cmp);
      results[result_head].v = bucket[offset + 1];
      results[result_head].id = t->id;
      results[result_head].status = DRAMBLAST_FOUND;
      result_head++;
    } else {
      count += 4;
      if (count >= ht->len) {
        results[result_head].v = 0;
        results[result_head].id = t->id;
        results[result_head].status = DRAMBLAST_TABLE_FULL;
        result_head++;
        continue;
      }
      __mmask8 ept_cmp =
          _mm512_mask_cmpeq_epu64_mask(FIXED_KEY_MASK, cacheline, zero_vector);
      if (ept_cmp == 0) {
        idx = (idx + 4) & (ht->len - 1) & (uint64_t)DRAMBLAST_BUCKET_IDX_MASK;
        _mm_prefetch((const char *)&ht->table[idx], 1);
        dramblast_queue_item_t *s = &q->find_queue[q->find_queue_head];
        s->idx = idx; s->k = t->k; s->id = t->id; s->visit_count = count;
        q->find_queue_head =
            (q->find_queue_head + 1) & (q->find_queue_size - 1);
      } else {
        results[result_head].v = 0;
        results[result_head].id = t->id;
        results[result_head].status = DRAMBLAST_ABSENT;
        result_head++;
      }
    }
  }
  return result_head;
}

/* ------------------------------------------------------------------ */
/* helpers                                                              */
/* ------------------------------------------------------------------ */

typedef uint32_t (*find_fn)(dramblast_ht_t *, dramblast_arg_t *, unsigned int,
                            dramblast_result_t *, unsigned int);

/* Look up `n` keys and scatter values (and optionally statuses) back by id. */
static void lookup_st(find_fn fn, dramblast_ht_t *ht, const uint64_t *keys,
                      unsigned n, uint64_t *out, uint32_t *st,
                      unsigned int qid) {
  dramblast_arg_t *args = alloc64(sizeof(*args) * n);
  dramblast_result_t *res = alloc64(sizeof(*res) * n);
  for (unsigned i = 0; i < n; i++) { args[i].k = keys[i]; args[i].id = i; }
  memset(out, 0xAB, sizeof(uint64_t) * n);
  unsigned got = fn(ht, args, n, res, qid);
  if (got != n) printf("    (returned %u results for %u args)\n", got, n);
  for (unsigned i = 0; i < got; i++) {
    out[res[i].id] = res[i].v;
    if (st) st[res[i].id] = res[i].status;
  }
  free(args); free(res);
}

static void lookup(find_fn fn, dramblast_ht_t *ht, const uint64_t *keys,
                   unsigned n, uint64_t *out, unsigned int qid) {
  lookup_st(fn, ht, keys, n, out, NULL, qid);
}

/* Find `want` distinct keys that all hash to the same bucket. */
static unsigned collide(uint64_t bucket, uint64_t len, uint64_t *out,
                        unsigned want) {
  unsigned n = 0;
  for (uint64_t k = 1; k < 40000000ULL && n < want; k++)
    if (ref_hash(k, len) == bucket) out[n++] = k;
  return n;
}

/* ------------------------------------------------------------------ */
/* tests                                                                */
/* ------------------------------------------------------------------ */

/* T1: which __m512i lanes actually hold keys, given dramblast_kv_t {k,v}? */
static void t_lane_layout(void) {
  HDR("T1 lane layout: where do keys live in a 64-byte bucket?");
  dramblast_kv_t bucket[4] __attribute__((aligned(64)));
  for (int i = 0; i < 4; i++) {
    bucket[i].k = 0x1111ULL * (i + 1);
    bucket[i].v = 0x9999ULL * (i + 1);
  }
  __m512i cl = _mm512_load_si512(bucket);
  __m512i probe = _mm512_set1_epi64(bucket[2].k); /* key of slot 2 */

  __mmask8 m_even = _mm512_mask_cmpeq_epu64_mask(0b01010101, cl, probe);
  __mmask8 m_odd  = _mm512_mask_cmpeq_epu64_mask(0b10101010, cl, probe);

  printf("    probing for a KEY: mask 0b01010101 -> 0x%02x, "
         "mask 0b10101010 -> 0x%02x\n", m_even, m_odd);
  CHECK(m_even != 0, "keys are not in the even lanes (0,2,4,6)");
  CHECK(m_odd == 0, "0b10101010 matched a key; it should select value lanes");
  CHECK(__builtin_ctz(m_even) == 4,
        "slot 2's key should be lane 4, got lane %d", __builtin_ctz(m_even));
  CHECK(((uint64_t *)bucket)[__builtin_ctz(m_even) + 1] == bucket[2].v,
        "lane+1 should be slot 2's value");
  printf("    => DRAMBLAST_SIMD_KEY_MASK must be 0b01010101 "
         "(cas_kht.hpp:61 KEYMSK)\n");
}

/* T2: insert then find must return the stored value. */
static void t_roundtrip(find_fn fn, const char *label) {
  HDR("T2 round-trip: insert N keys, then find them");
  printf("    kernel: %s\n", label);
  dramblast_ht_t *ht = make_table(TEST_TABLE_LEN);
  enum { N = 512 };
  uint64_t keys[N], out[N];
  for (int i = 0; i < N; i++) {
    keys[i] = 0x5000ULL + (uint64_t)i * 7919ULL;
    if (dramblast_insert_one(ht, keys[i], 0xB000ULL + i) != 0)
      printf("    insert failed for key %lu\n", keys[i]);
  }
  lookup(fn, ht, keys, N, out, 0);

  int hits = 0, correct = 0;
  for (int i = 0; i < N; i++) {
    if (out[i] != 0) hits++;
    if (out[i] == 0xB000ULL + i) correct++;
  }
  printf("    %d/%d reported found, %d/%d returned the right value\n",
         hits, N, correct, N);
  CHECK(correct == N, "only %d of %d keys round-tripped", correct, N);
  free_table(ht);
}

/* T3: keys never inserted must be reported absent. */
static void t_absent(find_fn fn, const char *label) {
  HDR("T3 absent keys must report v == 0");
  printf("    kernel: %s\n", label);
  dramblast_ht_t *ht = make_table(TEST_TABLE_LEN);
  enum { N = 256 };
  uint64_t keys[N], out[N];
  for (int i = 0; i < N; i++) keys[i] = 0xDEAD0000ULL + i;
  lookup(fn, ht, keys, N, out, 0);
  int absent = 0;
  for (int i = 0; i < N; i++) if (out[i] == 0) absent++;
  CHECK(absent == N, "%d of %d absent keys reported a value", N - absent, N);
  free_table(ht);
}

/* T4: re-inserting a key must update its value. */
static void t_update(find_fn fn, const char *label) {
  HDR("T4 update: second insert of the same key wins");
  printf("    kernel: %s\n", label);
  dramblast_ht_t *ht = make_table(TEST_TABLE_LEN);
  uint64_t k = 0xABCDEF01ULL, out[1];
  dramblast_insert_one(ht, k, 0x1111);
  dramblast_insert_one(ht, k, 0x2222);
  lookup(fn, ht, &k, 1, out, 0);
  CHECK(out[0] == 0x2222, "expected 0x2222, got 0x%lx", out[0]);
  free_table(ht);
}

/* T5: more than 4 colliding keys forces the idx += 4 reprobe path. */
static void t_reprobe(find_fn fn, const char *label) {
  HDR("T5 reprobe: >4 keys in one bucket must spill to the next");
  printf("    kernel: %s\n", label);
  dramblast_ht_t *ht = make_table(TEST_TABLE_LEN);
  enum { N = 9 };
  uint64_t keys[N], out[N];
  unsigned found = collide(ref_hash(12345, TEST_TABLE_LEN), TEST_TABLE_LEN,
                           keys, N);
  if (found < N) { printf("    SKIP: only found %u colliding keys\n", found);
                   free_table(ht); return; }
  printf("    %u keys all hashing to bucket %lu\n", found,
         ref_hash(keys[0], TEST_TABLE_LEN));
  for (int i = 0; i < N; i++) dramblast_insert_one(ht, keys[i], 0xC000ULL + i);
  lookup(fn, ht, keys, N, out, 0);
  int correct = 0;
  for (int i = 0; i < N; i++) if (out[i] == 0xC000ULL + i) correct++;
  printf("    %d/%d found after spilling across buckets\n", correct, N);
  CHECK(correct == N, "%d of %d colliding keys round-tripped", correct, N);
  free_table(ht);
}

/* T6: regression guard for the key/value lane mask. The probed
 * key must hash to the bucket that holds the matching value -- so all the keys
 * used here are chosen to collide into one bucket. */
static void t_false_hit(void) {
  HDR("T6 regression: a key must never match against a stored VALUE lane");
  dramblast_ht_t *ht = make_table(TEST_TABLE_LEN);
  uint64_t c[6];
  unsigned n = collide(ref_hash(777, TEST_TABLE_LEN), TEST_TABLE_LEN, c, 6);
  if (n < 6) { printf("    SKIP: only %u colliding keys\n", n);
               free_table(ht); return; }
  uint64_t bucket_idx = ref_hash(c[0], TEST_TABLE_LEN);
  printf("    6 keys collide into bucket %lu\n", bucket_idx);

  /* Fill all four slots of that bucket. Slot 0's VALUE is the key c[1];
   * slot 3's VALUE is the key c[5]. Lane layout becomes:
   *   [ c0 | c1 | c2 |0x1111| c3 |0x2222| c4 | c5 ]
   *   lane  0    1    2    3     4    5     6    7          */
  dramblast_insert_one(ht, c[0], c[1]);
  dramblast_insert_one(ht, c[2], 0x1111);
  dramblast_insert_one(ht, c[3], 0x2222);
  dramblast_insert_one(ht, c[4], c[5]);

  uint64_t *lanes = (uint64_t *)&ht->table[bucket_idx];
  printf("    lanes:");
  for (int i = 0; i < 8; i++) printf(" [%d]=0x%lx", i, lanes[i]);
  printf("\n");

  /* c[1] was never inserted as a KEY. Correct answer: absent.
   * Buggy mask matches it in value-lane 1, then returns lane 2 == c[2]. */
  uint64_t out[1];
  lookup(dramblast_find_batch_sync, ht, &c[1], 1, out, 0);
  printf("    lookup of key 0x%lx (never inserted) -> v = 0x%lx\n",
         c[1], out[0]);
  CHECK(out[0] == 0,
        "a key that was never inserted returned 0x%lx (lane 2 holds key 0x%lx)",
        out[0], c[2]);

  /* c[5] sits in value-lane 7. ctz(key_cmp) == 7, so dramblast.c:162 evaluates
   * cacheline[8] -- element 8 of an 8-element vector. */
  lookup(dramblast_find_batch_sync, ht, &c[5], 1, out, 0);
  CHECK(out[0] == 0,
        "key 0x%lx sits only in a VALUE lane and must not match; got 0x%lx",
        c[5], out[0]);

  /* The corrected kernel must call both of these absent. */
  lookup(find_batch_fixed, ht, &c[1], 1, out, 1);
  CHECK(out[0] == 0, "fixed kernel false-hit on 0x%lx (v = 0x%lx)",
        c[1], out[0]);
  lookup(find_batch_fixed, ht, &c[5], 1, out, 1);
  CHECK(out[0] == 0, "fixed kernel false-hit on 0x%lx (v = 0x%lx)",
        c[5], out[0]);
  /* ...and must still find the keys that really are there. */
  lookup(find_batch_fixed, ht, &c[0], 1, out, 1);
  CHECK(out[0] == c[1], "fixed kernel lost key 0x%lx (v = 0x%lx)", c[0], out[0]);
  lookup(find_batch_fixed, ht, &c[4], 1, out, 1);
  CHECK(out[0] == c[5], "fixed kernel lost key 0x%lx (v = 0x%lx)", c[4], out[0]);
  free_table(ht);
}

/* T7: result ids must be a permutation of the input ids. The async port in
 * docs/cas_kht.hpp relies on key_id being the only correlation handle. */
static void t_id_permutation(void) {
  HDR("T7 result ids are a permutation of input ids");
  dramblast_ht_t *ht = make_table(TEST_TABLE_LEN);
  enum { N = 64 };
  dramblast_arg_t args[N];
  dramblast_result_t res[N];
  int seen[N];
  memset(seen, 0, sizeof(seen));
  for (int i = 0; i < N; i++) {
    args[i].k = 0x900000ULL + i * 31;
    args[i].id = i;
    dramblast_insert_one(ht, args[i].k, 0xE000ULL + i);
  }
  unsigned got = dramblast_find_batch_sync(ht, args, N, res, 0);
  CHECK(got == N, "expected %d results, got %u", N, got);
  int dup = 0, oob = 0;
  for (unsigned i = 0; i < got; i++) {
    if (res[i].id >= N) { oob++; continue; }
    if (seen[res[i].id]++) dup++;
  }
  CHECK(oob == 0, "%d result ids out of range", oob);
  CHECK(dup == 0, "%d duplicate result ids", dup);
  free_table(ht);
}

/* T8: a value of 0 is indistinguishable from "absent" (documented limit). */
static void t_zero_value(void) {
  HDR("T8 a stored value of 0 is reported FOUND, not as a miss");
  dramblast_ht_t *ht = make_table(TEST_TABLE_LEN);
  uint64_t k = 0x5150ULL, out[2];
  uint32_t st[2];
  uint64_t keys[2] = {k, 0xBADC0FFEEULL};
  dramblast_insert_one(ht, k, 0);
  lookup_st(dramblast_find_batch_sync, ht, keys, 2, out, st, 0);
  printf("    stored (k=0x%lx, v=0)  -> v=0x%lx status=%u\n", k, out[0], st[0]);
  printf("    never-stored key       -> v=0x%lx status=%u\n", out[1], st[1]);
  CHECK(st[0] == DRAMBLAST_FOUND,
        "a stored value of 0 was reported as status %u, not FOUND", st[0]);
  CHECK(out[0] == 0, "expected v=0, got 0x%lx", out[0]);
  CHECK(st[1] == DRAMBLAST_ABSENT,
        "an absent key was reported as status %u, not ABSENT", st[1]);
  free_table(ht);
}

/* T10: the backend LUT must not sign-extend (finding C4). */
static void t_backend_lut(void) {
  HDR("T10 backend LUT values are not sign-extended");
  LookUpTable lut;
  populate_lut(lut);
  int64_t v = lut[12345 % TABLE_SIZE];
  printf("    populate_lut() entry = %ld (0x%lx), sizeof(entry) = %zu\n",
         (long)v, (unsigned long)v, sizeof(lut[0]));
  CHECK(v > 0, "LUT entry is %ld; as int8_t this sign-extended to -1", (long)v);
  CHECK((uint64_t)v != UINT64_MAX,
        "LUT entry is all-ones, the old sign-extension result");
}

/* T9: key 0 is the empty sentinel and cannot be stored. */
static void t_zero_key(void) {
  HDR("T9 key 0 is the empty sentinel");
  dramblast_ht_t *ht = make_table(TEST_TABLE_LEN);
  int rc = dramblast_insert_one(ht, 0, 0xFEED);
  printf("    dramblast_insert_one(k=0) -> %d\n", rc);
  CHECK(rc == -1, "inserting key 0 should be rejected, got %d", rc);
  printf("    matches cas_kht.hpp:1130 (__insert_one diverts the empty key)\n");
  free_table(ht);
}

/* T11: flowhash rewrite (packettool.h, static inline, unrolled) must give
 * bit-identical keys to the old one. ref_flowhash = transcription of HEAD
 * 57bf217 packettool.c flowhash + hash.c fnv_1_multi: signed `char`, 3 calls,
 * byte loops. Kept here, not in library: it is the spec the new code must hit.
 * (a) every pktgen tuple: 4 TX lcores 48..51, src 10.<lcore>.0.0 +
 *     (ctr & (2^22-1)), ctr 1..2^22, src port 1025+lcore*100, dst
 *     192.168.1.1:80, UDP. Also distinct-key count = 16777216 (s5.1).
 * (b) 10M random frames: random bytes, version nibble 4 or random, IHL
 *     0..15, proto 6 / 17 / random. */
static uint64_t ref_fnv_1_multi(char *data, size_t len, uint64_t state) {
  for (size_t i = 0; i < len; ++i) {
    state *= 0x100000001b3ull;
    state ^= (unsigned char)data[i];
  }
  return state;
}

static uint64_t ref_flowhash(void *frame) {
  char *f = (char *)frame;
  if (f[14] >> 4 != 4) return 0;
  char proto = f[14 + 9];
  if (proto != 6 && proto != 17) return 0;
  size_t v4len = 4 * (f[14] & 0b1111);
  uint64_t hash = 0xcbf29ce484222325ull;
  hash = ref_fnv_1_multi(f + 14 + 12, 8, hash);
  hash = ref_fnv_1_multi(f + 14 + 9, 1, hash);
  hash = ref_fnv_1_multi(f + 14 + v4len, 4, hash);
  return hash;
}

static int cmp_u64(const void *a, const void *b) {
  uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
  return x < y ? -1 : x > y;
}

static uint64_t xs64(uint64_t *s) { /* xorshift64*, fixed seed: repeatable */
  *s ^= *s >> 12; *s ^= *s << 25; *s ^= *s >> 27;
  return *s * 0x2545F4914F6CDD1Dull;
}

static void t_flowhash_equiv(void) {
  HDR("T11 flowhash rewrite = old flowhash, bit for bit");
  const uint32_t mask = (1u << 22) - 1;
  const size_t n = 4 * ((size_t)mask + 1);
  uint64_t *keys = malloc(n * sizeof(uint64_t));
  unsigned char f[128];
  size_t j = 0, mism = 0, zero = 0;
  uint64_t dig = 0xcbf29ce484222325ull;
  for (uint32_t c = 48; c < 52; c++) {
    uint32_t base = (10u << 24) | (c << 16);
    uint16_t sport = 1025 + c * 100;
    for (uint32_t ctr = 1; ctr <= mask + 1; ctr++) {
      memset(f, 0, sizeof f);
      f[14] = 0x45; f[14 + 9] = 17;
      uint32_t src = base + (ctr & mask), dst = 0xc0a80101u; /* big-endian out */
      for (int b = 0; b < 4; b++) {
        f[26 + b] = src >> (24 - 8 * b);
        f[30 + b] = dst >> (24 - 8 * b);
      }
      f[34] = sport >> 8; f[35] = sport & 0xff; f[36] = 0; f[37] = 80;
      uint64_t h = flowhash(f), r = ref_flowhash(f);
      if (h != r) mism++;
      if (!h) zero++;
      dig = (dig ^ h) * 0x100000001b3ull;
      keys[j++] = h;
    }
  }
  qsort(keys, n, sizeof(uint64_t), cmp_u64);
  size_t distinct = 0;
  for (size_t i = 0; i < n; i++) distinct += (!i || keys[i] != keys[i - 1]);
  free(keys);
  printf("    generator tuples %zu: mismatches %zu, zero keys %zu, distinct %zu,"
         " key-sequence digest %016lx\n", n, mism, zero, distinct, dig);
  CHECK(mism == 0, "%zu generator tuples hash differently", mism);
  CHECK(zero == 0, "%zu generator tuples hashed to 0", zero);
  CHECK(distinct == 16777216, "distinct keys %zu, want 16777216", distinct);

  enum { NRAND = 10000000 };
  uint64_t s = 0x9E3779B97F4A7C15ull;
  size_t rm = 0, valid = 0, nz_ihl_lt5 = 0;
  for (size_t i = 0; i < NRAND; i++) {
    for (size_t b = 0; b < sizeof f; b += 8) {
      uint64_t w = xs64(&s);
      memcpy(f + b, &w, 8);
    }
    uint64_t r = xs64(&s);
    /* version: half forced 4 (so most frames reach hashing), half raw byte */
    if (r & 1) f[14] = (unsigned char)(0x40 | ((r >> 8) & 0xf));
    /* proto: 1/3 TCP, 1/3 UDP, 1/3 raw byte (incl. >= 0x80) */
    switch ((r >> 16) % 3) {
    case 0: f[23] = 6; break;
    case 1: f[23] = 17; break;
    default: break;
    }
    uint64_t h = flowhash(f), rh = ref_flowhash(f);
    if (h != rh) rm++;
    if (rh) { valid++; if ((f[14] & 0xf) < 5) nz_ihl_lt5++; }
  }
  printf("    random frames %d: mismatches %zu, hashed (nonzero) %zu,"
         " of which IHL<5 %zu\n", NRAND, rm, valid, nz_ihl_lt5);
  CHECK(rm == 0, "%zu random frames hash differently", rm);
  CHECK(valid > NRAND / 4, "only %zu random frames reached hashing", valid);
}

/* T12: flowhash4 (packettool.h, 4 frames at once) must give flowhash(f) for
 * each frame, bit for bit, whatever mix of early-out frames group holds.
 * (a) pktgen tuples, 4 consecutive per group (all-valid fast path).
 * (b) 10M+ random frames (T11 generator) in groups of 4, validity mixed per
 *     frame: covers fast path (all 4 valid) and cold fallback (any invalid).
 * (c) main.c hash loop copied (x4 groups + scalar tail + compaction) vs old
 *     scalar loop, random burst sizes 1..64: same fn, frames[], args[].
 * (d) frames end 78 B before PROT_NONE page: read past byte 77 faults. */
static void rand_frame(unsigned char *f, size_t len, uint64_t *s) {
  for (size_t b = 0; b < len; b += 8) {
    uint64_t w = xs64(s);
    memcpy(f + b, &w, len - b < 8 ? len - b : 8);
  }
  uint64_t r = xs64(s);
  /* 3/4 forced version 4: groups of 4 all valid often enough (~(1/2)^4 of
     groups at T11's mix would leave fast path nearly untested) */
  if (r & 3) f[14] = (unsigned char)(0x40 | ((r >> 8) & 0xf));
  switch ((r >> 16) % 4) {
  case 0: f[23] = 6; break;
  case 1: case 2: f[23] = 17; break;
  default: break; /* raw byte, incl. >= 0x80 */
  }
}

static void t_flowhash4_equiv(void) {
  HDR("T12 flowhash4 = flowhash per frame, bit for bit");
  enum { FL = 128 };
  static unsigned char g[4][FL];
  const uint32_t mask = (1u << 22) - 1;
  size_t mism = 0, n = 0;
  for (uint32_t c = 48; c < 52; c++) {
    uint32_t base = (10u << 24) | (c << 16);
    uint16_t sport = 1025 + c * 100;
    for (uint32_t ctr = 1; ctr <= mask + 1; ctr += 4) {
      for (int u = 0; u < 4; u++) {
        unsigned char *f = g[u];
        memset(f, 0, FL);
        f[14] = 0x45; f[14 + 9] = 17;
        uint32_t src = base + ((ctr + u) & mask), dst = 0xc0a80101u;
        for (int b = 0; b < 4; b++) {
          f[26 + b] = src >> (24 - 8 * b);
          f[30 + b] = dst >> (24 - 8 * b);
        }
        f[34] = sport >> 8; f[35] = sport & 0xff; f[36] = 0; f[37] = 80;
      }
      uint64_t o[4];
      flowhash4(g[0], g[1], g[2], g[3], o);
      for (int u = 0; u < 4; u++) { mism += o[u] != flowhash(g[u]); n++; }
    }
  }
  printf("    generator tuples %zu (groups of 4): mismatches %zu\n", n, mism);
  CHECK(n == 16777216 && mism == 0, "%zu of %zu generator tuples differ", mism, n);

  enum { NGRP = 2750000 }; /* 11M frames */
  uint64_t s = 0x243F6A8885A308D3ull;
  size_t rm = 0, fast = 0, mixed = 0, zero = 0;
  for (size_t i = 0; i < NGRP; i++) {
    int nok = 0;
    for (int u = 0; u < 4; u++) {
      rand_frame(g[u], FL, &s);
      nok += flowhash(g[u]) != 0;
    }
    uint64_t o[4];
    flowhash4(g[0], g[1], g[2], g[3], o);
    for (int u = 0; u < 4; u++) {
      rm += o[u] != flowhash(g[u]);
      zero += o[u] == 0;
    }
    fast += nok == 4;
    mixed += nok > 0 && nok < 4;
  }
  printf("    random frames %d (groups of 4): mismatches %zu, groups all-valid"
         " %zu, mixed %zu, zero keys %zu\n", 4 * NGRP, rm, fast, mixed, zero);
  CHECK(rm == 0, "%zu random frames differ in flowhash4", rm);
  CHECK(fast > NGRP / 10 && mixed > NGRP / 10,
        "path coverage: all-valid %zu, mixed %zu groups", fast, mixed);

  /* (c) main.c hash loop, both shapes, over fake mbuf pointers = frames */
  static unsigned char pool[64][FL];
  void *fr_a[64], *fr_b[64];
  dramblast_arg_t ar_a[64], ar_b[64];
  size_t lm = 0, bursts = 0;
  for (size_t it = 0; it < 200000; it++) {
    unsigned nb = 1 + (unsigned)(xs64(&s) % 64);
    for (unsigned j = 0; j < nb; j++) rand_frame(pool[j], FL, &s);
    unsigned fa = 0, fb = 0, j = 0;
    for (unsigned q = 0; q < nb; q++) { /* HEAD loop */
      uint64_t h = flowhash(pool[q]);
      if (h > 0) { fr_a[fa] = pool[q]; ar_a[fa].k = h; ar_a[fa].id = fa; fa++; }
    }
    for (; j + 4 <= nb; j += 4) { /* new loop, as main.c */
      uint64_t k4[4];
      flowhash4(pool[j], pool[j + 1], pool[j + 2], pool[j + 3], k4);
      for (unsigned u = 0; u < 4; u++)
        if (k4[u] > 0) { fr_b[fb] = pool[j + u]; ar_b[fb].k = k4[u]; ar_b[fb].id = fb; fb++; }
    }
    for (; j < nb; j++) {
      uint64_t h = flowhash(pool[j]);
      if (h > 0) { fr_b[fb] = pool[j]; ar_b[fb].k = h; ar_b[fb].id = fb; fb++; }
    }
    int bad = fa != fb;
    for (unsigned q = 0; !bad && q < fa; q++)
      bad = fr_a[q] != fr_b[q] || ar_a[q].k != ar_b[q].k || ar_a[q].id != ar_b[q].id;
    lm += bad;
    bursts++;
  }
  printf("    main.c loop, %zu random bursts 1..64: differing bursts %zu\n",
         bursts, lm);
  CHECK(lm == 0, "%zu bursts compact differently", lm);

  /* (d) over-read guard: frame = last 78 B before PROT_NONE page */
  long pg = sysconf(_SC_PAGESIZE);
  unsigned char *gf[4];
  for (int u = 0; u < 4; u++) { /* one data page + one guard page per frame */
    unsigned char *mu = mmap(NULL, 2 * pg, PROT_READ | PROT_WRITE,
                             MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(mu != MAP_FAILED, "mmap guard pages");
    if (mu == MAP_FAILED) return;
    mprotect(mu + pg, pg, PROT_NONE);
    gf[u] = mu + pg - 78;
  }
  size_t gm = 0;
  for (size_t i = 0; i < 1000000; i++) {
    for (int u = 0; u < 4; u++) rand_frame(gf[u], 78, &s);
    uint64_t o[4];
    flowhash4(gf[0], gf[1], gf[2], gf[3], o);
    for (int u = 0; u < 4; u++) gm += o[u] != flowhash(gf[u]);
  }
  printf("    guard-page frames 4M (78 B, next page PROT_NONE): no fault,"
         " mismatches %zu\n", gm);
  CHECK(gm == 0, "%zu guard frames differ", gm);
  for (int u = 0; u < 4; u++) munmap(gf[u] - (pg - 78), 2 * pg);
}

int main(void) {
  printf("dramblast functional tests (no timing, no DPDK, no hugepages)\n");
  printf("table: %llu slots x %zu B = %llu KiB\n",
         (unsigned long long)TEST_TABLE_LEN, sizeof(dramblast_kv_t),
         (unsigned long long)(TEST_TABLE_LEN * sizeof(dramblast_kv_t)) / 1024);
  CAPACITY = TEST_TABLE_LEN;

  t_lane_layout();

  printf("\n=== dramblast_find_batch_sync ===");
  t_roundtrip(dramblast_find_batch_sync, "dramblast_find_batch_sync");
  t_absent(dramblast_find_batch_sync, "dramblast_find_batch_sync");
  t_update(dramblast_find_batch_sync, "dramblast_find_batch_sync");
  t_reprobe(dramblast_find_batch_sync, "dramblast_find_batch_sync");

  printf("\n=== cross-check: independent transcription ===");
  t_roundtrip(find_batch_fixed, "find_batch_fixed");
  t_absent(find_batch_fixed, "find_batch_fixed");
  t_update(find_batch_fixed, "find_batch_fixed");
  t_reprobe(find_batch_fixed, "find_batch_fixed");

  t_false_hit();
  t_id_permutation();
  t_zero_value();
  t_zero_key();
  t_backend_lut();
  t_flowhash_equiv();
  t_flowhash4_equiv();

  printf("\n----------------------------------------------------------\n");
  printf("pass %d   fail %d\n", g_pass, g_fail);
  printf("----------------------------------------------------------\n");
  return g_fail ? 1 : 0;
}
