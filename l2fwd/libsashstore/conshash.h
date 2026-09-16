#ifndef _RLTEST_CONSHASH_H
#define _RLTEST_CONSHASH_H

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>

#include "hash.h"

#define TABLE_SIZE 65537

typedef int Node;
/* Holds a backend identifier that callers use as a MAC value. Must be wider
 * than a byte: as int8_t, the 0xff written by populate_lut() sign-extended to
 * -1 in every caller, so every flow mapped to the same all-ones pseudo-MAC. */
typedef int64_t LookUpTable[TABLE_SIZE];

void populate_lut(LookUpTable lut);

#endif
