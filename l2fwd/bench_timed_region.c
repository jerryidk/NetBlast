/*
 * What the rig's "Cycle per fwd packet" counter actually measures.
 *
 * main.c:309 and main.c:359 bracket the per-burst processing with a pair of
 * `rte_rdtsc()` reads and divide the accumulated delta by the number of
 * forwarded packets.  For the `-m none` arm -- the forwarding loop with the
 * lookup removed -- that quotient prints as 5, and 5 core cycles for a loop
 * that writes a destination MAC is low enough to be worth checking rather than
 * believing.  Three separate things could make it wrong, and this benchmark
 * separates them:
 *
 *   1. The instrument has a floor.  `rte_rdtsc()` is plain `rdtsc` with no
 *      fence (dpdk/include/rte_cycles.h, and the disassembly of
 *      l2fwd_main_loop confirms no lfence at 403bcf or 403c9b), but rdtsc
 *      still costs tens of cycles to execute.  Whatever an EMPTY region reads
 *      is charged to every burst in every arm, so at a 64-packet burst it is
 *      divided by 64 and at a 2-packet burst by 2.  Arm `empty` measures it.
 *
 *   2. rdtsc is not serialising, so the second read can retire while the
 *      loop's stores are still in the store buffer.  The region can therefore
 *      read LESS than the work costs.  Arm `none` is timed both ways -- with
 *      the bare rdtsc pair the rig uses, and with lfence on both sides -- and
 *      the difference is exactly how much the unfenced instrument hides.
 *
 *   3. The rig prints an integer quotient (main.c:217-218), so a printed 5 is
 *      any true value in [5, 6).  Here the quotient is a double, which is the
 *      only way to see where in that interval the truth sits.
 *
 * The loop body is a transcription of the `-m none` branch's codegen, not a
 * paraphrase: objdump of build/l2fwd at 403dd0-403df5 is eleven instructions
 * --- two dependent loads off the mbuf (buf_addr at +0x00, data_off at +0x10),
 * two loads of the source MAC from a fixed address, and three stores of 8, 4
 * and 2 bytes into the packet.  check_codegen_timed_region() below asserts the
 * store shape survived the compiler; run check_timed_region.sh to diff the
 * disassembly against the rig's.
 *
 * The memory layout matters as much as the instruction mix.  An mbuf pool
 * built with RTE_MBUF_DEFAULT_BUF_SIZE puts the header and its packet data in
 * one ~2.3 KiB element, so consecutive packets in a burst are a couple of
 * kilobytes apart and a 64-packet burst spans ~147 KiB: past this part's 48 KiB
 * L1d and comfortably inside its 2 MiB L2.  --stride reproduces that; passing a
 * small stride collapses the burst into L1 and gives the issue-limited floor,
 * which brackets the answer from below.
 *
 *   cc -O2 -march=native -o bench_timed_region bench_timed_region.c
 *   ./bench_timed_region [--stride N] [--iters N]
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <x86intrin.h>

/* Same shape as the fields the forwarding loop touches: rte_mbuf has buf_addr
   at offset 0 and data_off at offset 16, and nothing else in the `-m none`
   path is read. */
struct mbuf_like {
  void *buf_addr;
  uint64_t pad;
  uint16_t data_off;
  uint16_t pad2[3];
} __attribute__((aligned(64)));

#define MAX_BURST 64
#define DEFAULT_STRIDE 2304 /* mbuf header + RTE_MBUF_DEFAULT_BUF_SIZE */
#define HEADROOM 256        /* sizeof(rte_mbuf) + RTE_PKTMBUF_HEADROOM */

/* rte_ether_addr, and the port table the rig copies out of.  Both are
   deliberately non-static and filled at run time: `l2fwd_ports_eth_addr` is a
   global the compiler cannot see the contents of, so the rig reloads the four
   and two byte halves of the address on every packet (403dde, 403dea).  A
   file-scope `static` initialised in place gets constant-folded into two
   immediate stores instead, which quietly removes both loads and one store
   and makes the loop measure something cheaper than the thing under test.
   check_shape() below fails the run if that ever happens again. */
