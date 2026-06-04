// Crappy FNV-1 implementation

#include "hash.h"

const uint64_t FNV_BASIS = 0xcbf29ce484222325ull;
const uint64_t FNV_PRIME = 0x100000001b3;

__inline__ uint64_t fnv_1(char *data, size_t len) {
	return fnv_1_multi(data, len, FNV_BASIS);
}

__inline__ uint64_t fnv_1_multi(char *data, size_t len, uint64_t state) {
	for (size_t i = 0; i < len; ++i) {
		state *= FNV_PRIME;
		state ^= data[i];
	}
	return state;
}

__inline__ uint64_t fnv_1a(char *data, size_t len) {
	return fnv_1_multi(data, len, FNV_BASIS);
}

__inline__ uint64_t fnv_1a_multi(char *data, size_t len, uint64_t state) {
	for (size_t i = 0; i < len; ++i) {
		state ^= data[i];
		state *= FNV_PRIME;
	}
	return state;
}
