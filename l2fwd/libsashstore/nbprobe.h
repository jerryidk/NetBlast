#ifndef NBPROBE_H
#define NBPROBE_H

/*
 * Per-burst phase timer for the dramblast reflect path.
 *
 * Built only with meson -Dnbprobe=true (defines NB_PROBE). Without it every
 * macro below is empty and the binary is the shipped one: probe build lives in
 * its own build dir (build-probe/), never build/.
 *
 * Why in-binary and not perf: question is work vs wait PER PHASE PER BURST.
 * perf stat gives whole-run totals, sampling gives per-instruction; neither
 * splits one burst into rx / hash / alloc / find / post / free / mac / tx.
 *
 * Mechanism. Nine boundaries, one read each: rdtsc (wall) and, with -S <K>p,
 * rdpmc of NBP_NEV (8) counters. Phase i = boundary i+1 minus boundary i, so phases
 * partition the burst, no gap, no double count -- same rule as main.c's
 * loop_tsc. Only every K-th poll is read (-S K); rest pay one predictable
 * branch per boundary. Empty polls cancelled, never recorded.
 *
 * Probe cost lands inside the phase after each read. Calibrated at worker
 * start (back-to-back reads, printed as `nbprobe mark cost`) so it can be
 * subtracted per phase, and priced end to end by A/B against build/.
 *
 * rdpmc reads use the perf mmap seqlock every time, never a cached index:
 * harness starts `perf stat -C` mid-run, kernel reschedules counters then, and
 * a cached index would silently read another event.
 */

#include <stdint.h>

enum {
  NBP_B_TOP,   /* before rte_eth_rx_burst                   */
  NBP_B_RX,    /* rx returned, nb_rx > 0                    */
  NBP_B_HASH,  /* flowhash loop done, args[] built          */
  NBP_B_ALLOC, /* results[] allocated (+ -A extra pairs)    */
  NBP_B_FIND,  /* dramblast_find_batch_sync returned        */
  NBP_B_POST,  /* result loop done: inserts, ret[] scatter  */
  NBP_B_FREE,  /* results[] freed                           */
  NBP_B_MAC,   /* MAC write loop done                       */
  NBP_B_TX,    /* tx burst, drop path, stats stores done    */
  NBP_NB
};
#define NBP_NPH (NBP_NB - 1)
/* cycles, stalls_total, stalls_l1d, stalls_l3, st_bound, rfo_hitm, insns,
   br_misp. New events append only: analysis.py and v1 dumps read by index.
   PMU budget/core: 8 GP + 4 fixed. Here cycles + insns may take fixed 1/0,
   other six = 6 GP; harness perf stat cycles,instructions then lands on the
   remaining 2 GP. Full: any further perf stat event multiplexes perf stat
   (probe events pinned, keep their counters). Holds only with
   kernel.nmi_watchdog=0 (as on this rig): watchdog takes fixed cycles counter,
   budget then needs 9 GP of 8 and perf stat multiplexes. */
#define NBP_NEV 8

/* One sampled burst. Deltas as u32: one phase never spans 2^32 ticks. */
struct nbp_rec {
  uint64_t tsc0;                  /* absolute tsc at NBP_B_TOP: time axis */
  uint32_t tsc[NBP_NPH];
  uint32_t ev[NBP_NEV][NBP_NPH];  /* zero when counters off */
  uint16_t nb_rx, fn, found, absent, full, inserts;
  uint32_t pops, reprobes;        /* find loop: pops = buckets loaded */
  uint32_t occ_sum;               /* queue size summed at each pop: software MLP */
  uint32_t ins_steps;             /* insert slots walked (insert_at from hint, or insert_one), this burst */
  uint16_t occ_max, lcore;
};

#ifdef NB_PROBE

