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
extern int dramblast_insert_at(dramblast_ht_t *ht, uint64_t k, uint64_t v,
                               uint64_t hint);
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

/* T3: keys never inserted must be reported absent. Status, not v: ABSENT v
 * carries insert hint (dramblast.h), never a value. */
static void t_absent(find_fn fn, const char *label) {
  HDR("T3 absent keys must report ABSENT");
  printf("    kernel: %s\n", label);
  dramblast_ht_t *ht = make_table(TEST_TABLE_LEN);
  enum { N = 256 };
  uint64_t keys[N], out[N];
  uint32_t st[N];
  for (int i = 0; i < N; i++) keys[i] = 0xDEAD0000ULL + i;
  lookup_st(fn, ht, keys, N, out, st, 0);
  int absent = 0;
  for (int i = 0; i < N; i++) if (st[i] == DRAMBLAST_ABSENT) absent++;
  CHECK(absent == N, "%d of %d absent keys not reported ABSENT", N - absent, N);
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
  /* status, not v: ABSENT v carries insert hint (dramblast.h) */
  uint64_t out[1];
  uint32_t st1[1];
  lookup_st(dramblast_find_batch_sync, ht, &c[1], 1, out, st1, 0);
  printf("    lookup of key 0x%lx (never inserted) -> status %u\n",
         c[1], st1[0]);
  CHECK(st1[0] == DRAMBLAST_ABSENT,
        "a key that was never inserted got status %u, v 0x%lx (lane 2 holds key 0x%lx)",
        st1[0], out[0], c[2]);

  /* c[5] sits in value-lane 7. ctz(key_cmp) == 7, so dramblast.c:162 evaluates
   * cacheline[8] -- element 8 of an 8-element vector. */
  lookup_st(dramblast_find_batch_sync, ht, &c[5], 1, out, st1, 0);
  CHECK(st1[0] == DRAMBLAST_ABSENT,
        "key 0x%lx sits only in a VALUE lane and must not match; status %u v 0x%lx",
        c[5], st1[0], out[0]);

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

/* ABSENT hint as spec: scalar walk from home to first empty slot; high 32
   bits = slots passed. Independent of find's bucket/lane arithmetic. */
static uint64_t ref_hint(const dramblast_ht_t *ht, uint64_t k) {
  uint64_t idx = ref_hash(k, ht->len), c = 0;
  while (ht->table[idx].k != 0 && c < ht->len) {
    idx = (idx + 1) & (ht->len - 1);
    c++;
  }
  return idx | (c << 32);
}

static uint64_t val_of(uint64_t k, unsigned round) {
  return ((k * 0x9E3779B97F4A7C15ull) ^ round) | 1;
}

/* T13: dramblast_find_batch_sync = HEAD e20527e find, per id. Spec copy
 * below (ref_find_head) = HEAD loop with push/pop/get_queue_sz helpers
 * inlined, queue depth read from queue as HEAD does. Random scenarios: table
 * len 2^8..2^12 at load 0..1.0 (1.0 = full: TABLE_FULL path), queue depth
 * 4..64, start head/tail anywhere, batch 1..64 (some 65..200: longer than
 * any queue), keys present / absent / repeated inside batch, stored values
 * incl. 0. Compared: count, id permutation, per id (status, v), and result
 * SEQUENCE. Post loop scatters by id, so any order is correct; sequence
 * checked anyway: it = bucket compare order = prefetch schedule, and D1/D3
 * (REFLECT_PATH s9) claim to leave it unchanged. A change that reorders
 * must say so and relax this. Library queue must be empty at return
 * (head == tail). ABSENT v
 * must equal ref_hint (scalar walk), not HEAD's 0. */
static uint32_t ref_find_head(dramblast_ht_t *ht, dramblast_arg_t *args,
                              unsigned int args_len,
                              dramblast_result_t *results, unsigned int id) {
  dramblast_queue_t *q = &ht->queues[id];
  unsigned int args_head = 0, result_head = 0;
  uint64_t idx, count;
  while (result_head < args_len) {
    while (args_head < args_len &&
           ((q->find_queue_head - q->find_queue_tail) &
            (q->find_queue_size - 1)) < q->find_queue_size - 1) {
      dramblast_arg_t *a = &args[args_head++];
      idx = ref_hash(a->k, ht->len);
      dramblast_queue_item_t *s = &q->find_queue[q->find_queue_head];
      s->idx = idx; s->k = a->k; s->id = a->id; s->visit_count = 0;
      q->find_queue_head = (q->find_queue_head + 1) & (q->find_queue_size - 1);
    }
    dramblast_queue_item_t *t = &q->find_queue[q->find_queue_tail];
    q->find_queue_tail = (q->find_queue_tail + 1) & (q->find_queue_size - 1);
    idx = t->idx;
    count = t->visit_count;
    uint64_t *bucket = (uint64_t *)&ht->table[idx];
    __m512i cl = _mm512_load_si512(bucket);
    __mmask8 key_cmp = _mm512_mask_cmpeq_epu64_mask(
        DRAMBLAST_SIMD_KEY_MASK, cl, _mm512_set1_epi64(t->k));
    dramblast_result_t *r = &results[result_head];
    if (key_cmp > 0) {
      r->v = bucket[__builtin_ctz(key_cmp) + 1];
      r->id = t->id; r->status = DRAMBLAST_FOUND; result_head++;
      continue;
    }
    count += 4;
    if (count >= ht->len) {
      r->v = 0; r->id = t->id; r->status = DRAMBLAST_TABLE_FULL; result_head++;
      continue;
    }
    __mmask8 ept_cmp = _mm512_mask_cmpeq_epu64_mask(
        DRAMBLAST_SIMD_KEY_MASK, cl, _mm512_setzero_si512());
    if (ept_cmp == 0) {
      idx = (idx + 4) & (ht->len - 1) & (uint64_t)DRAMBLAST_BUCKET_IDX_MASK;
      dramblast_queue_item_t *s = &q->find_queue[q->find_queue_head];
      uint64_t k = t->k; uint32_t kid = t->id;
      s->idx = idx; s->k = k; s->id = kid; s->visit_count = count;
      q->find_queue_head = (q->find_queue_head + 1) & (q->find_queue_size - 1);
    } else {
      r->v = 0; r->id = t->id; r->status = DRAMBLAST_ABSENT; result_head++;
    }
  }
  return result_head;
}

/* fill table to ~alpha with random keys via insert_one; keys[] gets them */
static unsigned fill_random(dramblast_ht_t *ht, double alpha, uint64_t *keys,
                            unsigned cap, uint64_t *s) {
  uint64_t want = (uint64_t)(alpha * (double)ht->len);
  unsigned n = 0;
  for (uint64_t occ = 0; occ < want && n < cap;) {
    uint64_t k = xs64(s);
    if (!k) continue;
    uint64_t v = (xs64(s) & 7) ? xs64(s) : 0; /* some stored 0 */
    if (dramblast_insert_one(ht, k, v) == 0) { keys[n++] = k; occ++; }
  }
  return n;
}

static int cmp_res_id(const void *a, const void *b) {
  const dramblast_result_t *x = a, *y = b;
  return x->id < y->id ? -1 : x->id > y->id;
}

static void t_find_equiv(void) {
  HDR("T13 find_batch_sync = HEAD find, per id (status, v)");
  static const unsigned lens[] = {1u << 8, 1u << 10, 1u << 12};
  static const double alphas[] = {0, 0.1, 0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0};
  static const unsigned depths[] = {4, 8, 16, 32, 64};
  enum { MAXB = 200 };
  dramblast_arg_t args[MAXB];
  dramblast_result_t rl[MAXB], rr[MAXB];
  uint64_t s = 0x6A09E667F3BCC909ull;
  size_t batches = 0, lookups = 0, mism = 0, badcnt = 0, badperm = 0,
         notempty = 0, seq_same = 0, st[3] = {0, 0, 0};
  for (unsigned li = 0; li < sizeof lens / sizeof *lens; li++)
    for (unsigned ai = 0; ai < sizeof alphas / sizeof *alphas; ai++) {
      dramblast_ht_t *ht = make_table(lens[li]);
      uint64_t *keys = malloc(sizeof(uint64_t) * lens[li]);
      unsigned nk = fill_random(ht, alphas[ai], keys, lens[li], &s);
      for (unsigned di = 0; di < sizeof depths / sizeof *depths; di++) {
        unsigned d = depths[di];
        for (int qi = 0; qi < 2; qi++) { /* 0 library, 1 reference */
          ht->queues[qi].find_queue_size = d;
          ht->queues[qi].find_queue_head = ht->queues[qi].find_queue_tail =
              (uint32_t)(xs64(&s) & (d - 1));
        }
        for (int it = 0; it < 120; it++) {
          unsigned n = (it % 10 == 9) ? 65 + (unsigned)(xs64(&s) % (MAXB - 64))
                                      : 1 + (unsigned)(xs64(&s) % 64);
          for (unsigned j = 0; j < n; j++) {
            uint64_t r = xs64(&s) % 10;
            uint64_t k;
            if (r < 5 && nk) k = keys[xs64(&s) % nk];          /* present  */
            else if (r < 8 || j == 0) do k = xs64(&s); while (!k); /* absent */
            else k = args[xs64(&s) % j].k;                     /* repeat   */
            args[j].k = k; args[j].id = j;
          }
          unsigned gl = dramblast_find_batch_sync(ht, args, n, rl, 0);
          unsigned gr = ref_find_head(ht, args, n, rr, 1);
          batches++; lookups += n;
          if (gl != n || gr != n) { badcnt++; continue; }
          if (ht->queues[0].find_queue_head != ht->queues[0].find_queue_tail)
            notempty++;
          int same = 1;
          for (unsigned j = 0; j < n; j++)
            same &= rl[j].id == rr[j].id && rl[j].status == rr[j].status &&
                    (rl[j].v == rr[j].v || rr[j].status == DRAMBLAST_ABSENT);
          seq_same += same;
          qsort(rl, n, sizeof *rl, cmp_res_id);
          qsort(rr, n, sizeof *rr, cmp_res_id);
          for (unsigned j = 0; j < n; j++) {
            if (rl[j].id != j || rr[j].id != j) { badperm++; break; }
            if (rr[j].status < 3) st[rr[j].status]++;
            /* ABSENT: HEAD v = 0, library v = insert hint (dramblast.h) */
            uint64_t ev = rr[j].status == DRAMBLAST_ABSENT
                              ? ref_hint(ht, args[j].k) : rr[j].v;
            if (rl[j].status != rr[j].status || rl[j].v != ev) mism++;
          }
        }
      }
      free(keys);
      free_table(ht);
    }
  printf("    %zu batches, %zu lookups (ref: found %zu, absent %zu, full %zu):"
         " mismatches %zu, bad count %zu, bad id set %zu, queue not empty %zu,"
         " same result order %zu/%zu\n", batches, lookups, st[0], st[1], st[2],
         mism, badcnt, badperm, notempty, seq_same, batches);
  CHECK(mism == 0, "%zu lookups differ from HEAD find", mism);
  CHECK(badcnt == 0 && badperm == 0, "count %zu / id set %zu wrong", badcnt, badperm);
  CHECK(notempty == 0, "library queue not empty at return %zu times", notempty);
  CHECK(seq_same == batches, "result order differs from HEAD in %zu of %zu batches",
        batches - seq_same, batches);
  CHECK(st[0] > 1000 && st[1] > 1000 && st[2] > 1000,
        "path coverage: found %zu absent %zu full %zu", st[0], st[1], st[2]);
}

/* T14: insert_at(hint) = insert_one, slot for slot, single thread. Two copies
 * of one table; per batch: library find on each, ABSENT results inserted in
 * result order, copy A via insert_one (old path), copy B via insert_at with
 * find's hint (new path). Batches carry new keys, in-batch duplicates of new
 * keys (both ABSENT, same hint), present keys (value update next round).
 * Also: whole post loop through real dramblast_process_frames on B vs old
 * post loop emulated on A; and crafted wrap case where failure bound decides
 * (table full but 2 slots: home, home - 1). */
static void t_insert_at_equiv(void) {
  HDR("T14 insert_at(find hint) = insert_one from home, slot for slot");
  static const unsigned lens[] = {1u << 8, 1u << 10, 1u << 12};
  static const double alphas[] = {0, 0.3, 0.6, 0.8, 0.9, 0.95, 0.99};
  enum { MAXB = 64 };
  dramblast_arg_t args[MAXB];
  dramblast_result_t ra[MAXB], rb[MAXB];
  uint64_t s = 0xBB67AE8584CAA73Bull;
  size_t batches = 0, absent = 0, dup_hint = 0, hint_bad = 0, res_diff = 0,
         rc_diff = 0, fails = 0, tbl_diff = 0, pf_tbl_diff = 0, pf_ret_diff = 0;
  for (unsigned li = 0; li < 3; li++)
    for (unsigned ai = 0; ai < sizeof alphas / sizeof *alphas; ai++) {
      unsigned len = lens[li];
      dramblast_ht_t *A = make_table(len), *B = make_table(len);
      uint64_t *keys = malloc(sizeof(uint64_t) * len * 2);
      unsigned nk = fill_random(A, alphas[ai], keys, len, &s);
      memcpy(B->table, A->table, len * sizeof(dramblast_kv_t));
      for (unsigned round = 0; round < 60; round++) {
        unsigned n = 1 + (unsigned)(xs64(&s) % MAXB);
        for (unsigned j = 0; j < n; j++) {
          uint64_t r = xs64(&s) % 10, k;
          if (r < 3 && nk) k = keys[xs64(&s) % nk];
          else if (r < 7 || j == 0) do k = xs64(&s); while (!k);
          else k = args[xs64(&s) % j].k;
          args[j].k = k; args[j].id = j;
        }
        unsigned ga = dramblast_find_batch_sync(A, args, n, ra, 0);
        unsigned gb = dramblast_find_batch_sync(B, args, n, rb, 0);
        batches++;
        if (ga != n || gb != n) { res_diff++; continue; }
        uint64_t seen_hint[MAXB]; unsigned nh = 0;
        for (unsigned j = 0; j < n; j++) {
          if (ra[j].id != rb[j].id || ra[j].status != rb[j].status ||
              (ra[j].status == DRAMBLAST_FOUND && ra[j].v != rb[j].v)) {
            res_diff++; continue;
          }
          if (rb[j].status != DRAMBLAST_ABSENT) continue;
          uint64_t k = args[rb[j].id].k;
          absent++;
          if (rb[j].v != ref_hint(B, k)) hint_bad++;
          for (unsigned h = 0; h < nh; h++) if (seen_hint[h] == rb[j].v) { dup_hint++; break; }
          seen_hint[nh++] = rb[j].v;
        }
        /* ref_hint above read B before any insert of this batch: hint is
           state at find time, as post loop sees it */
        for (unsigned j = 0; j < n; j++) {
          if (rb[j].status != DRAMBLAST_ABSENT) continue;
          uint64_t k = args[rb[j].id].k, v = val_of(k, round);
          int x = dramblast_insert_one(A, k, v);
          int y = dramblast_insert_at(B, k, v, rb[j].v);
          rc_diff += x != y;
          fails += x < 0;
          if (x == 0 && nk < 2 * len) keys[nk++] = k;
        }
        tbl_diff += memcmp(A->table, B->table, len * sizeof(dramblast_kv_t)) != 0;
      }
      /* real post loop: process_frames on B (D3 wiring) vs HEAD post loop
         emulated on A. dramblast_backends never populated here (no init):
         backend value 0 for every insert, as emulated. */
      dramblast_ht_t *saved = dramblast_ht;
      int saved_ap = dramblast_alloc_pairs;
      dramblast_alloc_pairs = 0; /* no hoisted buffer without init */
      for (unsigned round = 0; round < 60; round++) {
        unsigned n = 1 + (unsigned)(xs64(&s) % MAXB);
        for (unsigned j = 0; j < n; j++) {
          uint64_t r = xs64(&s) % 10, k;
          if (r < 3 && nk) k = keys[xs64(&s) % nk];
          else if (r < 7 || j == 0) do k = xs64(&s); while (!k);
          else k = args[xs64(&s) % j].k;
          args[j].k = k; args[j].id = j;
        }
        uint64_t reta[MAXB], retb[MAXB];
        memset(reta, 0xAB, sizeof reta); memset(retb, 0xCD, sizeof retb);
        unsigned ga = dramblast_find_batch_sync(A, args, n, ra, 0);
        for (unsigned j = 0; j < ga; j++) {
          uint64_t m = 0;
          if (ra[j].status == DRAMBLAST_ABSENT) {
            uint64_t k = args[ra[j].id].k;
            m = 0; /* dramblast_backends[k % TABLE_SIZE] == 0 here */
            if (dramblast_insert_one(A, k, m) < 0) m = 0;
          } else if (ra[j].status == DRAMBLAST_FOUND) m = ra[j].v;
          reta[ra[j].id] = m;
        }
        dramblast_ht = B;
        dramblast_process_frames(args, n, retb, 0);
        pf_ret_diff += memcmp(reta, retb, n * sizeof(uint64_t)) != 0;
        pf_tbl_diff += memcmp(A->table, B->table, len * sizeof(dramblast_kv_t)) != 0;
      }
      dramblast_ht = saved;
      dramblast_alloc_pairs = saved_ap;
      free(keys);
      free_table(A); free_table(B);
    }
  printf("    %zu batches, %zu ABSENT inserts (%zu share hint with earlier key"
         " in batch, %zu failed both paths): hint wrong %zu, results differ"
         " %zu, return code differs %zu, tables differ after %zu batches\n",
         batches, absent, dup_hint, fails, hint_bad, res_diff, rc_diff, tbl_diff);
  printf("    process_frames (new) vs HEAD post loop: ret[] differs %zu,"
         " table differs %zu\n", pf_ret_diff, pf_tbl_diff);
  CHECK(hint_bad == 0 && res_diff == 0, "hint wrong %zu, results differ %zu",
        hint_bad, res_diff);
  CHECK(rc_diff == 0 && tbl_diff == 0, "rc differs %zu, tables differ %zu",
        rc_diff, tbl_diff);
  CHECK(pf_ret_diff == 0 && pf_tbl_diff == 0,
        "process_frames: ret differs %zu, table differs %zu", pf_ret_diff, pf_tbl_diff);
  CHECK(dup_hint > 100, "in-batch same-hint case exercised only %zu times", dup_hint);

  /* crafted wrap: K1, K2 same home h; table full except slot h + L (lane
     L = 0..3 of home bucket) and slot h - 1 (last slot before home, reached
     only at count == len). K1 takes h + L; K2 must walk whole ring to h - 1
     and succeed, on both paths. Catches any hint count off by >= 1. */
  for (unsigned L = 0; L < 4; L++) {
    unsigned len = 1u << 8;
    uint64_t c2[2];
    uint64_t h = ref_hash(4242, len);
    collide(h, len, c2, 2);
    dramblast_ht_t *A = make_table(len), *B = make_table(len);
    uint64_t f = 0x77;
    for (unsigned i = 0; i < len; i++) {
      A->table[i].k = f + i * 0x10001ull; /* filler, never 0, never c2 */
      A->table[i].v = 1;
    }
    A->table[h + L].k = A->table[h + L].v = 0;
    A->table[(h - 1) & (len - 1)].k = A->table[(h - 1) & (len - 1)].v = 0;
    memcpy(B->table, A->table, len * sizeof(dramblast_kv_t));
    dramblast_arg_t a2[2] = {{c2[0], 0}, {c2[1], 1}};
    dramblast_result_t r2[2];
    dramblast_find_batch_sync(B, a2, 2, r2, 0);
    int ok = 1, x[2], y[2];
    for (int j = 0; j < 2; j++) {
      ok &= r2[j].status == DRAMBLAST_ABSENT;
      uint64_t k = a2[r2[j].id].k;
      x[j] = dramblast_insert_one(A, k, 5 + j);
      y[j] = dramblast_insert_at(B, k, 5 + j, r2[j].v);
    }
    int same = memcmp(A->table, B->table, len * sizeof(dramblast_kv_t)) == 0;
    /* same batch through real post loop (hint wiring): C gets backend 0 as
       value, so compare keys only */
    dramblast_ht_t *C = make_table(len), *saved = dramblast_ht;
    memcpy(C->table, A->table, len * sizeof(dramblast_kv_t));
    for (int j = 0; j < 2; j++) /* undo A's inserts in C: pre-batch state */
      for (unsigned i = 0; i < len; i++)
        if (C->table[i].k == c2[j]) C->table[i].k = C->table[i].v = 0;
    int saved_ap = dramblast_alloc_pairs;
    uint64_t ret2[2];
    dramblast_alloc_pairs = 0;
    dramblast_ht = C;
    dramblast_process_frames(a2, 2, ret2, 0);
    dramblast_ht = saved;
    dramblast_alloc_pairs = saved_ap;
    int pf_same = 1;
    for (unsigned i = 0; i < len; i++) pf_same &= C->table[i].k == A->table[i].k;
    printf("    wrap case lane %u: ABSENT both %d, insert_one rc %d %d,"
           " insert_at rc %d %d, tables same %d, process_frames keys same %d\n",
           L, ok, x[0], x[1], y[0], y[1], same, pf_same);
    CHECK(ok && x[0] == 0 && x[1] == 0 && y[0] == 0 && y[1] == 0 && same && pf_same,
          "wrap case lane %u: rc %d %d / %d %d, same %d, pf %d", L, x[0], x[1],
          y[0], y[1], same, pf_same);
    free_table(A); free_table(B); free_table(C);
  }
}

/* T15: 2 threads, overlapping key sets, find + insert_at as post loop does,
 * racing on one table. Other thread can fill hint slot between find and
 * insert (with other key, or with same shared key). After each round: no
 * key twice, every key present with its value, filler intact, no insert
 * failed. Same stress on old path (insert_one) as control. */
#include <pthread.h>
enum { T15_LEN = 1u << 13, T15_PER = 1500, T15_SHARED = 750, T15_ROUNDS = 300 };
struct t15_arg {
  dramblast_ht_t *ht;
  const uint64_t *keys; /* T15_PER keys, this thread's order */
  unsigned qid, round;
  int use_at;
  pthread_barrier_t *bar;
  size_t fails, hint_taken_other, hint_taken_same;
  uint64_t seed;
};

static void *t15_worker(void *p) {
  struct t15_arg *a = p;
  dramblast_arg_t args[64];
  dramblast_result_t res[64];
  pthread_barrier_wait(a->bar);
  for (unsigned i = 0; i < T15_PER;) {
    unsigned n = 1 + (unsigned)(xs64(&a->seed) % 64);
    unsigned m = 0;
    for (; m < n && i < T15_PER; m++) {
      /* 1 in 16: repeat earlier key of this batch (in-burst duplicate) */
      if (m && (xs64(&a->seed) & 15) == 0) args[m].k = args[xs64(&a->seed) % m].k;
      else args[m].k = a->keys[i++];
      args[m].id = m;
    }
    unsigned got = dramblast_find_batch_sync(a->ht, args, m, res, a->qid);
    uint64_t mine[64]; /* keys this thread inserted this batch */
    unsigned nm = 0;
    for (unsigned j = 0; j < got; j++) {
      if (res[j].status != DRAMBLAST_ABSENT) continue;
      uint64_t k = args[res[j].id].k;
      /* hint slot was empty at find. Now held by key not inserted by this
         thread this batch -> other thread filled it in between */
      uint64_t sk = a->ht->table[res[j].v & 0xffffffffu].k;
      int by_me = 0;
      for (unsigned h = 0; h < nm; h++) by_me |= mine[h] == sk;
      if (sk && !by_me) {
        if (sk == k) a->hint_taken_same++;
        else a->hint_taken_other++;
      }
      mine[nm++] = k;
      int rc = a->use_at ? dramblast_insert_at(a->ht, k, val_of(k, 0), res[j].v)
                         : dramblast_insert_one(a->ht, k, val_of(k, 0));
      a->fails += rc < 0;
    }
  }
  return NULL;
}

static void t_insert_race(void) {
  HDR("T15 2-thread insert race: no duplicate, nothing lost");
  uint64_t s = 0x3C6EF372FE94F82Bull;
  for (int use_at = 0; use_at < 2; use_at++) {
    size_t dups = 0, missing = 0, badv = 0, filler_bad = 0, fails = 0,
           taken_other = 0, taken_same = 0;
    for (unsigned round = 0; round < T15_ROUNDS; round++) {
      dramblast_ht_t *ht = make_table(T15_LEN);
      uint64_t *fk = malloc(sizeof(uint64_t) * T15_LEN);
      unsigned nf = fill_random(ht, 0.5, fk, T15_LEN, &s);
      /* shared keys first in both orders, so both threads race on them */
      uint64_t k0[T15_PER], k1[T15_PER];
      for (unsigned i = 0; i < T15_PER; i++) {
        do k0[i] = xs64(&s); while (!k0[i]);
        k1[i] = i < T15_SHARED ? k0[i] : 0;
        if (!k1[i]) do k1[i] = xs64(&s); while (!k1[i]);
      }
      for (unsigned i = T15_SHARED; i > 1; i--) { /* shuffle t1's shared */
        unsigned j = (unsigned)(xs64(&s) % i);
        uint64_t t = k1[i - 1]; k1[i - 1] = k1[j]; k1[j] = t;
      }
      pthread_barrier_t bar;
      pthread_barrier_init(&bar, NULL, 2);
      struct t15_arg a[2] = {
          {ht, k0, 0, round, use_at, &bar, 0, 0, 0, xs64(&s) | 1},
          {ht, k1, 1, round, use_at, &bar, 0, 0, 0, xs64(&s) | 1}};
      pthread_t th[2];
      for (int t = 0; t < 2; t++) pthread_create(&th[t], NULL, t15_worker, &a[t]);
      for (int t = 0; t < 2; t++) pthread_join(th[t], NULL);
      pthread_barrier_destroy(&bar);
      for (int t = 0; t < 2; t++) {
        fails += a[t].fails;
        taken_other += a[t].hint_taken_other;
        taken_same += a[t].hint_taken_same;
      }
      /* scan: every stored key once */
      uint64_t *all = malloc(sizeof(uint64_t) * T15_LEN);
      unsigned na = 0;
      for (unsigned i = 0; i < T15_LEN; i++) if (ht->table[i].k) all[na++] = ht->table[i].k;
      qsort(all, na, sizeof(uint64_t), cmp_u64);
      for (unsigned i = 1; i < na; i++) dups += all[i] == all[i - 1];
      free(all);
      /* every key findable with right value */
      for (int t = 0; t < 2; t++)
        for (unsigned i = 0; i < T15_PER; i++) {
          uint64_t k = t ? k1[i] : k0[i], out; uint32_t stt;
          lookup_st(dramblast_find_batch_sync, ht, &k, 1, &out, &stt, 0);
          if (stt != DRAMBLAST_FOUND) missing++;
          else if (out != val_of(k, 0)) badv++;
        }
      for (unsigned i = 0; i < nf; i++) {
        uint64_t out; uint32_t stt;
        lookup_st(dramblast_find_batch_sync, ht, &fk[i], 1, &out, &stt, 0);
        filler_bad += stt != DRAMBLAST_FOUND;
      }
      free(fk);
      free_table(ht);
    }
    printf("    %s: %d rounds x 2 threads x %d keys (%d shared): duplicates %zu,"
           " missing %zu, wrong value %zu, filler lost %zu, insert failed %zu;"
           " hint slot filled by other thread between find and insert: other key"
           " %zu, same key %zu\n",
           use_at ? "insert_at (new)" : "insert_one (old)", T15_ROUNDS,
           T15_PER, T15_SHARED, dups, missing, badv, filler_bad, fails,
           taken_other, taken_same);
    CHECK(dups == 0 && missing == 0 && badv == 0 && filler_bad == 0 && fails == 0,
          "%s: dup %zu missing %zu badv %zu filler %zu fails %zu",
          use_at ? "insert_at" : "insert_one", dups, missing, badv, filler_bad, fails);
    if (use_at)
      CHECK(taken_other > 0 && taken_same > 0,
            "race not exercised: taken other %zu same %zu", taken_other, taken_same);
  }
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
  t_find_equiv();
  t_insert_at_equiv();
  t_insert_race();

  printf("\n----------------------------------------------------------\n");
  printf("pass %d   fail %d\n", g_pass, g_fail);
  printf("----------------------------------------------------------\n");
  return g_fail ? 1 : 0;
}
