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

/* flowhash moved to packettool.h as static inline (see there). */
