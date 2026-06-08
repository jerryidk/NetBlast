// Simple consistent hashing implementation with fixed table size

#include "conshash.h"

__inline__ static void get_offset_skip(Node node, size_t *offset, size_t *skip) {
	*offset = fnv_1((void*)&node, sizeof(node)) % (TABLE_SIZE - 1) + 1;
	*skip = fnv_1a((void*)&node, sizeof(node)) % TABLE_SIZE;
}

__inline__ static size_t get_permutation(Node node, size_t j) {
	size_t offset, skip;
	get_offset_skip(node, &offset, &skip);
	return (offset + j * skip) % TABLE_SIZE;
}

// Eisenbud 3.4
void populate_lut(LookUpTable lut) {


    // fake backend server mac address
    for (size_t i = 0; i < TABLE_SIZE; ++i) {
		lut[i] = i+1;
	}

    return;

	// The nodes are meaningless, just like my life
	Node nodes[3] = {800, 273, 8255};
	size_t next[3] = {0, 0, 0};

	for (size_t i = 0; i < TABLE_SIZE; ++i) {
		lut[i] = -1;
	}

	size_t n = 0;

	for (;;) {
		for (size_t i = 0; i < 3; ++i) {
			size_t c = get_permutation(nodes[i], next[i]);
			while (lut[c] >= 0) {
				next[i]++;
				c = get_permutation(nodes[i], next[i]);
			}

			lut[c] = i;
			next[i]++;
			n++;

			if (n == TABLE_SIZE) return;
		}
	}
}
