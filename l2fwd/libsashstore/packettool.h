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

/* flowhash4: 4 frames at once, out[i] == flowhash(fi) for every input.

   WHY: after inline rewrite, hash phase still 31 ticks/pkt on rig, IPC 2.2
   (docs/REFLECT_PATH.md s7.5). Per packet 12 imul, all on port 1, plus ~40
   other uops. Straight-line loop lets OOO overlap only neighbouring chains;
   4 chains side by side in one block give scheduler 4 independent imul
   streams. Scratch bench (L1 headers, 64-pkt burst, real compaction loop):
   22.3 -> 20.5 ticks/pkt; 2 chains 21.3. Gain small because port 1 (imul) is
   near saturated already, not latency: 12 imul/pkt = 12 cycle floor.

   Fast path only when all 4 are IPv4 TCP/UDP: then no early-out, 4 chains
   run unconditionally. Any other group -> flowhash per frame, out of line
   and cold, so fallback code costs no registers in fast path (inline
   fallback: register spills, measured same speed as no interleave;
   `unused`: TUs that never call flowhash4 get no -Wunused-function). Keys
   and bytes read identical to flowhash per frame in both paths: fast path
   reads same 15 bytes per frame (14, 23, 26-33, 4 at L4), only for frames
   that passed check. tests/test_dramblast.c T12 checks vs flowhash. */
static inline __attribute__((always_inline)) int flowhash_ok(const unsigned char *f) {
	const unsigned v = f[14], p = f[14 + 9];
	/* & not &&: one test for 4 frames, no branch per frame */
	return ((v >> 4) == 4) & ((p == 6) | (p == 17));
}

static __attribute__((noinline, cold, unused)) void
flowhash4_slow(void *f0, void *f1, void *f2, void *f3, uint64_t out[4]) {
	out[0] = flowhash(f0);
	out[1] = flowhash(f1);
	out[2] = flowhash(f2);
	out[3] = flowhash(f3);
}

static inline __attribute__((always_inline)) void
flowhash4(void *f0, void *f1, void *f2, void *f3, uint64_t out[4]) {
	const unsigned char *b0 = f0, *b1 = f1, *b2 = f2, *b3 = f3;
	if (__builtin_expect(!(flowhash_ok(b0) & flowhash_ok(b1) &
	                       flowhash_ok(b2) & flowhash_ok(b3)), 0)) {
		flowhash4_slow(f0, f1, f2, f3, out);
		return;
	}
	const unsigned char *l0 = b0 + 14 + 4 * (b0[14] & 0xf);
	const unsigned char *l1 = b1 + 14 + 4 * (b1[14] & 0xf);
	const unsigned char *l2 = b2 + 14 + 4 * (b2[14] & 0xf);
	const unsigned char *l3 = b3 + 14 + 4 * (b3[14] & 0xf);
	uint64_t h0 = 0xcbf29ce484222325ull, h1 = h0, h2 = h0, h3 = h0;
#define FLOWHASH4_STEP(p0, p1, p2, p3)                                         \
	do {                                                                   \
		h0 = FLOWHASH_FNV_STEP(h0, p0);                                \
		h1 = FLOWHASH_FNV_STEP(h1, p1);                                \
		h2 = FLOWHASH_FNV_STEP(h2, p2);                                \
		h3 = FLOWHASH_FNV_STEP(h3, p3);                                \
	} while (0)
#define FLOWHASH4_AT(off) FLOWHASH4_STEP(b0[off], b1[off], b2[off], b3[off])
	/* same byte order as flowhash: src+dst ip, proto, 4 L4 bytes */
	FLOWHASH4_AT(26); FLOWHASH4_AT(27); FLOWHASH4_AT(28); FLOWHASH4_AT(29);
	FLOWHASH4_AT(30); FLOWHASH4_AT(31); FLOWHASH4_AT(32); FLOWHASH4_AT(33);
	FLOWHASH4_AT(23);
	FLOWHASH4_STEP(l0[0], l1[0], l2[0], l3[0]);
	FLOWHASH4_STEP(l0[1], l1[1], l2[1], l3[1]);
	FLOWHASH4_STEP(l0[2], l1[2], l2[2], l3[2]);
	FLOWHASH4_STEP(l0[3], l1[3], l2[3], l3[3]);
#undef FLOWHASH4_AT
#undef FLOWHASH4_STEP
	out[0] = h0;
	out[1] = h1;
	out[2] = h2;
	out[3] = h3;
}
void *get_udp_payload(char *frame);

#endif
