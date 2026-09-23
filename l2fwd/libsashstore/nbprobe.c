/* Per-burst phase timer. See nbprobe.h for what and why. */
#ifdef NB_PROBE

#include "nbprobe.h"

#include <linux/perf_event.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <x86intrin.h>

int nbp_every = 0;
int nbp_pmc = 0;
const char *nbp_dump = NULL;
__thread struct nbp_lcore *nbp = NULL;

#define NBP_MAX_LCORE 128
static struct nbp_lcore *nbp_all[NBP_MAX_LCORE];

/* Raw encodings, same numbers perf 7.2 lists for this part (family 6 model
   207). Validated against bench_membw known answers, docs/REFLECT_PATH.md
   section 0. Order fixed: analysis.py probe reads by index. */
const char *nbp_ev_name[NBP_NEV] = {"cycles",   "stall_total", "stall_l1d",
                                    "stall_l3", "st_bound",    "rfo_hitm"};
static const struct {
  uint32_t type;
  uint64_t config, config1;
} nbp_ev[NBP_NEV] = {
    {PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES, 0},
    {PERF_TYPE_RAW, 0x04a3 | (4ULL << 24), 0},  /* cycle_activity.stalls_total   */
    {PERF_TYPE_RAW, 0x0ca3 | (12ULL << 24), 0}, /* cycle_activity.stalls_l1d_miss */
    {PERF_TYPE_RAW, 0x06a3 | (6ULL << 24), 0},  /* cycle_activity.stalls_l3_miss  */
    {PERF_TYPE_RAW, 0x40a6 | (2ULL << 24), 0},  /* exe_activity.bound_on_stores   */
    {PERF_TYPE_RAW, 0x012a, 0x10003C0002ULL},   /* ocr.demand_rfo.l3_hit.snoop_hitm */
};

/* Self-monitoring read, per perf_event_mmap_page protocol. Seqlock every read:
   index and offset change when counters are rescheduled. */
static inline uint64_t nbp_rdpmc(void *p) {
  volatile struct perf_event_mmap_page *pc = p;
  uint32_t seq, idx;
  uint64_t cnt;
  do {
    seq = pc->lock;
    __asm__ volatile("" ::: "memory");
    idx = pc->index;
    cnt = pc->offset;
    if (idx) {
      uint64_t v = __rdpmc(idx - 1);
      int w = pc->pmc_width;
      v <<= 64 - w;
      cnt += (uint64_t)((int64_t)v >> (64 - w));
    }
    __asm__ volatile("" ::: "memory");
  } while (pc->lock != seq);
  return cnt;
}

static inline void nbp_read(struct nbp_lcore *s, int b) {
  s->t[b] = __rdtsc();
  for (int i = 0; i < s->nev; i++) s->e[i][b] = nbp_rdpmc(s->pc[i]);
  s->hit[b] = 1;
}

void nbp_mark_slow(int b) {
  struct nbp_lcore *s = nbp;
  if (b == NBP_B_TOP) {
    memset(s->hit, 0, sizeof(s->hit));
    memset(&s->cur, 0, sizeof(s->cur));
  }
  nbp_read(s, b);
}

static int nbp_open(unsigned lcore, int i) {
  struct perf_event_attr a;
  memset(&a, 0, sizeof(a));
  a.size = sizeof(a);
  a.type = nbp_ev[i].type;
  a.config = nbp_ev[i].config;
  a.config1 = nbp_ev[i].config1;
  a.pinned = 1;          /* never multiplexed: a scaled count is not a count */
  a.exclude_kernel = 1;  /* PMD is user space; kernel time is not the path  */
  a.exclude_hv = 1;
  int fd = syscall(SYS_perf_event_open, &a, 0, -1, -1, 0);
  if (fd < 0) {
    fprintf(stderr, "nbprobe: lcore %u perf_event_open %s failed\n", lcore,
            nbp_ev_name[i]);
    exit(1);
  }
  return fd;
}

