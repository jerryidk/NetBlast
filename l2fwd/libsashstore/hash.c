// Crappy FNV-1 implementation

#include "hash.h"

const uint64_t FNV_BASIS = 0xcbf29ce484222325ull;
const uint64_t FNV_PRIME = 0x100000001b3;

__inline__ uint64_t fnv_1(char *data, size_t len) {
	return fnv_1_multi(data, len, FNV_BASIS);
}

/* Bytes XORed as unsigned. `char` is signed on x86: byte >= 0x80 sign-extended
   and flipped top 56 bits, and FNV-1 then mapped pairs of tuples to one key.
   pktgen's 16,777,216 flows came out as 8,454,144 keys (docs/REFLECT_PATH.md
   5.1). Every figure before this fix ran on half the configured flows. */
__inline__ uint64_t fnv_1_multi(char *data, size_t len, uint64_t state) {
	for (size_t i = 0; i < len; ++i) {
		state *= FNV_PRIME;
		state ^= (unsigned char)data[i];
	}
	return state;
}

__inline__ uint64_t fnv_1a(char *data, size_t len) {
	return fnv_1_multi(data, len, FNV_BASIS);
}

__inline__ uint64_t fnv_1a_multi(char *data, size_t len, uint64_t state) {
	for (size_t i = 0; i < len; ++i) {
		state ^= (unsigned char)data[i]; /* same sign-extension defect as fnv_1_multi */
		state *= FNV_PRIME;
	}
	return state;
}
