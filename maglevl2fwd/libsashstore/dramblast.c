
#include "conshash.h"
#include "packettool.h"
#include "rte_mbuf_core.h"
#include "dramblast.h"
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

extern uint64_t CAPACITY;
static LookUpTable dramblast_backends;

dramblast_ht_t *dramblast_ht;

inline uint32_t dramblast_get_queue_sz(dramblast_ht_t *ht) {
  return (ht->find_queue_head - ht->find_queue_tail) &
         (ht->find_queue_size - 1);
}

inline void dramblast_push_queue(dramblast_ht_t *ht, uint64_t idx, uint64_t k,
                                 uint64_t id) {
  dramblast_queue_item_t *queue_head_slot =
      &ht->find_queue[ht->find_queue_head];
  queue_head_slot->idx = idx;
  queue_head_slot->k = k;
  queue_head_slot->id = id;
  ht->find_queue_head++;
  ht->find_queue_head = ht->find_queue_head & (ht->find_queue_size - 1);
}

inline dramblast_queue_item_t *dramblast_pop_queue(dramblast_ht_t *ht) {
  dramblast_queue_item_t *queue_tail_slot =
      &ht->find_queue[ht->find_queue_tail];
  ht->find_queue_tail++;
  ht->find_queue_tail = ht->find_queue_tail & (ht->find_queue_size - 1);

  return queue_tail_slot;
}

// Define hint levels based on Intel/GCC standards
#define PREFETCH_T0 0 // Temporal: Load into all levels of cache (L1/L2/L3)
#define PREFETCH_T1 1 // Temporal: Load into L2/L3
#define PREFETCH_T2 2 // Temporal: Load into L3
#define PREFETCH_NTA 3 // Non-Temporal: Minimize cache pollution (e.g., streaming)

// Macro to encode the instruction
#define LX_PREFETCH(addr, level) _mm_prefetch((const char*)(addr), (level))

// Updated dramblast_prefetch function
inline void dramblast_prefetch(dramblast_ht_t *ht, uint64_t idx) {
    // Using PREFETCH_T0 is standard for items you are about to access immediately
    LX_PREFETCH(&ht->table[idx], PREFETCH_T0);
}

inline uint64_t dramblast_hash(dramblast_ht_t *ht, uint64_t k) {
  uint64_t hash = _mm_crc32_u64(0, k);
#ifdef SSE42
  hash = _mm_crc32_u64(0, k);
#else
  // 64bit golden prime
  hash = k * 0x9E3779B97F4A7C15ULL;
#endif

  return (uint64_t)hash & (ht->len - 1) & DRAMBLAST_BUCKET_IDX_MASK;
}

void dramblast_insert_one(dramblast_ht_t *ht, uint64_t k, uint64_t v) {


  uint64_t idx = dramblast_hash(ht, k);
  dramblast_kv_t *kv;

  uint64_t count = 0;

  if(k == 0) {
      return;
  }

  try_insert:
    kv = &ht->table[idx];
    count++;
    if (kv->k == 0) {
      kv->k = k;
      kv->v = v;
      return;
    }

    if (kv->k == k) {
      kv->v = v;
      return;
    }

    idx++;
    if(!(idx & 0x3)){
        dramblast_prefetch(ht, idx);
    }

    if(count >= ht->len)
        return;

    goto try_insert;
}

// Note: args_len is also length of results array
uint32_t dramblast_find_batch_sync(dramblast_ht_t *ht, dramblast_arg_t *args,
                                  unsigned int args_len,
                                  dramblast_result_t *results) {

  unsigned int args_head = 0;
  unsigned int result_head = 0;


  while (result_head < args_len) {

    // push as many as possible without stalling on LFB.
    uint64_t idx;
    while (args_head < args_len &&
           dramblast_get_queue_sz(ht) < ht->find_queue_size - 1) {
      dramblast_arg_t *arg = &args[args_head];
      args_head++;
      idx = dramblast_hash(ht, arg->k);
      dramblast_prefetch(ht, idx);
      dramblast_push_queue(ht, idx, arg->k, arg->id);
    }

    // pop_find_queue
    dramblast_queue_item_t *queue_tail_slot = dramblast_pop_queue(ht);
    idx = queue_tail_slot->idx;
    dramblast_kv_t *bucket = (dramblast_kv_t *)&ht->table[idx];
    __m512i cacheline = _mm512_load_si512(bucket);
    __m512i key_vector = _mm512_set1_epi64(queue_tail_slot->k);
    __m512i zero_vector = _mm512_setzero_si512();
    __mmask8 key_cmp = _mm512_mask_cmpeq_epu64_mask(DRAMBLAST_SIMD_KEY_MASK,
                                                    cacheline, key_vector);
    if (key_cmp > 0) {
      int offset = __builtin_ctz(key_cmp);
      dramblast_result_t *result = &results[result_head];
      result->v = cacheline[(offset + 1)];
      result->id = queue_tail_slot->id;
      result_head++;
    } else {
      __mmask8 ept_cmp = _mm512_mask_cmpeq_epu64_mask(DRAMBLAST_SIMD_KEY_MASK,
                                                      cacheline, zero_vector);
      if (ept_cmp == 0) {
        idx += 4;
        idx = idx & (ht->len - 1);
        idx = idx & DRAMBLAST_BUCKET_IDX_MASK;
        dramblast_prefetch(ht, idx);
        dramblast_push_queue(ht, idx, queue_tail_slot->k, queue_tail_slot->id);
      } else {
        // didn't found
        dramblast_result_t *result = &results[result_head];
        result->v = 0;
        result->id = queue_tail_slot->id;
        result_head++;
      }
    }
  }

  return result_head;
}