struct nbp_lcore {
  int on;                         /* this poll sampled */
  int nev;                        /* 0 or NBP_NEV */
  unsigned lcore;
  uint64_t polls;
  uint64_t t[NBP_NB];
  uint64_t e[NBP_NEV][NBP_NB];
  uint8_t hit[NBP_NB];            /* boundary reached this burst */
  void *pc[NBP_NEV];              /* perf_event_mmap_page per counter */
  struct nbp_rec cur;
  /* ring: last ring_cap sampled bursts, overwrite oldest */
  struct nbp_rec *ring;
  uint64_t ring_n, ring_cap;
  /* whole-run sums over sampled bursts */
  uint64_t n, pkts, fn, found, absent, full, inserts, pops, reprobes, occ_sum,
      ins_steps, occ_max;
  uint64_t sum_tsc[NBP_NPH];
  uint64_t sum_ev[NBP_NEV][NBP_NPH];
  /* calibration: min cost of one boundary read */
  uint64_t cal_tsc, cal_ev[NBP_NEV];
};

extern int nbp_every;             /* -S K: sample every K-th poll, 0 = off */
extern int nbp_pmc;               /* -S Kp: also read counters */
extern const char *nbp_dump;      /* -D prefix: ring dump path prefix */
extern const char *nbp_ev_name[NBP_NEV];
extern __thread struct nbp_lcore *nbp;

void nbp_worker_init(unsigned lcore);   /* worker start: pmcs, ring, calibrate */
void nbp_mark_slow(int b);
void nbp_finish(void);                  /* at NBP_B_TX: deltas -> ring, sums */
void nbp_report(void);                  /* main lcore, after workers joined */

static inline void nbp_top(void) {
  if (nbp && nbp_every && ++nbp->polls % (uint64_t)nbp_every == 0) {
    nbp->on = 1;
    nbp_mark_slow(NBP_B_TOP);
  }
}
#define NBP_TOP() nbp_top()
#define NBP_MARK(b) do { if (__builtin_expect(nbp && nbp->on, 0)) nbp_mark_slow(b); } while (0)
#define NBP_CANCEL() do { if (nbp) nbp->on = 0; } while (0)
#define NBP_FINISH() do { if (__builtin_expect(nbp && nbp->on, 0)) nbp_finish(); } while (0)
/* counts: written every burst in probe build (register adds), kept only when sampled */
#define NBP_SET(field, val) do { if (nbp) nbp->cur.field = (val); } while (0)
#define NBP_ADD(field, val) do { if (nbp) nbp->cur.field += (val); } while (0)

#else

#define NBP_TOP() do { } while (0)
#define NBP_MARK(b) do { } while (0)
#define NBP_CANCEL() do { } while (0)
#define NBP_FINISH() do { } while (0)
#define NBP_SET(field, val) do { } while (0)
#define NBP_ADD(field, val) do { } while (0)

#endif /* NB_PROBE */

/*
 * Per-packet marks for Intel PT (meson -Dnbptw=true, NB_PTW). ptwrite payload
 * = kind<<56 | id<<40 | low 40 bits of arg. Decoded by `analysis.py ptw`.
 * Only way to get push->pop time per packet without rdtsc per push and pop,
 * which would double the find loop it is measuring. Validated on
 * bench_membw ptmark: count, order, timing to 0.1% of program clock.
 */
enum {
  NBW_BURST = 1,   /* arg = poll counter */
  NBW_PUSH,        /* id, arg = bucket idx */
  NBW_FOUND,       /* id, arg = visit_count */
  NBW_ABSENT,
  NBW_REPROBE,
  NBW_FULL,
  NBW_PHASE,       /* arg = NBP_B_* boundary */
};
#ifdef NB_PTW
#define NBW(kind, id, arg)                                                     \
  __asm__ volatile("ptwrite %0" ::"r"(((uint64_t)(kind) << 56) |               \
                                      ((uint64_t)((id) & 0xffff) << 40) |      \
                                      ((uint64_t)(arg) & 0xffffffffffULL)))
#else
#define NBW(kind, id, arg) do { } while (0)
#endif

#endif /* NBPROBE_H */
