// Hash map by David Detweiler and Dan Appel

#include "hashmap.h"
#include "hash.h"
#include <rte_prefetch.h>

uint8_t empty_key[KEY_SIZE] = {0};

__inline__ static unsigned int hash_fn(uint64_t x) {
  return (x * 14695981039346656037ull) >> (64 - 24);
}

// Maglev hashmap
// empty buckets have zero keys
int maglev_hashmap_insert(struct maglev_hashmap *map, uint64_t key,
                          uint64_t value) {
  // uint64_t hash = hash_fn(key);
  uint64_t hash = fnv_1((char *)&key, sizeof(key));
  for (uint64_t i = 0; i < CAPACITY; ++i) {
    uint64_t probe = hash + i;
    struct maglev_kv_pair *pair = &map->pairs[probe % CAPACITY];
    if (pair->key == 0) {

      maglev_swap_kv_t swapped;
      swapped.pair.key = key;
      swapped.pair.value = value;
      if (__sync_bool_compare_and_swap((__int128 *)pair, (__int128)0, *(__int128 *)&swapped)) {
          return 0;
      }
    }

    if (pair->key == key) {
      pair->value = value;
      return 0;
    }
  }
  return -1;
}

struct maglev_kv_pair *maglev_hashmap_get(struct maglev_hashmap *map,
                                          uint64_t key) {
  // uint64_t hash = hash_fn(key);
  uint64_t hash = fnv_1((char *)&key, sizeof(key));
  for (uint64_t i = 0; i < CAPACITY; ++i) {
    uint64_t probe = hash + i;
    struct maglev_kv_pair *pair = &map->pairs[probe % CAPACITY];
    if (pair->key == 0) {
      return NULL;
    }
    if (pair->key == key) { // hacky, uses zero key as empty marker
      return pair;
    }
  }

  return NULL;
}

// Sashstore hashmap
// empty buckets have zero keys
inline struct sashstore_kv_pair *
sashstore_hashmap_insert(struct sashstore_hashmap *map, uint8_t *key,
                         uint8_t *value) {
  // uint64_t hash = hash_fn(key);
  uint64_t hash = fnv_1((char *)key, KEY_SIZE);
  for (uint64_t i = 0; i < CAPACITY; ++i) {
    uint64_t probe = hash + i;
    /*if ((i + 2) < CAPACITY) {
            uint64_t p2 = hash + i + 4;
            rte_prefetch0(&map->pairs[p2 % CAPACITY]);
    }*/
    struct sashstore_kv_pair *pair = &map->pairs[probe % CAPACITY];
    if (!memcmp(pair->key, key, KEY_SIZE) ||
        !memcmp(pair->key, empty_key, KEY_SIZE)) {
      memcpy(pair->key, key, KEY_SIZE);
      memcpy(pair->value, value, VALUE_SIZE);
      return pair;
    }
  }
  return NULL;
}

inline struct sashstore_kv_pair *
sashstore_hashmap_get(struct sashstore_hashmap *map, uint8_t *key) {
  // uint64_t hash = hash_fn(key);
  uint64_t hash = fnv_1((char *)key, KEY_SIZE);
  for (uint64_t i = 0; i < CAPACITY; ++i) {
    uint64_t probe = hash + i;
    struct sashstore_kv_pair *pair = &map->pairs[probe % CAPACITY];
    if (!memcmp(pair->key, empty_key, KEY_SIZE)) {
      return NULL;
    }

    if (!memcmp(pair->key, key,
                KEY_SIZE)) { // hacky, uses zero key as empty marker
      return pair;
    }
  }

  return NULL;
}
