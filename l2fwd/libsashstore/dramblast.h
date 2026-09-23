#ifndef _DRAMBLAST_DPDK_H_
#define _DRAMBLAST_DPDK_H_

#include <stdint.h>



typedef struct __attribute__((aligned(16))) {
    uint64_t k;
    uint64_t v;
} dramblast_kv_t;

typedef union {
    dramblast_kv_t pair;
    __int128 i128;
} dramblast_swap_kv_t;

/* Outcome of a lookup. Kept distinct from the value so that a legitimately
 * stored value of 0 is not read as a miss, and so that giving up on a full
 * table is not read as "definitively absent". */
#define DRAMBLAST_FOUND      0u /* key present; v holds the stored value     */
#define DRAMBLAST_ABSENT     1u /* key definitively not present; v = hint   */
#define DRAMBLAST_TABLE_FULL 2u /* probe bound reached; presence unknown     */

/* ABSENT v = insert hint, for dramblast_insert_at: low 32 bits first empty
   slot find saw, high 32 slots walked from home before it. Rides in v (unused
   on ABSENT) so result stays 16 B. Needs len <= 2^32 (checked at init). */
#define DRAMBLAST_ABSENT_HINT 1
typedef struct {
  uint64_t v;
  uint32_t id;
  uint32_t status;
} dramblast_result_t;

typedef struct {
  uint64_t k;
  uint32_t id;
} dramblast_arg_t;

typedef struct {
  uint64_t k;
  uint64_t idx;
  uint64_t visit_count;
  uint32_t id;
} dramblast_queue_item_t;

/* One cache line per lcore. Unpadded (24 B, indexed by lcore_id) lcores 8 and 10
   shared a line of head/tail stores: +13 ticks/pkt in find at alpha~0, ~+100 at
   alpha 0.9, since every push/pop writes head/tail (docs/REFLECT_PATH.md 3.3).
   aligned(64) makes sizeof 64, so queues[MAX_CPU] from dramblast_alloc64 has one
   line per entry. */
typedef struct __attribute__((aligned(64))) {
    dramblast_queue_item_t *find_queue;
    uint32_t find_queue_head;
    uint32_t find_queue_tail;
    uint32_t find_queue_size;
} dramblast_queue_t;

typedef struct {
  dramblast_kv_t *table;
  uint64_t len;
  dramblast_queue_t* queues; // per cpu
} dramblast_ht_t;

#define DRAMBLAST_FIND_QUEUE_SIZE 64
/* Must equal main.c's MAX_PKT_BURST: the hoisted -A -1 buffer is sized
   from it, and only that arm would overflow if MAX_PKT_BURST were raised,
   which would read as a hoisting result rather than as corruption. */
#define DRAMBLAST_MAX_BURST 64
// make multiple of 4.
#define DRAMBLAST_BUCKET_IDX_MASK ~0x3
#define DRAMBLAST_SIMD_KEY_MASK 0b01010101
extern int dramblast_queue_depth;  /* prefetch pipeline depth, power of two */
extern int dramblast_alloc_pairs; /* -1 hoisted (default), 0 one pair/burst, n extra */
void dramblast_init(void);
void dramblast_process_frames(dramblast_arg_t* args, unsigned int args_len, uint64_t* ret, unsigned int id);
void dramblast_destroy(void);
extern double dramblast_prefill_alpha; /* -P: target load factor, 0 = off */
void dramblast_prefill_part(unsigned part, unsigned nparts);
uint64_t dramblast_prefill_failures(void);
void dramblast_table_stats(const char *when);

#endif /* _MAGLEV_DPDK_H_ */
