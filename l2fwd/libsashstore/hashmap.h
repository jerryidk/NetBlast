#ifndef _RLTEST_HASHMAP_H
#define _RLTEST_HASHMAP_H

#include <stdlib.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <rte_cycles.h>

// change this is part of command line argument.
// #define CAPACITY ((1ULL << 20) * 16)
// uint64_t CAPACITY = 16 * (1 << 20);
//
extern uint64_t CAPACITY;

/* maglev KV pair */
struct __attribute__((aligned(16))) maglev_kv_pair {
    uint64_t key;
    uint64_t value;
};

#define KEY_SIZE	64
#define VALUE_SIZE	64

/* sashstore KV pair */
struct sashstore_kv_pair {
    uint8_t key[KEY_SIZE];
    uint8_t value[VALUE_SIZE];
} __attribute__((packed));

/*struct __attribute__((aligned(4096))) maglev_hashmap {
    struct maglev_kv_pair pairs[CAPACITY];
};

struct __attribute__((aligned(4096))) sashstore_hashmap {
    struct sashstore_kv_pair pairs[CAPACITY];
};*/


struct maglev_hashmap {
    struct maglev_kv_pair *pairs;
};

struct sashstore_hashmap {
	struct sashstore_kv_pair *pairs;
};

int maglev_hashmap_insert(struct maglev_hashmap *map,
				uint64_t key,
				uint64_t value);

struct maglev_kv_pair* maglev_hashmap_get(struct maglev_hashmap *map,
				uint64_t key);

struct sashstore_kv_pair *sashstore_hashmap_insert(struct sashstore_hashmap *map,
				uint8_t *key,
				uint8_t *value);

struct sashstore_kv_pair *sashstore_hashmap_get(struct sashstore_hashmap *map,
				uint8_t *key);

#endif