void nbp_worker_init(unsigned lcore) {
  if (!nbp_every) return;
  struct nbp_lcore *s = calloc(1, sizeof(*s));
  if (!s || lcore >= NBP_MAX_LCORE) { fprintf(stderr, "nbprobe: init failed\n"); exit(1); }
  s->lcore = lcore;

  if (nbp_pmc) {
    s->nev = NBP_NEV;
    for (int i = 0; i < NBP_NEV; i++) {
      int fd = nbp_open(lcore, i);
      s->pc[i] = mmap(NULL, sysconf(_SC_PAGESIZE), PROT_READ, MAP_SHARED, fd, 0);
      if (s->pc[i] == MAP_FAILED) { perror("nbprobe mmap"); exit(1); }
      volatile struct perf_event_mmap_page *pc = s->pc[i];
      /* index 0 = not on a counter (pinned event failed to schedule). Fail
         loudly: a counter that reads 0 looks like a real zero. */
      if (!pc->cap_user_rdpmc || pc->index == 0) {
        fprintf(stderr, "nbprobe: lcore %u %s not readable by rdpmc (cap %u idx %u)\n",
                lcore, nbp_ev_name[i], (unsigned)pc->cap_user_rdpmc, pc->index);
        exit(1);
      }
    }
  }

  /* Ring: 1<<18 records x 264 B = 66 MiB per lcore. MADV_HUGEPAGE + prefault
     so ring stores do not add 4 KiB TLB misses to the path being timed. */
  s->ring_cap = 1u << 18;
  size_t bytes = s->ring_cap * sizeof(struct nbp_rec);
  s->ring = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (s->ring == MAP_FAILED) { perror("nbprobe ring"); exit(1); }
  madvise(s->ring, bytes, MADV_HUGEPAGE);
  memset(s->ring, 0, bytes);

  /* Calibrate: min cost of one boundary read, over 4096 back-to-back pairs.
     Min, not mean: interrupts only add. */
  s->cal_tsc = ~0ULL;
  for (int i = 0; i < NBP_NEV; i++) s->cal_ev[i] = ~0ULL;
  for (int r = 0; r < 4096; r++) {
    nbp_read(s, 0);
    nbp_read(s, 1);
    uint64_t d = s->t[1] - s->t[0];
    if (d < s->cal_tsc) s->cal_tsc = d;
    for (int i = 0; i < s->nev; i++) {
      uint64_t de = s->e[i][1] - s->e[i][0];
      if (de < s->cal_ev[i]) s->cal_ev[i] = de;
    }
  }
  nbp_all[lcore] = s;
  nbp = s;
}

void nbp_finish(void) {
  struct nbp_lcore *s = nbp;
  s->on = 0;
  /* Boundary not reached (e.g. -m none has no alloc/find/free) = zero-length
     phase: copy previous boundary. */
  for (int b = 1; b < NBP_NB; b++)
    if (!s->hit[b]) {
      s->t[b] = s->t[b - 1];
      for (int i = 0; i < s->nev; i++) s->e[i][b] = s->e[i][b - 1];
    }
  struct nbp_rec *r = &s->cur;
  r->tsc0 = s->t[0];
  r->lcore = s->lcore;
  for (int p = 0; p < NBP_NPH; p++) {
    r->tsc[p] = (uint32_t)(s->t[p + 1] - s->t[p]);
    s->sum_tsc[p] += r->tsc[p];
    for (int i = 0; i < s->nev; i++) {
      r->ev[i][p] = (uint32_t)(s->e[i][p + 1] - s->e[i][p]);
      s->sum_ev[i][p] += r->ev[i][p];
    }
  }
  s->n++;
  s->pkts += r->nb_rx;
  s->fn += r->fn;
  s->found += r->found;
  s->absent += r->absent;
  s->full += r->full;
  s->inserts += r->inserts;
  s->pops += r->pops;
  s->reprobes += r->reprobes;
  s->occ_sum += r->occ_sum;
  s->ins_steps += r->ins_steps;
  if (r->occ_max > s->occ_max) s->occ_max = r->occ_max;
  s->ring[s->ring_n++ & (s->ring_cap - 1)] = *r;
}

static const char *nbp_phase_name[NBP_NPH] = {"rx",   "hash", "alloc", "find",
                                              "post", "free", "mac",   "tx"};

