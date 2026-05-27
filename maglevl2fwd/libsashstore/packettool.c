// Maglev flow-hash implementation

#include "packettool.h"

const uint64_t ETH_HEADER_LEN = 14;
const uint64_t UDP_HEADER_LEN = 8;

// https://en.wikipedia.org/wiki/IPv4
const uint64_t IPV4_PROTO_OFFSET = 9;
const uint64_t IPV4_LENGTH_OFFSET = 2;
const uint64_t IPV4_CHECKSUM_OFFSET = 10;
const uint64_t IPV4_SRCDST_OFFSET = 12;
const uint64_t IPV4_SRCDST_LEN = 8;
const uint64_t UDP_LENGTH_OFFSET = 4;
const uint64_t UDP_CHECKSUM_OFFSET = 6;

void hexdump(char *buf, size_t len);

void hexdump(char *buf, size_t len) {
	printf ("----> \n");
	for (size_t i = 0; i < len; i++) {
		printf("%02x ", buf[i] & 0xff);
		if ((i + 1) % 16 == 0)
			printf("\n");
	}
	printf ("END <---- \n\n");
}

void *get_udp_payload(char *frame) {

	if (frame[ETH_HEADER_LEN] >> 4 != 4) {
		// This shitty implementation can only handle IPv4 :(
		return NULL;
	}

	// Length of IPv4 header
	uint32_t v4len = (frame[ETH_HEADER_LEN] & 0xf) * 4;

	// Check IP protocol number
	uint8_t proto = frame[ETH_HEADER_LEN + IPV4_PROTO_OFFSET];
	if (proto != 17) {
		// UDP only sorry
		return NULL;
	}

	return &frame[ETH_HEADER_LEN + v4len + UDP_HEADER_LEN];
}

__inline__ uint64_t flowhash(void *frame) {
	// Warning: This implementation returns 0 for invalid inputs
	char *f = (char*)frame;

	// Fail early
	if (f[ETH_HEADER_LEN] >> 4 != 4) {
        // This shitty implementation can only handle IPv4 :(
		printf("unhandled! not ipv4?\n");
		return 0;
	}

	char proto = f[ETH_HEADER_LEN + IPV4_PROTO_OFFSET];
	if (proto != 6 && proto != 17) {
        // This shitty implementation can only handle TCP and UDP
		printf("Unhandled proto %x\n", proto);
		return 0;
	}

	size_t v4len = 4 * (f[ETH_HEADER_LEN] & 0b1111);

	uint64_t hash = FNV_BASIS;

    // Hash source/destination IP addresses
	hash = fnv_1_multi(f + ETH_HEADER_LEN + IPV4_SRCDST_OFFSET, IPV4_SRCDST_LEN, hash);

	// Hash IP protocol number
	hash = fnv_1_multi(f + ETH_HEADER_LEN + IPV4_PROTO_OFFSET, 1, hash);

    // Hash source/destination port
	hash = fnv_1_multi(f + ETH_HEADER_LEN + v4len, 4, hash);

	return hash;
}
