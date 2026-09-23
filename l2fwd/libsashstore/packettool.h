#ifndef _RLTEST_PACKETTOOL_H
#define _RLTEST_PACKETTOOL_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "hash.h"

extern const uint64_t ETH_HEADER_LEN;
extern const uint64_t IPV4_PROTO_OFFSET;
extern const uint64_t IPV4_LENGTH_OFFSET;
extern const uint64_t IPV4_CHECKSUM_OFFSET;
extern const uint64_t IPV4_SRCDST_OFFSET;
extern const uint64_t IPV4_SRCDST_LEN;
extern const uint64_t UDP_LENGTH_OFFSET;
extern const uint64_t UDP_CHECKSUM_OFFSET;

/* flowhash: FNV-1 over 13 bytes = src+dst IPv4 (8), proto (1), L4 src+dst
   port (4). 0 = not IPv4 or not TCP/UDP (also dramblast empty-slot key).

   static inline in header, WHY: libsashstore built b_lto=false, so old
   out-of-line flowhash in packettool.c never inlined into main.c hash loop,
   and it made 3 calls to fnv_1_multi with variable-trip byte loops. Hash
   phase = 49 ticks/pkt of ~120 (docs/REFLECT_PATH.md s3.2). Here: fixed 13
   steps, fully unrolled, visible to main.c and maglev.c.

   Keys bit-identical to old code for every input (tests/test_dramblast.c
   T11 checks vs transcription of old loop): same byte order, bytes XORed
   unsigned (s5.1 fix), version test (byte >> 4 == 4) and proto test (6/17)
   give same answer signed or unsigned, IHL 0..15 read same offset
   14 + 4*IHL (IHL < 5 reads inside IP header, as before). Reads up to frame
   byte 77. fnv_1_multi etc. stay in hash.c for hashmap.c / conshash.c. */
#define FLOWHASH_FNV_STEP(h, b) ((h) * 0x100000001b3ull ^ (uint64_t)(b))

static inline __attribute__((always_inline)) uint64_t flowhash(void *frame) {
	const unsigned char *f = (const unsigned char *)frame;
	const unsigned char vihl = f[14];              /* ETH 14: version|IHL */
	const unsigned char proto = f[14 + 9];
	if ((vihl >> 4) != 4) return 0;
	if (proto != 6 && proto != 17) return 0;
	const unsigned char *a = f + 14 + 12;          /* src ip, dst ip */
	const unsigned char *l4 = f + 14 + 4 * (vihl & 0xf);
	uint64_t h = 0xcbf29ce484222325ull;            /* FNV_BASIS */
	h = FLOWHASH_FNV_STEP(h, a[0]);
	h = FLOWHASH_FNV_STEP(h, a[1]);
	h = FLOWHASH_FNV_STEP(h, a[2]);
	h = FLOWHASH_FNV_STEP(h, a[3]);
	h = FLOWHASH_FNV_STEP(h, a[4]);
	h = FLOWHASH_FNV_STEP(h, a[5]);
	h = FLOWHASH_FNV_STEP(h, a[6]);
	h = FLOWHASH_FNV_STEP(h, a[7]);
	h = FLOWHASH_FNV_STEP(h, proto);
	h = FLOWHASH_FNV_STEP(h, l4[0]);
	h = FLOWHASH_FNV_STEP(h, l4[1]);
	h = FLOWHASH_FNV_STEP(h, l4[2]);
	h = FLOWHASH_FNV_STEP(h, l4[3]);
	return h;
}
void *get_udp_payload(char *frame);

#endif
