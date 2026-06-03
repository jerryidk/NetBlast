#ifndef _DRAMBLAST_DPDK_H_
#define _DRAMBLAST_DPDK_H_

#include <stdint.h>


typedef struct {
  uint64_t k;
  uint64_t v;
} dramblast_kv_t;

typedef struct {
  uint64_t v;
  uint64_t id;
} dramblast_result_t;

// align this to 16
typedef struct {
  uint64_t k;
  uint32_t id;
} dramblast_arg_t;

typedef struct {
  uint64_t k;
  uint64_t idx;
  uint32_t id;
} dramblast_queue_item_t;

typedef struct {
  dramblast_kv_t *table;
  dramblast_queue_item_t *find_queue;
  uint32_t find_queue_head;
  uint32_t find_queue_tail;
  uint32_t find_queue_size;
  uint64_t len;
} dramblast_ht_t;

#define DRAMBLAST_CAPACITY 1024
#define DRAMBLAST_FIND_QUEUE_SIZE 64
// make multiple of 4.
#define DRAMBLAST_BUCKET_IDX_MASK ~0x3
#define DRAMBLAST_SIMD_KEY_MASK 0b10101010
void dramblast_init(void);
void dramblast_process_frames(dramblast_arg_t* args, unsigned int args_len, uint64_t* ret);
void dramblast_destroy(void);

#endif /* _MAGLEV_DPDK_H_ */
