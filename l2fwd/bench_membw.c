/*
 * A workload whose memory traffic is known by construction, for validating the
 * counters the saturation study depends on.
 *
 * docs/INVESTIGATION.md section 5.23 records why this file exists: `perf` has no
 * JSON event file for this part (family 6, model 207), so generic aliases can
 * map to nothing and read zero, and a raw encoding can be wrong in a way that
 * still produces a plausible number. The defence is to point each counter at a
 * workload whose answer is computable and check that it agrees before pointing
 * it at the forwarder, where the answer is what we are trying to find out.
 *
 * Three modes, each isolating one thing:
 *
 *   stream  Sequential read of a buffer several times L3, for a fixed TIME.
 *           Used for the achievable-ceiling measurement, where the answer
 *           wanted is a rate.
 *
 *   streamn Sequential read for a fixed NUMBER of passes. This is the one the
 *           counter validation uses, and the distinction matters. Comparing a
 *           counter against a program's self-reported GB/s compares two numbers
 *           that both come from the same clock, so it is a softer check than it
 *           looks, and a factor-of-two tolerance is wide enough to pass a wrong
 *           umask. With a fixed pass count the expected traffic is arithmetic
 *           -- passes x buffer bytes, no timing anywhere -- and differencing
 *           two pass counts cancels allocation, first touch and whatever else
 *           happens once per run. That supports a +/-15% bound instead of 2x.
 *           (Method due to the peer DRAMHiT session, which held its own
 *           bandwidth probe to that standard and pointed out that mine did
 *           not.)
 *
 *   chase   Pointer chase around a random cycle through the same buffer. Every
 *           load depends on the previous one, so at most ONE miss is ever
 *           outstanding: L1D_PEND_MISS.PENDING / PENDING_CYCLES must come out
 *           at ~1.0. This is the known answer that tells us the MLP encoding is
 *           right -- a wrong umask will not land on 1.0 by luck.
 *
 *   rmw/rmwn  Read-modify-write straight through a buffer far larger than L3,
 *           dirtying one word in every cache line. This is the MIXED ceiling,
 *           and it exists because a read-only peak is the wrong denominator for
 *           this workload in the one direction that matters.
 *
 *           DRAM does not deliver reads and writes symmetrically: a mixed
 *           stream pays write-drain and bus-turnaround costs a pure read stream
 *           never sees, so the achievable mixed ceiling sits materially below
 *           the achievable read ceiling. Dividing l2fwd's mixed traffic by a
 *           read-only peak therefore UNDERSTATES how much of the memory system
 *           it is using -- it flatters, making the forwarder look further from
 *           saturation than it is, which is precisely the error a saturation
 *           study cannot afford and which would look plausible and
 *           conservative while being neither. (Raised by the peer DRAMHiT
 *           session, whose own peak probe is read-only by design.)
 *
 *           The pattern is also l2fwd's own: every line is filled and then
 *           dirtied, so DRAM sees one read and one write per line -- a ~50/50
 *           mix, matching the 2.68-of-5.78 GB/s write share sampled during a
 *           live sweep. Traffic per pass is arithmetic in both directions:
 *           one buffer of reads and one buffer of writebacks.
 *
 *   nomem   A dependent ALU chain in registers. Touches no memory at all, so
 *           the memory counters must read ~0 and top-down must be almost
 *           entirely retiring. Catches a counter that is reading something
 *           unrelated, which a zero-reading counter cannot be distinguished
 *           from by any other means.
 *
 * Usage: bench_membw <stream|streamn|rmw|rmwn|chase|nomem> <MiB> <seconds|passes>
 * Prints the bytes it actually touched, so the expected counter value is
 * computable from the program's own output rather than assumed.
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>

static double now(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec + ts.tv_nsec * 1e-9;
}

int main(int argc, char **argv) {
  if (argc < 4) {
    fprintf(stderr, "usage: %s <stream|streamn|rmw|rmwn|chase|nomem> <MiB> "
                    "<seconds, or passes for streamn>\n", argv[0]);
    return 2;
  }
  const char *mode = argv[1];
  size_t mib = strtoul(argv[2], NULL, 10);
  double secs = strtod(argv[3], NULL);
  size_t bytes = mib * 1024 * 1024;

  /* MADV_HUGEPAGE so the validation is not dominated by page walks, which is
     a different resource and is measured separately. */
  char *buf = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (buf == MAP_FAILED) { perror("mmap"); return 1; }
  madvise(buf, bytes, MADV_HUGEPAGE);
  memset(buf, 1, bytes);

  size_t nlines = bytes / 64;
  double t0, t1;
  uint64_t touched = 0, sink = 0;

  if (!strcmp(mode, "chase")) {
    /* A single random cycle over all cache lines, so the walk cannot be
       predicted and every step is a dependent miss. */
    uint64_t *idx = malloc(nlines * sizeof(uint64_t));
    for (size_t i = 0; i < nlines; i++) idx[i] = i;
    uint64_t s = 88172645463325252ULL;
    for (size_t i = nlines - 1; i > 0; i--) {   /* Fisher-Yates */
      s ^= s << 13; s ^= s >> 7; s ^= s << 17;
      size_t j = s % (i + 1);
      uint64_t t = idx[i]; idx[i] = idx[j]; idx[j] = t;
    }
    for (size_t i = 0; i < nlines; i++)         /* link into one cycle */
      *(uint64_t **)(buf + idx[i] * 64) =
          (uint64_t *)(buf + idx[(i + 1) % nlines] * 64);
    free(idx);

    uint64_t *p = (uint64_t *)buf;
    t0 = now();
    while (now() - t0 < secs) {
      for (int k = 0; k < 100000; k++) p = *(uint64_t **)p;
      touched += 100000 * 64;
    }
    t1 = now();
    sink = (uint64_t)(uintptr_t)p;
  } else if (!strcmp(mode, "stream")) {
    t0 = now();
    while (now() - t0 < secs) {
      uint64_t acc = 0;
      for (size_t i = 0; i < bytes / 8; i += 8) acc += *(uint64_t *)(buf + i * 8);
      touched += bytes;
      sink += acc;
    }
    t1 = now();
  } else if (!strcmp(mode, "rmw") || !strcmp(mode, "rmwn")) {
    /* One store per 64 B line, so every line is fetched and left dirty.
       `rmwn` takes a pass count instead of a duration, because the CEILING
       measurement has to difference two pass counts -- see below. */
    int fixed = !strcmp(mode, "rmwn");
    long reps = (long)secs;
    t0 = now();
    if (fixed) {
      for (long r = 0; r < reps; r++) {
        for (size_t off = 0; off < bytes; off += 64)
          *(uint64_t *)(buf + off) += 1;
        touched += bytes * 2;   /* one buffer in, one buffer back out */
      }
    } else {
      while (now() - t0 < secs) {
        for (size_t off = 0; off < bytes; off += 64)
          *(uint64_t *)(buf + off) += 1;
        touched += bytes * 2;
      }
    }
    t1 = now();
    sink = *(uint64_t *)buf;
  } else if (!strcmp(mode, "streamn")) {
    long reps = (long)secs;          /* third argument is a pass count here */
    t0 = now();
    for (long r = 0; r < reps; r++) {
      uint64_t acc = 0;
      for (size_t i = 0; i < bytes / 8; i += 8) acc += *(uint64_t *)(buf + i * 8);
      touched += bytes;
      sink += acc;
    }
    t1 = now();
  } else {                                      /* nomem */
    uint64_t a = 1;
    t0 = now();
    while (now() - t0 < secs) {
      for (int k = 0; k < 1000000; k++) a = a * 6364136223846793005ULL + 1;
    }
    t1 = now();
    sink = a;
  }

  double el = t1 - t0;
  printf("mode=%s buffer=%zu MiB elapsed=%.3f s touched=%llu B "
         "rate=%.2f GB/s sink=%llu\n",
         mode, mib, el, (unsigned long long)touched,
         touched / el / 1e9, (unsigned long long)sink);
  return 0;
}