static void nbp_print(const char *who, struct nbp_lcore *s) {
  if (!s->n || !s->pkts) {
    printf("nbprobe %s sampled=0\n", who);
    return;
  }
  double pk = (double)s->pkts;
  printf("nbprobe %s sampled=%lu pkts=%lu every=%d pmc=%d burst=%.2f\n", who, s->n,
         s->pkts, nbp_every, s->nev ? 1 : 0, pk / s->n);
  for (int p = 0; p < NBP_NPH; p++) {
    printf("nbprobe %s phase=%s tsc_pkt=%.2f", who, nbp_phase_name[p], s->sum_tsc[p] / pk);
    for (int i = 0; i < s->nev; i++) printf(" %s_pkt=%.2f", nbp_ev_name[i], s->sum_ev[i][p] / pk);
    printf("\n");
  }
  double fn = s->fn ? (double)s->fn : 1;
  printf("nbprobe %s counts keys=%lu found=%.4f absent=%.4f full=%.4f "
         "pops_per_key=%.4f reprobes_per_key=%.4f occ_mean=%.2f occ_max=%lu "
         "inserts_per_key=%.4f ins_steps_per_insert=%.2f\n",
         who, s->fn, s->found / fn, s->absent / fn, s->full / fn, s->pops / fn,
         s->reprobes / fn, s->pops ? (double)s->occ_sum / s->pops : 0, s->occ_max,
         s->inserts / fn, s->inserts ? (double)s->ins_steps / s->inserts : 0);
}

static void nbp_dump_ring(struct nbp_lcore *s) {
  char path[4096];
  snprintf(path, sizeof(path), "%s_l%u.nbp", nbp_dump, s->lcore);
  FILE *f = fopen(path, "wb");
  if (!f) { perror(path); return; }
  /* header: magic, version, rec size, nev, every, n in file, cal */
  uint64_t n = s->ring_n < s->ring_cap ? s->ring_n : s->ring_cap;
  uint64_t start = s->ring_n - n;
  uint64_t hdr[8] = {0x3170726f6270626eULL /* "nbprobp1" */, 1, sizeof(struct nbp_rec),
                     (uint64_t)s->nev, (uint64_t)nbp_every, n, s->cal_tsc, s->lcore};
  fwrite(hdr, sizeof(hdr), 1, f);
  fwrite(s->cal_ev, sizeof(s->cal_ev), 1, f);
  for (uint64_t k = 0; k < n; k++)
    fwrite(&s->ring[(start + k) & (s->ring_cap - 1)], sizeof(struct nbp_rec), 1, f);
  fclose(f);
  printf("nbprobe dump %s records=%lu\n", path, n);
}

void nbp_report(void) {
  if (!nbp_every) return;
  struct nbp_lcore all;
  memset(&all, 0, sizeof(all));
  for (unsigned l = 0; l < NBP_MAX_LCORE; l++) {
    struct nbp_lcore *s = nbp_all[l];
    if (!s) continue;
    char who[32];
    snprintf(who, sizeof(who), "lcore=%u", l);
    printf("nbprobe %s mark_cost tsc=%lu", who, s->cal_tsc);
    for (int i = 0; i < s->nev; i++) printf(" %s=%lu", nbp_ev_name[i], s->cal_ev[i]);
    printf("\n");
    nbp_print(who, s);
    if (nbp_dump) nbp_dump_ring(s);
    all.nev = s->nev;
    all.n += s->n; all.pkts += s->pkts; all.fn += s->fn; all.found += s->found;
    all.absent += s->absent; all.full += s->full; all.inserts += s->inserts;
    all.pops += s->pops; all.reprobes += s->reprobes; all.occ_sum += s->occ_sum;
    all.ins_steps += s->ins_steps;
    if (s->occ_max > all.occ_max) all.occ_max = s->occ_max;
    for (int p = 0; p < NBP_NPH; p++) {
      all.sum_tsc[p] += s->sum_tsc[p];
      for (int i = 0; i < s->nev; i++) all.sum_ev[i][p] += s->sum_ev[i][p];
    }
  }
  nbp_print("lcore=all", &all);
}

#endif /* NB_PROBE */
