#include "backing.h"
#include "conshash.h"
#include "hashmap.h"
#include "packettool.h"

#include "maglev.h"

// MAGLEV: Connection tracking table
static struct maglev_hashmap maglev_conntrack = {};

// MAGLEV: Lookup table
static LookUpTable maglev_lookup;

// using 0 as marker for invalid backend server mac address.
uint64_t maglev_process_frame(void *frame) {
  uint64_t backend = 0;
  uint64_t hash = flowhash(frame);

  // if (hash == 0) {
  //   // printf("hash 0\n");
  //   return 0;
  // }

  if(hash > 0)
  {
      struct maglev_kv_pair *cached = maglev_hashmap_get(&maglev_conntrack, hash);
      if (cached == NULL) {
        // Use lookup table
        backend = maglev_lookup[hash % TABLE_SIZE];
        // insertion can fail if it is full.
        if (maglev_hashmap_insert(&maglev_conntrack, hash, backend) < 0) {
          printf("hashtable insertion failed, need more memory\n");
          return 0;
        }
      } else {
        backend = cached->value;
        // Just use the cached backend (noop in this test)
      }
  }
  return backend;
}

void maglev_init(void) {
  size_t size = CAPACITY * sizeof(struct maglev_kv_pair);
  /* aligned_alloc(4096, 8 GiB) as shipped, which THP promotes to 2 MiB pages
     (measured: whole table shows up in AnonHugePages). Stated explicitly here
     so the -B matrix can move it to 1 GiB or hold it down at 4 KiB. */
  maglev_conntrack.pairs = backing_alloc(size, BACKING_THP2M);

  if (!maglev_conntrack.pairs) {
    printf("Aligned alloc failed!\n");
    exit(1);
  }

  for(size_t i = 0; i< CAPACITY; i++){
      maglev_conntrack.pairs[i].key = 0;
      maglev_conntrack.pairs[i].value = 0;
  }

  populate_lut(maglev_lookup);
}
