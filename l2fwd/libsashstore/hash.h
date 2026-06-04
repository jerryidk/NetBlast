#ifndef _RLTEST_HASH_H
#define _RLTEST_HASH_H

#include <stddef.h>
#include <stdint.h>

extern const uint64_t FNV_BASIS;
extern const uint64_t FNV_PRIME;

uint64_t fnv_1(char *data, size_t len);
uint64_t fnv_1_multi(char *data, size_t len, uint64_t state);
uint64_t fnv_1a(char *data, size_t len);
uint64_t fnv_1a_multi(char *data, size_t len, uint64_t state);

#endif