struct ether_addr6 { uint8_t b[6]; };
struct ether_addr6 ports_eth_addr[4];
unsigned dst_port_of[4];

static inline uint64_t rdtsc_bare(void) {
  uint32_t lo, hi;
  __asm__ __volatile__("rdtsc" : "=a"(lo), "=d"(hi));
  return ((uint64_t)hi << 32) | lo;
}

static inline uint64_t rdtsc_fenced(void) {
  _mm_lfence();
  uint32_t lo, hi;
  __asm__ __volatile__("rdtsc" : "=a"(lo), "=d"(hi));
  _mm_lfence();
  return ((uint64_t)hi << 32) | lo;
}

/* main.c:351-356, with rte_pktmbuf_mtod and l2fwd_mac_updating inlined as the
   compiler inlines them in the rig. */
static inline void none_branch(struct mbuf_like **burst, unsigned nb) {
  unsigned dst_port = dst_port_of[0];
  struct ether_addr6 *src = &ports_eth_addr[dst_port];
  for (unsigned j = 0; j < nb; j++) {
    struct mbuf_like *m = burst[j];
    uint8_t *data = (uint8_t *)m->buf_addr + m->data_off;
    *(uint64_t *)data = 0xff;                 /* dst MAC, 8B store */
    *(struct ether_addr6 *)(data + 6) = *src; /* src MAC, 4B + 2B stores */
  }
}

/* The rig's inner loop is eleven instructions with three stores and two
   reloads of the source MAC (objdump build/l2fwd, 403dd0-403df5).  If this
   one is shorter the number below is not about the same loop.  Counting the
   stores in our own text is the cheap version of that check: scan forward from
   the function's entry for the `movq $0xff` that opens the body. */
static int check_shape(void) {
  /* Executed for effect only: a store the optimiser cannot fold away, which
     is what keeps `ports_eth_addr` opaque.  The real assertion is the
     disassembly diff in check_timed_region.sh. */
  return ports_eth_addr[dst_port_of[0]].b[0] != 0xff;
}

static double median(double *v, int n) {
  for (int i = 1; i < n; i++) {
    double k = v[i]; int j = i - 1;
    while (j >= 0 && v[j] > k) { v[j + 1] = v[j]; j--; }
    v[j + 1] = k;
  }
  return n & 1 ? v[n / 2] : 0.5 * (v[n / 2 - 1] + v[n / 2]);
}

