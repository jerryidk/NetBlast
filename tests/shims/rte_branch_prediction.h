/* Test-harness shim: minimal stand-in for DPDK's rte_branch_prediction.h.
 * Lets libsashstore compile without libdpdk. Not used by the real build. */
#ifndef _TEST_SHIM_RTE_BRANCH_PREDICTION_H_
#define _TEST_SHIM_RTE_BRANCH_PREDICTION_H_
#ifndef likely
#define likely(x)   __builtin_expect(!!(x), 1)
#endif
#ifndef unlikely
#define unlikely(x) __builtin_expect(!!(x), 0)
#endif
#endif
