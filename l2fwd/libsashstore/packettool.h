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

uint64_t flowhash(void *frame);
void *get_udp_payload(char *frame);

#endif
