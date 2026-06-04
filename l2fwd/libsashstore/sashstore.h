#ifndef _SASHSTORE_DPDK_H_
#define _SASHSTORE_DPDK_H_

// Maglev Init
void sashstore_init(void);

// Maglev process frame
int64_t sashstore_process_frame(void *frame, size_t len);

int64_t handle_network_request(void *payload);

void print_sashstore_stats(uint64_t start, uint64_t end);
#endif /* _SASHSTORE_DPDK_H_ */
