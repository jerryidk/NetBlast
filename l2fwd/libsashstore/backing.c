#include "backing.h"

#include <stdio.h>
#include <string.h>
#include <sys/mman.h>

#define MAP_HUGE_2MB (21 << 26)
#define MAP_HUGE_1GB (30 << 26)
#define SZ_2M (2ULL * 1024 * 1024)
#define SZ_1G (1024ULL * 1024 * 1024)
#define ALIGN_UP(x, a) (((x) + (a) - 1) & ~((a) - 1))

backing_t g_backing = BACKING_AS_SHIPPED;

backing_t backing_effective(backing_t as_shipped) {
  return g_backing == BACKING_AS_SHIPPED ? as_shipped : g_backing;
}

const char *backing_name(backing_t b) {
  switch (b) {
  case BACKING_1G:    return "1g";
  case BACKING_THP2M: return "thp2m";
  case BACKING_4K:    return "4k";
  default:            return "as-shipped";
  }
}

int backing_parse(const char *s, backing_t *out) {
  if (!strcmp(s, "1g"))              *out = BACKING_1G;
  else if (!strcmp(s, "thp2m"))      *out = BACKING_THP2M;
  else if (!strcmp(s, "4k"))         *out = BACKING_4K;
  else if (!strcmp(s, "as-shipped")) *out = BACKING_AS_SHIPPED;
  else return -1;
  return 0;
}

size_t backing_alloc_size(size_t bytes, backing_t as_shipped) {
  return backing_effective(as_shipped) == BACKING_1G ? ALIGN_UP(bytes, SZ_1G)
                                                     : ALIGN_UP(bytes, SZ_2M);
}

void *backing_alloc(size_t bytes, backing_t as_shipped) {
  backing_t b = backing_effective(as_shipped);
  size_t n = backing_alloc_size(bytes, as_shipped);
  int flags = MAP_PRIVATE | MAP_ANONYMOUS;

  /*
   * Only the 1 GiB arm uses hugetlb. A 2 MiB arm via MAP_HUGETLB|MAP_HUGE_2MB
   * is NOT usable here: 8 GiB needs 4096 pages and the 2 MiB pool has exactly
   * 4096 total, of which DPDK's own -m 2000 has already taken ~1000. It would
   * fail at mmap. THP reaches the same page size out of ordinary free memory,
   * and is also what maglev already gets as shipped, so the 2 MiB arm is THP
   * by design rather than by fallback.
   */
  if (b == BACKING_1G)
    flags |= MAP_HUGETLB | MAP_HUGE_1GB;

  void *p = mmap(NULL, n, PROT_READ | PROT_WRITE, flags, -1, 0);
  if (p == MAP_FAILED) {
    perror("backing_alloc: mmap");
    return NULL;
  }

  /*
   * THP is `always` on this host, so an 8 GiB anonymous mapping is promoted to
   * 2 MiB pages whether or not we ask. Both non-hugetlb arms therefore have to
   * state their intent explicitly: without MADV_NOHUGEPAGE the 4k arm would be
   * silently promoted and would measure the thp2m arm instead.
   */
  if (b == BACKING_THP2M) {
    if (madvise(p, n, MADV_HUGEPAGE) != 0)
      perror("backing_alloc: madvise(MADV_HUGEPAGE)");
  } else if (b == BACKING_4K) {
    if (madvise(p, n, MADV_NOHUGEPAGE) != 0)
      perror("backing_alloc: madvise(MADV_NOHUGEPAGE)");
  }

  memset(p, 0, n);

  printf("backing: %s (%s), %.2f GiB mapped at %p\n", backing_name(b),
         g_backing == BACKING_AS_SHIPPED ? "call-site default" : "forced by -B",
         (double)n / (1024.0 * 1024.0 * 1024.0), p);
  fflush(stdout);
  return p;
}