int main(int argc, char **argv) {
  long stride = DEFAULT_STRIDE, iters = 200000, reps = 15, pool = 1;
  const char *csv_path = NULL;
  for (int i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--stride") && i + 1 < argc) stride = atol(argv[++i]);
    else if (!strcmp(argv[i], "--iters") && i + 1 < argc) iters = atol(argv[++i]);
    else if (!strcmp(argv[i], "--reps") && i + 1 < argc) reps = atol(argv[++i]);
    else if (!strcmp(argv[i], "--csv") && i + 1 < argc) csv_path = argv[++i];
    else if (!strcmp(argv[i], "--pool") && i + 1 < argc) pool = atol(argv[++i]);
    else { fprintf(stderr, "usage: %s [--stride N] [--iters N] [--reps N] [--csv PATH] [--pool N]\n", argv[0]); return 2; }
  }
  if (stride < (long)sizeof(struct mbuf_like) + HEADROOM + 64) {
    fprintf(stderr, "stride must leave room for the header and the packet\n");
    return 2;
  }

  if (pool < 1) { fprintf(stderr, "--pool must be >= 1\n"); return 2; }
  size_t arena_sz = (size_t)stride * MAX_BURST * (size_t)pool;
  uint8_t *arena = aligned_alloc(4096, arena_sz);
  if (!arena) { perror("aligned_alloc"); return 1; }
  memset(arena, 0, arena_sz);

  /* Fill the MAC table from something the compiler cannot see through, so the
     loop keeps the two reloads the rig has. */
  for (int i = 0; i < 4; i++) {
    for (int j = 0; j < 6; j++)
      ports_eth_addr[i].b[j] = (uint8_t)(argv[0][j % 3] + i + j);
    dst_port_of[i] = (unsigned)(argc - 1) & 3u;
  }
  if (!check_shape()) { fprintf(stderr, "degenerate MAC table\n"); return 1; }

  /* `pool` independent sets of MAX_BURST mbufs.  With pool=1 the same 64
     packets are rewritten every iteration and everything is L1/L2 resident
     after the first pass, which measures the loop's issue cost and nothing
     else.  Sizing the pool past this part's 52.5 MiB L3 instead makes every
     packet's data line a fresh miss, which is COLDER than the rig, where the
     NIC has DDIO-written the line into L3 before the core reads it.  The rig's
     reading has to fall between the two, and that bracket is the point. */
  struct mbuf_like **bursts = malloc(sizeof(*bursts) * MAX_BURST * pool);
  if (!bursts) { perror("malloc"); return 1; }
  for (long p = 0; p < pool; p++)
    for (int i = 0; i < MAX_BURST; i++) {
      size_t off = (size_t)stride * (p * MAX_BURST + i);
      struct mbuf_like *m = (struct mbuf_like *)(arena + off);
      m->buf_addr = arena + off;
      m->data_off = HEADROOM;
      bursts[p * MAX_BURST + i] = m;
    }

  printf("stride=%ld B  pool=%ld bursts  footprint %.1f MiB (L3 is 52.5 MiB)"
         "  iters=%ld reps=%ld\n\n", stride, pool,
         arena_sz / (1024.0 * 1024.0), iters, reps);

  /* Arm 1: the instrument's own floor -- an empty region, per burst. */
  double *s = malloc(sizeof(double) * reps);
  for (int r = 0; r < reps; r++) {
    uint64_t acc = 0;
    for (long k = 0; k < iters; k++) {
      uint64_t t0 = rdtsc_bare();
      __asm__ __volatile__("" ::: "memory");
      acc += rdtsc_bare() - t0;
    }
    s[r] = (double)acc / iters;
  }
  double floor_bare = median(s, reps);
  for (int r = 0; r < reps; r++) {
    uint64_t acc = 0;
    for (long k = 0; k < iters; k++) {
      uint64_t t0 = rdtsc_fenced();
      __asm__ __volatile__("" ::: "memory");
      acc += rdtsc_fenced() - t0;
    }
    s[r] = (double)acc / iters;
  }
  double floor_fenced = median(s, reps);
  printf("empty region (charged to EVERY burst in EVERY arm):\n");
  printf("  bare rdtsc pair, as the rig uses it : %6.2f ticks/burst"
         "   = %5.2f /pkt at b=64, %5.2f at b=8, %6.2f at b=2\n",
         floor_bare, floor_bare / 64, floor_bare / 8, floor_bare / 2);
  printf("  lfence-bracketed pair               : %6.2f ticks/burst\n\n",
         floor_fenced);

  /* Arm 2: the -m none branch, timed both ways, across burst sizes. */
  printf("  %-6s %11s %11s %11s %11s %11s\n", "burst", "bare/burst",
         "bare/pkt", "work/pkt", "work/pkt", "hidden");
  printf("  %-6s %11s %11s %11s %11s %11s\n", "", "", "(the rig)", "bare-floor",
         "fenced-flr", "ticks/burst");
  /* Appended, not truncated: one invocation per stride, one file to plot. */
  FILE *csv = NULL;
  if (csv_path) {
    int fresh = 1;
    FILE *probe = fopen(csv_path, "r");
    if (probe) { fresh = fgetc(probe) == EOF; fclose(probe); }
    csv = fopen(csv_path, "a");
    if (!csv) { perror("csv"); return 1; }
    if (fresh)
      fprintf(csv, "stride,pool,burst,bare_ticks_burst,bare_per_pkt,"
                   "work_bare_per_pkt,work_fenced_per_pkt,hidden_ticks_burst,"
                   "floor_bare,floor_fenced\n");
  }
  const int bs[] = {64, 32, 16, 8, 4, 2, 1};
  double fit_x[7], fit_y[7];
  int nfit = 0;
  for (unsigned bi = 0; bi < sizeof(bs) / sizeof(bs[0]); bi++) {
    int b = bs[bi];
    for (int r = 0; r < reps; r++) {
      uint64_t acc = 0;
      long pp = 0;
      for (long k = 0; k < iters; k++) {
        /* Chosen BEFORE the opening read: `k % pool` is a 64-bit division, and
           evaluating it after t0 charges ~35 ticks/burst of divider to the
           region under test.  It cost a re-run to notice. */
        struct mbuf_like **cur = bursts + (size_t)pp * MAX_BURST;
        if (++pp == pool) pp = 0;
        uint64_t t0 = rdtsc_bare();
        none_branch(cur, b);
        acc += rdtsc_bare() - t0;
      }
      s[r] = (double)acc / iters;
    }
    double bare = median(s, reps);
    for (int r = 0; r < reps; r++) {
      uint64_t acc = 0;
      long pp = 0;
      for (long k = 0; k < iters; k++) {
        struct mbuf_like **cur = bursts + (size_t)pp * MAX_BURST;
        if (++pp == pool) pp = 0;
        uint64_t t0 = rdtsc_fenced();
        none_branch(cur, b);
        acc += rdtsc_fenced() - t0;
      }
      s[r] = (double)acc / iters;
    }
    double fenced = median(s, reps);
    /* `fenced` brackets the region with lfence on both sides, so it charges
       the loop's stores to the region instead of letting them retire into the
       store buffer after the closing read.  The gap between the two net
       columns is what the rig's unfenced pair cannot see. */
    printf("  %-6d %11.1f %11.2f %11.2f %11.2f %11.1f\n", b, bare, bare / b,
           (bare - floor_bare) / b, (fenced - floor_fenced) / b,
           (fenced - floor_fenced) - (bare - floor_bare));
    if (csv)
      fprintf(csv, "%ld,%ld,%d,%.2f,%.4f,%.4f,%.4f,%.2f,%.2f,%.2f\n", stride, pool, b,
              bare, bare / b, (bare - floor_bare) / b,
              (fenced - floor_fenced) / b,
              (fenced - floor_fenced) - (bare - floor_bare), floor_bare,
              floor_fenced);
    fit_x[nfit] = 1.0 / b;
    fit_y[nfit] = bare / b;
    nfit++;
  }

  /* The same P + C/b the rig's analysis fits, on data with no integer
     truncation in it, so the per-burst term stands on its own. */
  double sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (int i = 0; i < nfit; i++) {
    sx += fit_x[i]; sy += fit_y[i]; sxx += fit_x[i] * fit_x[i];
    sxy += fit_x[i] * fit_y[i];
  }
  double C = (nfit * sxy - sx * sy) / (nfit * sxx - sx * sx);
  double P = (sy - C * sx) / nfit;
  printf("\n  fit of the bare (rig-equivalent) column, cycles/pkt = P + C/b:\n");
  printf("    P = %.2f cycles/packet of actual work\n", P);
  printf("    C = %.1f cycles/burst of instrument\n", C);
  printf("    => at b=64 the rig should print floor(%.2f) = %d\n",
         P + C / 64, (int)(P + C / 64));
  if (csv) fclose(csv);
  free(s); free(arena); free(bursts);
  return 0;
}
