#ifndef _MAGLEV_DPDK_H_
#define _MAGLEV_DPDK_H_

// Maglev Init
void maglev_init(void);

// Maglev process frame
uint64_t maglev_process_frame(void *frame);


#endif /* _MAGLEV_DPDK_H_ */