// return number of frames modified.
void dramblast_process_frames(dramblast_arg_t* args, unsigned int args_len, uint64_t* ret) {
  dramblast_result_t *results =
      aligned_alloc(64, sizeof(dramblast_result_t) * args_len);

  unsigned int len =
      dramblast_find_batch_sync(dramblast_ht, args, args_len, results);

  if(len != args_len){
      printf("dramblast sync is not correct ");
      exit(-1);
  }

  for (unsigned int i = 0; i < len; i++) {
    int64_t backend_mac_addr;
    dramblast_result_t *result = &results[i];

    if (result->v == 0) {
      uint64_t client_hash = args[result->id].k;
      backend_mac_addr = dramblast_backends[client_hash % TABLE_SIZE];
      printf("mapping 0x%016lx to 0x%016lx\n", client_hash, backend_mac_addr);
      dramblast_insert_one(dramblast_ht, client_hash, backend_mac_addr);
    } else {
      backend_mac_addr = result->v;
    }

    ret[result->id] = backend_mac_addr;
  }

  free(results);
}

#define MAP_HUGE_2MB (21 << 26)
#define MAP_HUGE_1GB (30 << 26)

void* allocate_dramblast_table(size_t bytes) {
    int flags = MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB;

    // Select page size based on capacity
    if (bytes > 1024 * 1024 * 1024) { // > 1GB
        flags |= MAP_HUGE_1GB;
    } else {
        flags |= MAP_HUGE_2MB;
    }

    void* ptr = mmap(NULL, bytes, PROT_READ | PROT_WRITE, flags, -1, 0);

    if (ptr == MAP_FAILED) {
        perror("mmap hugepages failed, falling back to standard allocation");
        // Fallback: standard allocation if hugepages are unavailable
        ptr = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        return NULL;
    }

    memset(ptr, 0, bytes);

    return ptr;
}

void dramblast_init(void) {
  dramblast_ht = aligned_alloc(64, sizeof(dramblast_ht_t));

  if (!dramblast_ht) {
    printf("Aligned alloc failed!\n");
    exit(1);
  }

  dramblast_ht->find_queue_size = DRAMBLAST_FIND_QUEUE_SIZE;
  dramblast_ht->find_queue = aligned_alloc(
      64, sizeof(dramblast_queue_item_t) * dramblast_ht->find_queue_size);
  dramblast_ht->find_queue_head = 0;
  dramblast_ht->find_queue_tail = 0;

  dramblast_ht->len = CAPACITY;
  // using hugepages 2mb or 1gb for hsahtbale base on table capacity.
  uint64_t bytes = dramblast_ht->len * sizeof(dramblast_kv_t);
  dramblast_ht->table = allocate_dramblast_table(bytes);

  if(!dramblast_ht->table){
      printf("dramblast->table alloc failed!\n");
      exit(1);
  }

  populate_lut(dramblast_backends);

  printf("dramblast initialized!\n");
}

void dramblast_destroy()
{
  if (dramblast_ht == NULL) {
    return;
  }

  /* 1. Unmap the hugepage table allocated via mmap */
  if (dramblast_ht->table != NULL) {
    uint64_t bytes = (uint64_t)dramblast_ht->len * sizeof(dramblast_kv_t);
    if (munmap(dramblast_ht->table, bytes) != 0) {
      perror("munmap failed during dramblast_destroy");
    }
    dramblast_ht->table = NULL;
  }

  /* 2. Free the find queue buffer allocated via aligned_alloc */
  if (dramblast_ht->find_queue != NULL) {
    free(dramblast_ht->find_queue);
    dramblast_ht->find_queue = NULL;
  }

  /* 3. Free the root state structure */
  free(dramblast_ht);
  dramblast_ht = NULL;

  printf("dramblast destroyed and memory cleaned up successfully.\n");
}
