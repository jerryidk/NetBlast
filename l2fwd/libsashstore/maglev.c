#include "hashmap.h"
#include "packettool.h"
#include "conshash.h"

#include "maglev.h"

// MAGLEV: Connection tracking table
static struct maglev_hashmap maglev_conntrack = {};

// MAGLEV: Lookup table
static LookUpTable maglev_lookup;

int64_t maglev_process_frame(void *frame) {
	int64_t backend = -1;
	uint64_t hash = flowhash(frame);
	if (hash > 0) {
		struct maglev_kv_pair *cached = maglev_hashmap_get(&maglev_conntrack, hash);
		if (cached == NULL) {
			// Use lookup table
			backend = maglev_lookup[hash % TABLE_SIZE];
			maglev_hashmap_insert(&maglev_conntrack, hash, backend);
		} else {
			backend = cached->value;
			// Just use the cached backend (noop in this test)
		}

	}
	return backend;
}

void maglev_init(void) {
	size_t size = CAPACITY * sizeof(struct maglev_kv_pair);
	maglev_conntrack.pairs = aligned_alloc(4096, size);

	if (!maglev_conntrack.pairs) {
		printf("Aligned alloc failed!\n");
		exit(1);
	}

	populate_lut(maglev_lookup);
}
