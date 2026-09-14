#ifndef _BACKING_H_
#define _BACKING_H_

#include <stddef.h>

/*
 * Page backing for the big hash tables, made selectable at run time.
 *
 * As shipped, the two modes differ in more than their lookup algorithm: they
 * allocate the same 8 GiB of table on different page sizes.
 *
 *   dramblast  mmap(MAP_HUGETLB | MAP_HUGE_1GB)   -> 1 GiB pages, 8 TLB entries
 *   maglev     aligned_alloc(4096, ...)           -> 2 MiB THP, 4096 TLB entries
 *
 * (Measured, not assumed: with a maglev run live, /proc/meminfo reported
 * AnonHugePages 8,513,536 kB and the process's own smaps_rollup reported
 * AnonHugePages 8,331,264 kB, i.e. the whole table is transparently promoted
 * to 2 MiB pages. THP is `always` on this host.)
 *
 * That is a confound: any per-packet cost difference between the two modes is
 * a difference in algorithm AND in address translation at once. This lets
 * either mode be run on either backing so the two can be separated.
 *
 * g_backing is BACKING_AS_SHIPPED unless -B is given, in which case every call
 * site is overridden, which is what makes the 2x2 a pure command-line matrix.
 */
typedef enum {
  BACKING_AS_SHIPPED = 0, /* each call site keeps its own default */
  BACKING_1G,             /* explicit 1 GiB hugetlb pages */
  BACKING_THP2M,          /* anonymous + MADV_HUGEPAGE -> 2 MiB THP */
  BACKING_4K,             /* anonymous + MADV_NOHUGEPAGE -> 4 KiB pages */
} backing_t;

extern backing_t g_backing;

/* Which backing this call site will actually get. */
backing_t backing_effective(backing_t as_shipped);

/* Map `bytes` (rounded up to the backing's page size), zeroed. NULL on failure. */
void *backing_alloc(size_t bytes, backing_t as_shipped);

/* The length backing_alloc() actually mapped, for munmap(). */
size_t backing_alloc_size(size_t bytes, backing_t as_shipped);

const char *backing_name(backing_t b);
int backing_parse(const char *s, backing_t *out); /* 0 ok, -1 unrecognised */

#endif /* _BACKING_H_ */
