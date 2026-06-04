#ifndef _RLTEST_CONSHASH_H
#define _RLTEST_CONSHASH_H

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>

#include "hash.h"

#define TABLE_SIZE 65537

typedef int Node;
typedef int8_t LookUpTable[TABLE_SIZE];

void populate_lut(LookUpTable lut);

#endif
