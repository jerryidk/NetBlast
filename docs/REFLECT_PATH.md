# dramblast reflect path: critical path, work vs wait, by queue count and load factor

Branch `new-async`. Groundwork for async submit/complete layer. Nothing here changes the
sync path's behaviour. Everything measured on sync path as shipped, plus instruments.

Order of this file = order of work: §0 instruments proven, §1 static inventory, §2
predictions written BEFORE data, §3 results, §4 what it means for async, §5 findings
found on the way.

---

## 0. Instruments, and the known answer each one passed

Tools shell: `nix develop .#profile` (flake input `tools` = nixos-26.05, tools only; DPDK
and gcc stay on mars-std so binary under test unchanged). perf 7.2.6, pcm 202604, pahole
1.31, llvm-mca 21, bpftrace 0.25, xed, likwid 5.5.1, babeltrace2. Kernel 5.15 PMU driver
reports `Sapphire Rapids events, 32-deep LBR, PEBS fmt4+`; 8 GP + 4 fixed counters.

New perf knows this part's event names; mars-std perf 5.17 did not (why harness uses raw
encodings). Name = new instrument, so each validated against `bench_membw` workload with
computable answer. New modes added to `l2fwd/bench_membw.c`: `pingpong`, `fshare`,
`padded`, `ptmark`. Build line now `cc -O2 -pthread`.

| instrument | known answer | result | verdict |
|---|---|---|---|
| PEBS load latency (`perf mem`, `weight`) | dependent chase: exact cyc/load = perf-stat cycles / loads | 8 MiB 59.8 vs 61.3; 64 MiB 107.8 vs 116.4; 512 MiB 200.8 vs 223.6 | PASS within 10%, always low. Gap grows with buffer = page-walk time outside weight. Table is on 1 GiB pages, so bias small there |
| PEBS `ins_lat` | same | 63.2 / 110.3 / 208.9 | PASS within 7% |
| `ocr.demand_data_rd.l3_hit.snoop_hitm` | pingpong: 1 line transfer per handoff | **1.0001** per handoff, cores 24/26 and 24/25 | PASS exact |
| `ocr.demand_rfo.l3_hit.snoop_hitm` | fshare >> padded | 1.64M vs 9 per second | PASS pattern |
| `mem_load_l3_hit_retired.xsnp_*` (the unapplied coherence patch) | pingpong: ~1 FWD per handoff | FWD 0.035 / 0.33, NO_FWD 0.47 / 0.78; depends on core pair | **FAIL**. Retired-load events: squashed spin loads (machine clears 1.1-1.9 per handoff) lose snoop attribution. Raw encodings = perf names, so not an encoding bug. Do not use for counts |
| `perf c2c` | pingpong line top-ranked; fshare line found | pingpong: line #0, 66 local HITM. fshare: **not found** | PASS load side; BLIND to store-side sharing (fshare loads forward from store buffer, only RFOs contend). c2c alone cannot clear false sharing |
| Intel PT + ptwrite (`intel_pt/ptw=1,cyc=1,branch=0/u`) | ptmark n=200000: n PTW, values 0..n-1 in order, ns/mark = program clock | 200000/200000, in order, no dups; 1345.1 vs 1345.0 ns (64 MiB), 1596.1 vs 1595.0 (512 MiB) | PASS to 0.1% |
| `cycle_activity.stalls_l1d_miss` | nomem 0; L3-resident chase high | 0.000; 0.862 per cycle | PASS |
| `cycle_activity.stalls_l3_miss` | L3-resident chase 0; 4 GiB chase high | 0.000; 0.565 | PASS |
| `cycle_activity.stalls_total` | — | nomem 0.427 | NOT "idle": counts cycles no uop executes, incl. ALU dependency latency. Use `stalls_total - stalls_l1d` as non-memory stall |
| `exe_activity.bound_on_stores` | fshare >> padded? | fshare 0.221, padded **0.406** | FAIL as sharing signal. Store-buffer-full only |

Multiplexing trap found: OCR events share two offcore-response MSRs. Four OCR events in
one `perf stat` multiplexed and read 1.54 per handoff where truth is 1.0001. At most two
OCR events at once.

### 0.1 In-binary phase timer (`libsashstore/nbprobe.{h,c}`)

Why: question is work vs wait per phase per burst. perf stat = whole-run totals;
sampling = per instruction. Neither splits one burst into its phases.

- Build: `meson setup build-probe -Dnbprobe=true`. `build/` unchanged: shipped hot
  functions (`l2fwd_main_loop`, `dramblast_find_batch_sync`, `dramblast_process_frames`,
  `dramblast_insert_one`, `flowhash`) instruction-identical to pre-change binary, modulo
  RIP-relative data offsets. Shipped binary refuses `-S`/`-D` (error, not ignore).
  (Since §7: `flowhash` no longer own symbol, `static inline` in `packettool.h`, inlined
  into `l2fwd_main_loop` / `maglev_process_frame`.)
- Nine boundaries → eight phases partitioning one burst: `rx` (rte_eth_rx_burst), `hash`
  (flowhash loop), `alloc` (aligned_alloc + `-A` pairs), `find`
  (dramblast_find_batch_sync), `post` (result loop: inserts, ret[] scatter), `free`,
  `mac` (MAC write loop), `tx` (tx burst, drop path, stats stores).
- `-S K`: every K-th poll, rdtsc per boundary. `-S Kp`: plus rdpmc of six counters:
  cycles, stall_total, stall_l1d, stall_l3, st_bound, rfo_hitm. Pinned events, never
  multiplexed. rdpmc through perf mmap seqlock every read: harness starts `perf stat -C`
  mid-run, kernel reschedules counters, cached index would read wrong counter.
- Counts per sampled burst: nb_rx, keys, found/absent/full, pops (buckets loaded),
  reprobes, queue occupancy summed at each pop (software MLP), insert slots walked.
- `-D PREFIX`: ring of last 2^18 sampled bursts per lcore → `PREFIX_l<lcore>.nbp`. Ring =
  steady state (~16 s at K=16). Log sums = whole run incl. insert warm-up.
- Mark cost, calibrated at worker start (min of 4096 back-to-back): **24 ticks** tsc-only,
  **106 ticks** with six PMCs (~14 per rdpmc). Each phase opens with one mark, so
  `analysis.py probe` subtracts one mark per reached phase per burst; raw kept alongside.
- Partition check, `-m none` on `net_null` (no NIC): phase sum **68.13** ticks/pkt vs
  main.c `Full-loop cyc per fwd packet: 68`. Phases account for the loop.
- **Probe v1 defect, caught by the probe-cost block, fixed.** v1 counted found/absent/full
  with `NBP_ADD` per packet: read-modify-write of one TLS word every packet of every
  burst whenever `-S` set, a store→load forwarding chain inside `post`. q=5 ladder
  (loop ticks/pkt): shipped 121, hooks compiled but `-S` off 127, `-S 16` 134, `-S 16p`
  137. Expected sampling cost ≈ 9 marks x 24 ticks / 16 bursts / 64 pkts = 0.2, measured
  7: that gap was the defect, and it sat in the one phase (`post`) that insert analysis
  reads. v2 counts in locals, one store per burst. v1 runs kept in
  `sweeps/reflect/probe_v1/`, never mixed with v2. At q=6 v1 cost looked ~0 because q=6
  is unsaturated (batch 43-57): slack absorbs per-burst work as smaller bursts, not
  ticks. **Price probe cost at saturated q (≤5), never at q=6.**

Wait taxonomy used below, from the known answers above:

    work        = cycles - stall_total        some uop executed
    mem_wait    = stall_l1d                   no uop executed, L1D miss pending
    dram_wait   = stall_l3   (subset of mem_wait)
    other_stall = stall_total - stall_l1d     dependency latency, frontend
    rfo_hitm    = count, not cycles: RFOs served from a modified line in another core

Per-packet PT marks: `meson setup build-ptw -Dnbprobe=true -Dnbptw=true`. ptwrite payload
= kind<<56 | id<<40 | arg (push / found / absent / reprobe / full / phase / burst). Gives
push→pop time per packet without rdtsc per push/pop.

### 0.2 Load factor knob (`-P ALPHA`)

Occupancy = load factor. Generator flow count moves load factor AND touched working set
together, so it cannot vary one alone. `-P` prefills with filler keys (xorshift64*, fixed
seeds) on all lcores before forwarding; traffic stays fixed. `dramblast table
{prefill,exit}` line measures, not assumes: alpha, displacement histogram, hit and miss
bucket counts. Prefill 0.9 x 2^29: 5.2 s at 7 lcores, 17.4 s at 2. Harness
`SAMPLE_AFTER` waits for `Link UP` so perf samples forwarding, not init.

---

## 1. Static inventory: what the sync path touches, holds, allocates

### 1.1 Critical path, per burst (one lcore = one RX/TX queue pair, reflect: TX on same port+queue)

    rte_eth_rx_burst ──► hash loop ──► alloc ──► find_batch_sync ──► post loop ──► free ──► MAC loop ──► rte_eth_tx_burst
      (PMD)             all nb_rx      1 KiB     fill, then pop       ABSENT →        glibc    all fn        (PMD)
                        before any     glibc     until DRAINED        insert_one      tcache   packets
                        lookup                   (barrier)            (serial)

Every arrow = full barrier over the burst. No stage overlaps the next, and no burst
overlaps the next. `find` returns only when every key resolved, so the prefetch pipeline
ramps from 0 and drains to 0 inside every burst (§5.14's ramp cost).

### 1.2 Per batch (inside find_batch_sync)

    fill:  while queue not full and args left:  crc32(k) → prefetcht2(bucket) → push{k, idx, visit=0, id}
    pop:   pop tail → 512-bit load of bucket (4 slots) → cmpeq key lanes
             hit              → result{v, id, FOUND}
             empty lane seen  → result{0, id, ABSENT}
             neither          → idx += 4, prefetcht2, push again (reprobe)
             visit ≥ len      → result{0, id, TABLE_FULL}

Queue capacity `Q-1` (63). Burst ≤ 64, so first fill takes 63 keys, first pop frees
room for the 64th, then only reprobes are pushed: fill-once-then-drain per burst. Mean
occupancy at pop ≈ half the burst (31.7 measured at B=64, smoke run), falling to 0 at
the tail of every burst. Emitted
instruction is `prefetcht2` (hint table inverted, §5.26 / `opt/dramblast-prefetch-hints.patch`, unapplied).

### 1.3 Per packet

1 flowhash (13 bytes FNV-1, byte-serial imul chain), 1 crc32, ≥1 prefetch, ≥1 push + pop
(32 B queue item each way), ≥1 bucket load (64 B, table), 1 result write (16 B, completion
order), 1 scatter `ret[id]`, 1 MAC write (8 B dst + 6 B src into packet line). Miss adds:
rehash + scalar insert walk + `lock cmpxchg16b` + `% 65537` (64-bit div) into
`dramblast_backends`.

### 1.4 Data structures on the path

| object | where | size | lifetime | written by |
|---|---|---|---|---|
| `pkts_burst[64]` | worker stack | 512 B | one poll | PMD |
| `frames[64]`, `mac_addrs[64]` | worker stack | 512 B each | one poll | main loop |
| `args[64]` `{k, id}` | worker stack | 16 B each, 1 KiB | one poll | hash loop |
| `results[fn]` `{v, id, status}` | glibc heap, 64 B aligned | 16 B each | one call | find loop, completion order |
| find queue items `{k, idx, visit_count, id}` | heap, per lcore | 32 B (4 B pad) x Q | process | find loop |
| `dramblast_queue_t queues[128]` `{ptr, head, tail, size}` | heap, dense array indexed by lcore_id | **24 B, unpadded** | process | every push and pop |
| `dramblast_ht_t` | heap | 24 B | process | init only |
| table `dramblast_kv_t[2^29]` | 1 GiB hugetlb | 16 B slot, 64 B bucket, 8 GiB | process | insert_one (CAS) |
| `dramblast_backends` | static | 8 B x 65537 = 512 KiB | process | init only |
| `rte_mbuf` | mempool, per-lcore cache 256 | 128 B, 2 lines | RX → TX completion | PMD, main loop (MAC) |
| `port_statistics[port][lcore]` | static, cache aligned | 128 B | process | every poll |

### 1.5 Identifiers

- `args[j].id` = dense index 0..fn-1 in this burst. Same index keys `frames[]` and
  `mac_addrs[]`. Burst-local: means nothing after the poll returns.
- queue item carries `id` + `k` + bucket `idx` + `visit_count` (slots probed, +4 per
  bucket). Only state a lookup has.
- `results[]` filled in **completion order**, not submission order; post loop scatters by
  `id`. Sync path already completes out of order inside a burst.
- key `k` = 64-bit FNV flowhash; `k == 0` = unhashable frame (skipped) AND empty slot.
- home bucket `idx` = `crc32(k) & (len-1) & ~3`: 32 bits of hash, caps table at 2^32.

Async consequence: burst-local `id` + stack arrays = nothing can outlive the poll. An
async layer needs a completion id valid across polls, and mbuf ownership held across
polls.

### 1.6 Allocations per burst

- 1 `aligned_alloc(64, 16·fn rounded to 64)` + 1 `free` (glibc tcache) with `-A 0`.
  `-A -1` hoists to per-lcore buffer, `-A n` adds n pairs.
- mbufs: PMD RX refill takes nb_rx from mempool; TX completion returns completed mbufs.
- Nothing else. insert_one allocates nothing.

---

## 2. Predictions, written before the block data

### 2.1 False sharing in `queues[lcore_id]` (P3 in dramblast_analysis.md)

24 B stride, workers on even lcores 2..2q, array 64 B aligned:

| line | bytes | who |
|---|---|---|
| 0 | 0-63 | lcore 2 ptr/head/tail (48-63) |
| 1 | 64-127 | lcore 2 `find_queue_size` (read every push) + lcore 4 head/tail (written) |
| 2 | 128-191 | lcore 6 alone |
| 3 | 192-255 | lcore 8 head/tail AND lcore 10 head/tail: write-write |
| 4 | 256-319 | lcore 10 `size` (read) + lcore 12 head/tail (written) |

Prediction: rfo_hitm / load HITM in `find` on lcores {2,4} from q≥2, {8,10} from q≥5,
{10,12} at q=6; **never on lcore 6**. If sharing costs anything, `find` ticks per packet
on those lcores exceed lcore 6's at equal burst size.

### 2.2 Load factor

From `dramblast table` at prefill: α=0.5 → hit loads 1.05 buckets, miss 1.23. α=0.9 → hit
2.00, miss 13.2, worst miss 476-570. Traffic keys land after filler, so their displacement
is worse than the table mean at the same α.

Prediction: steady state is ~all hits (absent < 1%), so `find` cost follows hit
buckets: flat to α≈0.5, then rising with reprobes per key. `post` rises only by
insert-walk cost of the few misses. Other phases do not move with α.

---

## 3. Results

Data: `/users/sohamb/sweeps/reflect/`. Blocks `pc_*` (probe cost, α≈0.016 = traffic
only) and `al_*` (load factor). q order 6→1, arms alternate within each q. All ticks =
TSC ticks (2.1 GHz invariant; core pinned 2.09 GHz, so ticks ≈ cycles here, checked:
probe `cycles_pkt` within 1% of `tsc_pkt` in every phase).

### 3.1 Probe cost (loop ticks/pkt, main.c `Full-loop cyc per fwd packet`)

| q | shipped | hooks, `-S` off | `-S 16` | `-S 16p` |
|---|---|---|---|---|
| 6 | 135 | 139 | 136 | 137 |
| 5 | 124 | 128 | 134 | 132 |
| 4 | 120 | 126 | 127 | 130 |
| 3 | 120 | 128 | 127 | 128 |
| 2 | 121 | 129 | 128 | 129 |
| 1 | 117 | 124 | 124 | 125 |

Hooks compiled in: +4 to +8 at every q (single-lcore q=1 too, so not contention). Sampling
and six PMC reads on top: 0-3, within run-to-run noise (shipped q=5 read 121 and 124 in
two passes). Probe numbers below carry ~+6 ticks/pkt not present in shipped build; at
α=0.9 the gap grows to 34 (524 vs 490), i.e. roughly proportional to lookup cost.
Per-phase attribution also shifted ±3-4 ticks between probe v1 and v2 at equal totals:
**quote phases ±3**.

### 3.2 Where one packet's time goes, α≈0.016 (ring, `-S 16p`, mark cost subtracted)

| q | rx | hash | alloc | find | post | free | mac | tx | sum | loop |
|---|---|---|---|---|---|---|---|---|---|---|
| 6 | 16.5 | 49.3 | 8.8 | 33.1 | 4.4 | 2.5 | 5.7 | 21.1 | 141 | 137 |
| 5 | 14.6 | 49.2 | 7.9 | 33.0 | 3.6 | 2.0 | 4.9 | 21.0 | 136 | 132 |
| 4 | 14.7 | 49.1 | 7.9 | 28.3 | 4.3 | 2.3 | 5.9 | 21.1 | 134 | 130 |
| 3 | 15.0 | 49.1 | 7.9 | 28.7 | 3.6 | 2.0 | 4.7 | 20.3 | 131 | 128 |
| 2 | 14.9 | 49.3 | 7.8 | 29.3 | 3.6 | 2.0 | 4.8 | 20.8 | 133 | 129 |
| 1 | 14.3 | 49.1 | 7.8 | 26.0 | 3.5 | 1.9 | 4.8 | 20.3 | 128 | 125 |

(sum over sampled bursts > loop mean over all bursts by the sampled bursts' own probe
cost, ~3-4.)

- **hash is the largest phase, 49 ticks/pkt, flat in q.** Work 41.5, mem_wait 4.8 (first
  touch of the packet header line). FNV-1 byte-serial imul chain. Bigger than the lookup.
- **find ≈ 26-33, mostly work.** mem_wait 1.2-3.5 of it. Every key hits its home bucket
  (pops/key 1.000); mean occupancy at pop 32.5 = half the burst. At α≈0 the 64-deep
  prefetch pipeline hides the DRAM latency; the lookup is compute + queue bookkeeping.
- **alloc + free ≈ 10 ticks/pkt** = ~640 ticks/burst: the aligned_alloc/free pair.
  Matches §5.13's per-burst C in size.
- rx 14-16, tx 20-21 (PMD). mac 5. post 3.5-4.4.

### 3.3 False sharing in `queues[lcore_id]`: prediction §2.1 holds

find ticks/pkt per lcore, `-S 16p`, α≈0.016:

| q | l2 | l4 | l6 | l8 | l10 | l12 |
|---|---|---|---|---|---|---|
| 6 | 29.4 | 31.1 | 31.2 | **43.8** | **42.5** | 32.5 |
| 5 | 30.0 | 31.0 | 28.7 | **42.0** | **42.9** | |
| 4 | 30.4 | 31.7 | 29.2 | 29.2 | | |
| 3 | 30.5 | 31.5 | 29.4 | | | |
| 2 | 30.6 | 31.3 | | | | |
| 1 | 27.9 | | | | | |

- **Write-write line 3 (lcores 8 + 10 head/tail): +13 ticks/pkt in find, only once lcore 10
  exists (q≥5).** lcore 8 alone at q=4: 29.2. rfo_hitm 0.08-0.09/pkt on exactly lcores 8
  and 10, 0.00 on every other lcore at every q: ~5 stolen lines per burst, ~170 ticks each.
- Read-write lines (lcore 2's / 10's `size` read, neighbour writes): mem_wait up (lcores 2,
  4: 3.0-3.9 from q≥2, vs 1.8-2.0 unshared lcore 6 and 1.2 for lcore 2 alone at q=1) but
  time +1-2 only. lcore 12 unaffected.
- lcore 6: never affected, as predicted.
- `perf c2c` could not have found the write-write case (§0: blind to store-side sharing).
  The probe's per-lcore rfo_hitm did, at the predicted lcores and q.

Cost: ~10% of lcores 8/10's loop from q≥5. Fix is one attribute (`__rte_cache_aligned`
on `dramblast_queue_t`), not applied here: separate arm.

### 3.4 Load factor

Shipped binary, no probe (Mpps / loop ticks per pkt):

| q | α≈0.016 | α=0.5 | α=0.9 |
|---|---|---|---|
| 6 | 93.2 / 135 | 89.6 / 140 | 25.7 / 490 |
| 5 | 84.7 / 124 | 74.7 / 140 | 21.6 / 486 |
| 4 | 69.7 / 120 | 60.3 / 139 | 20.4 / 412 |
| 3 | 52.1 / 120 | 44.9 / 140 | 15.3 / 412 |
| 2 | 34.5 / 121 | 29.7 / 141 | 10.1 / 415 |
| 1 | 17.8 / 117 | 15.2 / 137 | 5.0 / 419 |

Probe, find phase (ticks/pkt) and lookup counts; same at every q within ±3 except where noted:

| α measured | pops/key | find | work | mem_wait | other_stall | occ at pop | find p99 |
|---|---|---|---|---|---|---|---|
| 0.016 | 1.000 | 26-34 | 21 | 1-4 | 4-9 | 32.5 | 37-77 |
| 0.266 | 1.022 | 29-37 | 23 | 2-4 | 5-10 | 32.5 | 42-83 |
| 0.516 | 1.243 | 47-51 | 33 | 5-7 | 9-10 | 31.7 | 67-91 |
| 0.766 | 2.81 | 109-118 | 81 | 8-10 | 20-26 | 26.9 | 138-185 |
| 0.916 | 15.4 | 324-329 (q≤4), 407-437 (q≥5) | 264-272 | 32-36 | 28 (q≤4), 98-128 (q≥5) | 22 | 461-1058 |

- **Only find moves.** hash, alloc, post, free, mac, rx, tx flat within ±3 from α 0.016 to
  0.9. post stays 3.5-5.6: steady state is ~all hits (absent 0.3% over whole run = the
  8.45M first sightings; 0.0% in the steady ring). Prediction §2.2 "post moves only by
  insert walk": holds.
- **Traffic keys pay the MISS distance.** Table-wide mean hit cost at α 0.766 / 0.916 =
  1.30 / 2.23 buckets; traffic keys measure 2.81 / 15.4. Traffic keys are inserted after
  the filler, so each lands at the end of its cluster and every later lookup walks back
  out to it: ~ miss distance at insert time (2.96 / 13.2-18.3). The table-wide average is
  the filler's and says nothing about the traffic. §2.2 predicted "worse than mean"; the
  size (5-7x) was missed, and "flat to α≈0.5" is refuted: +17 ticks already at α 0.5.
- **The collapse is work, not wait.** At α 0.9 find is 264-272 work vs 32-36 mem_wait.
  Pipeline still hides DRAM (stall_l3 ≤ 1.4). Cost per reprobe falls as chains lengthen
  (q≤4, base find 28.5 at α≈0): ~84 ticks at α 0.5, ~46 at 0.75, ~21 at 0.9: long chains keep the pipeline busy;
  short ones pay a full pop/push turn per extra bucket.
- **False sharing scales with probe count.** α 0.9, q≥5: find +80-110, all other_stall;
  shipped loop steps 412-419 → 486-490 at the same q. q=5 is when lcores 8 and 10
  share line 3 (§3.3). Every pop and push writes head/tail, so 15 pops/key = 15x the
  writes. +13 ticks on the affected lcores at α≈0 becomes ~+100.
- Occupancy at pop falls 32.5 → 22 as α rises: long-chain keys stay in the queue while
  their burst's short ones drain, and the burst barrier still empties it.

### 3.5 Deep dives: PT per-packet timeline, c2c, allocations

Data: `sweeps/reflect/deep/`. PT on CPU 2 (lcore 2, not in a false-sharing write pair),
build-ptw, 0.2 s windows, no loss. c2c on shipped build, worker CPUs, 3 s.

PT, per 64-packet burst (`analysis.py ptw`; q=6 and q=1 agree within 3% for lcore 2):

| | α≈0.016 | α=0.9 |
|---|---|---|
| burst period | 4.63 µs (72 ns/pkt) | 20.5 µs |
| hash phase | 1.39 µs | 1.53 µs |
| find phase | 1.58 µs | 17.0 µs |
| push → resolve per lookup (in flight), p50 / p99 | 735 / 1198 ns | 4.8 / 18.8 µs |
| gap between consecutive resolves, p50 / p99 | **8 / 255 ns** | 115 / 1623 ns |
| reprobes per burst | 0 | 918 (14.3/key) |
| bursts resolved in submission order | 49690 / 49691 | 0 / 12203 |

- α≈0: a lookup lives 735 ns in the queue, ~5x one DRAM access, while resolves are 8 ns
  apart and only the p99 tail (first pops after fill) stalls. **Pipeline depth ~63 is ~4x
  more than latency needs** (≈ DRAM latency / per-op cost ≈ 150/12 ≈ 12-16). Consistent
  with §5.14 (depth sweep) and with find mem_wait 1-4 ticks.
- α≈0 resolves are FIFO = submission order. α=0.9: never. Completion order is a function
  of chain length.

c2c (load HITM, shipped build):

| point | local HITM loads | top line share | lines |
|---|---|---|---|
| q=1, α≈0 | 0 | — | 0 |
| q=6, α≈0 | 95 | 41% | 3 consecutive |
| q=6, α=0.9 | 353 | **83%** | 5 consecutive |

Top line at α=0.9: offsets 0x8/0xc and 0x38/0x3c = head/tail of lcore 8 (line offset 0) and
lcore 10 (line offset 0x30), 24 B stride: **line 3 exactly**. Second line: 0x28/0x2c
(lcore 4 head/tail) + 0x0 (lcore 2 `find_queue_size`) = **line 1 exactly**. All code
addresses in `dramblast_find_batch_sync`. HITM load latency 145-168 cycles, matching the
~170 inferred from rfo_hitm (§3.3). Here c2c DID see it (unlike `fshare`): the find loop
re-loads head/tail from memory each iteration (P9), so neighbour stores surface as load
HITMs.

Allocations (bpftrace uprobe counts, lcore-worker-2, 3 s): aligned_alloc : free ≈ 1 : 1 at
every point (384628 : 370216 at q=6 α≈0; 197052 : 193707 at q=1 α=0.9). uprobe traps slow
the worker, so absolute counts are not a burst rate; ratio only. With the code inventory
(§1.6): one pair per non-empty burst, nothing else from libc on the worker.

### 3.6 perf-stat instructions: unexplained, not used

`insns` over the harness's 8 s window is erratic at identical cycles (cycles = exactly 8 s
x workers x clock). `-P` runs, q=6: ≈297, 165, 113, 461 insns/pkt at α 0, .25, .5, .75.
Not `-P`-specific (independent review): pc q=4 ship / pnone / p16 / p16p = 289 / 358 /
355 / 297. Probe shows hash-phase work unchanged across all of these. Treated as broken;
cause open. u/k split recorded in `deep/*.perf` for follow-up.

### 3.7 Fix A/B: flowhash unsigned bytes, `dramblast_queue_t` padded (2026-09-23)

Fixes: `libsashstore/hash.c` `fnv_1_multi` / `fnv_1a_multi` XOR `(unsigned char)data[i]`;
`libsashstore/dramblast.h` `dramblast_queue_t` `__attribute__((aligned(64)))` (sizeof 24 →
64, checked by gdb). Four shipped binaries, only the named fix differing (built from source
copies): base, hfix, pfix, both. Data `sweeps/reflect/fix/`, 60 runs, q 6→1, all
forwarded, generator 93.28 Mpps throughout.

Loop ticks/pkt (Mpps in brackets):

| q | α | base | pfix | hfix | both |
|---|---|---|---|---|---|
| 6 | ≈0 | 135 (93.2) | 135 (93.2) | 135 (93.1) | 135 (93.1) |
| 5 | ≈0 | 124 (84.1) | 121 (86.3) | 126 (83.3) | 122 (85.8) |
| 4 | ≈0 | 121 (69.4) | 121 (68.9) | 120 (69.5) | 122 (68.5) |
| 3 | ≈0 | 121 (51.9) | 121 (51.6) | 121 (51.8) | 122 (51.4) |
| 2 | ≈0 | 122 (34.3) | 123 (34.1) | 122 (34.3) | 123 (34.0) |
| 1 | ≈0 | 117 (17.9) | 119 (17.5) | 118 (17.7) | 121 (17.4) |
| 6 | 0.9 | 479 (26.3) | **417 (30.2)** | 527 (23.9) | **456 (27.6)** |
| 5 | 0.9 | 480 (21.9) | **415 (25.3)** | 527 (19.9) | **454 (23.1)** |
| 4 | 0.9 | 412 (20.4) | 414 (20.3) | 454 (18.5) | 455 (18.4) |
| 3 | 0.9 | 411 (15.3) | 413 (15.3) | 454 (13.9) | 456 (13.8) |
| 2 | 0.9 | 415 (10.1) | 417 (10.1) | 464 (9.1) | 464 (9.0) |
| 1 | 0.9 | 420 (5.0) | 423 (5.0) | 483 (4.3) | 487 (4.3) |

**Pad fix = code fix, compare within a hash state.** α 0.9, q≥5: −63 (base→pfix) and −71
(hfix→both); q≤4: +2, within noise. α≈0, q=5: −3/−4; elsewhere ±2 (q=1 +2-3, single
lcore, nothing to share: noise or the larger stride). With both fixes α 0.9 cost is
flat 454-456 from q=6 to q=3: the q≥5 step (§3.4) is gone. Per lcore (probe, whole-run
find ticks/pkt, q=6 α 0.9): before l2-l12 = 335 333 330 **690 673** 334, rfo_hitm 2.6/pkt
on l8/l10; after 371 369 370 372 373 372, rfo_hitm 0.00 on every lcore. α≈0 l8/l10: 42-44
→ 33.

**Hash fix = workload correction, not a speed change.** The cast costs nothing: at α≈0
hash phase 50.8 vs 50.7 and find 30.8 vs 30.3 ticks/pkt (q=4 probe). Its effect is that
l2fwd now sees 16,777,216 flows, not 8,454,144: at α≈0 occupancy 1.6% → 3.1%, invisible
(pops/key 1.000 both). At α 0.9 the extra 8.3M traffic keys land in a nearly full table:
exit α 0.916 → 0.931, miss distance 18.3 → 27.1 buckets, traffic pops/key 15.4 → 17.8,
+42-67 ticks/pkt. That is the cost of the configured workload, previously understated.
Every pre-fix number (§3.1-3.6) is on 8.45M flows.

---

## 4. What this says for the async submission/completion layer

Measured facts, each with its section:

1. **At α≈0 the lookup is not waiting on memory.** find 26-34 ticks/pkt, mem_wait 1-4
   (§3.2); PT gap between resolves 8 ns (§3.5). The existing sync pipeline already hides
   DRAM. Async gains at low α cannot come from latency hiding; they come from removing
   the **burst barrier** (ramp/drain every burst, §1.1, §5.14) and per-burst fixed costs
   (alloc+free ≈ 10 ticks/pkt, §3.2).
2. **hash (49 ticks/pkt) is the largest phase at low α** and sits before any lookup can be
   submitted (§3.2). It is serial per packet today (hash loop completes before find
   starts). Overlapping hash of burst N+1 with find of burst N is the same restructuring
   an async layer needs anyway.
3. **At high α the cost is probe work, not wait** (§3.4): α 0.9 find = 264-272 work vs 32-36
   mem_wait. Async submission does not reduce the number of bucket compares; only table
   policy does (resize threshold, or inserting traffic keys earlier than filler).
   Traffic keys pay miss-distance, 5-7x the table-wide mean (§3.4).
4. **Completion order ≠ submission order at any α > ~0.5** (§3.5): 0/12203 bursts in order
   at α 0.9. Completion must be keyed by an id that survives the poll, not burst position.
   Today's id is burst-local and every array it indexes is on the stack (§1.5).
5. **Queue head/tail must be per-lcore cache-line-private** (§3.3, §3.5): unpadded
   `queues[]` costs lcores 8/10 +13 ticks/pkt at α≈0 and ~+100 at α 0.9. An async layer
   that adds more shared ring state per lcore would multiply this. Padded as of §3.7;
   every async ring/state struct gets the same.
6. **Required in-flight depth is small** at α≈0 (12-16 by PT, §3.5); at α 0.9 chains keep
   occupancy at 22 with 918 reprobes/burst. Depth is not the lever at either end.

Baseline for the async arm: shipped build WITH both fixes (`both`, §3.7: 16.8M flows,
padded), q 6→1, α {≈0.03, 0.9}. §3.4 is pre-fix (8.45M flows, unpadded). Probe
build adds +4-8 ticks/pkt (§3.1); compare async-probe vs sync-probe, never across builds.

---

### 4.1 Independent review (async design artifact), where it disagrees with this file

Artifact: https://claude.ai/artifact/5ysMGMGRwEBs34id1cfk4V (subagent, read-only on repo).
Points it raised against §3-§4, kept here so this file does not overstate:

- hash (49 ticks) is code shape, not the FNV imul chain: unrolled inlined FNV-1 with
  bit-identical keys measured 15.3 vs 46.2 ticks/pkt (microbench, L1-resident headers,
  ratio not absolute). Library built without LTO, 3 calls/pkt, variable-trip loops.
  So overlap is not the lever for hash; hash and find are ≥73% work, overlap caps ~7.6.
- alloc+free (§4.1) removable without async (`-A -1`, INVESTIGATION §5.13: 7-8 ticks).
  Barrier memory cost bounded by find mem_wait (2.5 at q=4 post-fix).
- High α: table policy fixes bucket COUNT; cost per bucket (~16.8 work ticks) is code
  (queue round trip per bucket; wide reprobe, head/tail in locals).
- §3.5 "depth ~4x more than needed": 735 ns in flight ≈ half of find phase = fill-then-drain
  geometry, not a latency requirement; 150 ns DRAM latency assumed, not measured here;
  §5.14 measured depth 16 at +6.4 cycles/pkt; PT build inflates absolute times (592 vs
  490 loop ticks at α 0.9). §3.5 depth claim withdrawn as a sizing rule.
- §3.4 "other phases flat ±3": post-fix α 0.9 tx +4.1 (mem_wait +2.6), hash up to +3.8 —
  cache-pressure signal.
- Async arm must be compared against a sync arm carrying the same non-async changes,
  not only `both`.
- Loop metric excludes empty polls (`main.c` idle branch): async work done on empty polls
  would look free. Async arms must report (loop+idle)/fwded.
- Build: `-march=nehalem` (DPDK pkg-config) lands after `-march=native` on every compile
  line (checked in build/compile_commands.json). AVX-512 kept by explicit -m flags; tuning
  and non-explicit ISA are nehalem. Pre-existing, affects every figure; not changed here.

## 5. Findings on the way

### 5.1 flowhash collapses the generator's 16.8M flows to 8.45M keys

Smoke run (q=6, α=0.5): exit occupancy − prefill occupancy = **8,454,144** keys inserted,
not the 16,777,216 flows pktgen is configured for. Offline reproduction
(generator tuples: 4 TX lcores 48-51, `-r 4194304`, through the real `flowhash()`): 16,777,216
tuples → **8,454,144 distinct keys**, exact match. Same code with `(unsigned char)` cast
in `fnv_1_multi`: 16,777,216 distinct.

Cause: `hash.c` `state ^= data[i]` with `char data[]` sign-extends bytes ≥ 0x80, flipping
the top 56 bits; FNV-1 multiply-then-xor then maps pairs of tuples to one key.

Consequence: every committed dramblast and maglev figure ran on 8.45M distinct flows
(~8.2M home buckets, ~525 MB of table lines), not 16.8M. Still far past 52.5 MiB L3, so
still DRAM-bound, but the working set is half what `pktgen/run.sh` says.

**Fixed 2026-09-23** (`hash.c`, `fnv_1_multi` and `fnv_1a_multi` XOR `(unsigned char)`):
offline reproduction now gives 16,777,216 distinct keys. A/B in §3.7. Every figure
before the fix, §3.1-3.6 included, is on 8.45M flows.

---

## 6. Step 0: harness, loop metric, probe counters, build flags

Data: `/users/sohamb/sweeps/reflect/step0/` (`rows.txt`, `launch_order.txt`, `arms.txt`,
logs). Table: `python3 analysis.py ab <dir> old:new old_p9:new_p9`.

### 6.1 What changed

- `harness.sh ab <out> <arms> [q ...]`: A/B block driver. q outer (default 6..1), arms
  inner in spec order, each run = one `cmd_sweep` in a subshell. Arm = `name bin extra`;
  `-S` without `-D` gets `-D <out>/<name>_q<q>`. Defaults `SAMPLE_AFTER=6`,
  `PERF_EVENTS=cycles,instructions`. `DRY_RUN=1` = plan only. Fails before first launch
  on missing bin or duplicate name. Writes `rows.txt` (row prefixed `arm=`),
  `launch_order.txt`, `arms.txt` (spec + bin sha256 + rev). `analysis.py ab` reads it.
- main.c `All-poll cyc per fwd packet` = (loop_tsc + idle_tsc) / fwded. Every old label
  unchanged. Sweep row gains `allpoll=` after `bin=`; `analysis.py probe` column `allp`.
  Why: §4.1, work moved onto empty polls must not look free.
- nbprobe `NBP_NEV` 6 → 8: `insns` (raw 0x00c0, inst_retired.any), `br_misp` (raw 0x00c5,
  br_misp_retired.all_branches), appended so indices 0-5 unchanged. Record 264 → 328 B,
  dump magic `nbprobp2` + version 2; `analysis.py` reads v1 and v2 (v1 parse of
  `al_a000_dramblast_q1` byte-identical to old reader). Mark cost 106 → **136 ticks**
  (+2 rdpmc, ~15 each).
- PMU budget: probe pins cycles + insns (fixed 1/0 eligible) + 6 GP; harness
  `cycles,instructions` then takes 2 GP = 8 GP full. Checked: `perf stat -C` during
  `-S 16p` run reads 100.00% enabled; ring 262144 records, 0 with zero cycles or insns.
  Any 3rd perf-stat event would multiplex perf stat (probe pinned, keeps counters).
- `bench_membw` modes `insn` (asm loop, 8 instructions) and `brrand` / `brfix` (asm
  `test; jz` on random vs alternating bit). `harness.sh validate` check 1b.
- Build: meson option `march`, default `icelake-server`, appended in target c_args
  (land after dependency cflags). `-Dmarch=dpdk` = old build. See 6.3.

### 6.2 Validation

| check | known answer | result | verdict |
|---|---|---|---|
| insns (0xc0 :u), `insn` n=1e8 vs 1e9 | 8 per iteration | (8000430111 − 800429981) / 9e8 = **8.0000001** | PASS exact |
| br_misp (0xc5 :u), `brrand` / `brfix` 1e9 vs 1e8 diff | ~0.5 / ~0 | **0.50001** / 1.6e-7 per iteration | PASS |
| same, perf generic `instructions:u` / `branch-misses:u` | = raw | identical counts every run | PASS |
| net_null `-m none -S 16p` partition | phase sum ≈ Full-loop | corrected 66.3 (raw 72.6) vs Full-loop 68, All-poll 68 (no empty polls on net_null). Loop carries ~1.2 amortised mark cost of sampled bursts | PASS |
| `tests/` (27 cases), old flags / new flags | pass | 27/27 both. Shipped Makefile FAILS under nix shell (`-march=native` stripped → no SSE4.2 crc32); passes with system gcc 11 | PASS |
| hot functions, `-Dmarch=dpdk` vs HEAD build | identical | main_loop, find_batch_sync, process_frames, insert_one, flowhash, fnv_1_multi: identical modulo addresses | PASS |
| net_null `-m dramblast -P 0.9 -c 2^22 -B 4k`, old vs new | same table | occupied 3774873 both, disp/hit/miss means equal to 3 dp; histogram deltas ≤ run-to-run of old itself (2-lcore CAS race) | PASS |
| `harness.sh codegen` old, new | alloc/free/prefetch present | OK both | PASS |

### 6.3 Build flags: what the old build really was

`-march=native` in meson.build never took effect, twice over:
1. nix cc-wrapper `NIX_ENFORCE_NO_NATIVE=1` strips `-march=native` (cc1 sees
   `-march=x86-64 -mtune=generic`).
2. DPDK 21.11 `libdpdk-libs.pc` Cflags end `-march=nehalem`, placed after project args.

Effective old target: nehalem ISA + tune, plus explicit `-mavx512f -mavx512dq` (implies
AVX2). No BMI/BMI2, FMA, LZCNT, MOVBE, AVX-512 BW/VL. gcc 10.3 knows no
sapphirerapids/emeraldrapids; `native` with wrapper off would guess `cooperlake`.
`icelake-server` = newest server target gcc 10.3 has, ISA subset of Xeon Gold 5512U
(checked in `/proc/cpuinfo`). Codegen diff, hot functions: tuning only (inc vs add 1,
`tzcnt` vs `bsf`, nop alignment). Only new-ISA encoding in hot path: 2 `tzcnt` (BMI1,
same result as `bsf` here) in `dramblast_find_batch_sync`. Whole binary adds 2 `blsr`
(BMI1) and drops zmm vectorisation (`vpermt2q`, `vextracti64x4`) in `main` stats code
(lcore 0, not per-packet). LTO main loop picks it up too.

A/B, one block, q 6→1, arms alternating old, new, old_p9, new_p9 (24 launches, 0 failed,
02:22-02:40Z). Steady Mpps / loop ticks per pkt (All-poll = loop within 1 everywhere):

| q | old α≈0 | new α≈0 | Δloop | old α 0.9 | new α 0.9 | Δloop |
|---|---|---|---|---|---|---|
| 6 | 93.28 / 135 | 93.28 / 134 | −1 | 28.27 / 453 | 28.51 / 449 | −4 |
| 5 | 88.82 / 118 | 89.76 / 117 | −1 | 23.73 / 451 | 23.93 / 447 | −4 |
| 4 | 70.89 / 119 | 71.48 / 118 | −1 | 19.04 / 452 | 19.21 / 448 | −4 |
| 3 | 53.16 / 119 | 53.68 / 118 | −1 | 14.39 / 452 | 14.48 / 449 | −3 |
| 2 | 35.22 / 120 | 35.75 / 118 | −2 | 9.57 / 460 | 9.65 / 455 | −5 |
| 1 | 18.01 / 118 | 18.07 / 118 | 0 | 4.63 / 482 | 4.67 / 478 | −4 |

(α 0.9 loop ~450, not §3.7's 454-487: same `both` code, one block apart. Compare within
a block only.)

**Verdict: keep new (`-Dmarch=icelake-server`).** α≈0: −0 to −2 ticks, inside ±3 noise
per point. α 0.9: −3 to −5 at every q, ~1%. Sign agrees in all 12 pairs (Mpps +0.1-1.1%).
Small, one pass, and old/new also differ in code layout (alignment nops), so part may be
layout, not ISA/tune. Kept anyway because it is also the correct build: flags now mean
what build files say, and nothing regressed. Revert = `-Dmarch=dpdk` or drop the
`arch_args` hunks (`meson.build`, `libsashstore/meson.build`, `meson_options.txt`).
Every figure before §6 is on the old (nehalem-tuned) build.

### 6.4 Verification (independent replicate)

Data: `/users/sohamb/sweeps/reflect/step0_rep/`, same binaries as §6.3 (`build-old` sha
ab9ef90d, `build-new` e04aee0d; `.text` byte-identical to fresh rebuilds of working tree).
Arm order REVERSED: new, old, new_p9, old_p9 per q, q 6→1, 24 launches, 0 failed,
02:47-03:06Z. Freq 2090-2098 MHz all runs.

Offline: HEAD build vs `-Dmarch=dpdk` build, 6 hot functions identical modulo addresses
(probe changes = nothing in shipped hot path). `tests/` 27/27: system gcc native, system
gcc icelake-server, nix gcc old flags, nix gcc new flags. Generator-tuple emulation
through `flowhash`, old vs new flags: 16777216 distinct keys both, key-sequence digest
e798f9c2d91c0f83 both (bit-identical). v1 dumps (`al_a000_dramblast_q1`, `_q4`): new
`analysis.py probe` JSON = HEAD reader's, minus new `allpollcyc` key. bench_membw rerun:
8.00000024 insns/iter; br_misp 0.50013 (brrand) vs 4.8e-5 (brfix) per iter at n=1e8,
instructions equal both modes.

Δloop = new − old, ticks/pkt (All-poll = same everywhere):

| q | α≈0 p1 | α≈0 p2 | mean | α 0.9 p1 | α 0.9 p2 | mean |
|---|---|---|---|---|---|---|
| 6 | −1 | 0 | −0.5 | −4 | −3 | −3.5 |
| 5 | −1 | −1 | −1 | −4 | −2 | −3 |
| 4 | −1 | −1 | −1 | −4 | −2 | −3 |
| 3 | −1 | −1 | −1 | −3 | −4 | −3.5 |
| 2 | −2 | −2 | −2 | −5 | −5 | −5 |
| 1 | 0 | 0 | 0 | −4 | −4 | −4 |

ΔMpps p1/p2: α≈0 q6..1 0.00/0.00, +0.94/+0.43, +0.59/+0.64, +0.52/+0.54, +0.53/+0.53,
+0.06/+0.08; α 0.9 +0.24/+0.18, +0.20/+0.12, +0.17/+0.08, +0.09/+0.11, +0.08/+0.12,
+0.04/+0.04. Same arm pass-to-pass |Δloop| ≤ 1 every q, every arm.

Verdict: α 0.9 gain holds, −2 to −5 every q both passes, mean −3.7, above ≤1 drift. α≈0:
−0 to −2, no sign flip in 24 pairs but 3 zeros; q6 α≈0 at generator cap (93.28 both).
§6.3 "sign agrees in all 12 pairs" overstated: pass 1 has 1 zero Δloop, 1 zero ΔMpps.
Layout vs ISA/tune still not separated (same caveat). Keep new build.

---

## 7. Step 1: flowhash rewrite

Data: `/users/sohamb/sweeps/reflect/step1/` (`rows.txt`, `launch_order.txt`, `arms.txt`,
`bin/` + `SHA256SUMS`, logs, `*.nbp`).

### 7.1 What changed

- `flowhash` out of `libsashstore/packettool.c` into `packettool.h`, `static inline`
  `always_inline`. FNV-1 over fixed 13 bytes, fully unrolled, byte loads, literal offsets
  (no `extern const` loads). Why: lib built `b_lto=false`, so old flowhash never inlined
  into main.c; 3 calls to `fnv_1_multi` per packet, variable-trip loops (§4.1).
- Keys bit-identical by construction: same byte order, unsigned XOR (§5.1 fix), version
  test `byte>>4 == 4` and proto test 6/17 same answer signed or unsigned, L4 offset
  `14 + 4*IHL` for IHL 0..15 as before.
- `hash.c` untouched: `fnv_1`, `fnv_1_multi`, `fnv_1a*` still used by `hashmap.c`,
  `conshash.c`. Revert = restore `packettool.{c,h}`.
- Codegen: new main loop 16 imul (12 FNV + 4 old; first step folds to constant), 0 calls
  to flowhash/fnv; `maglev_process_frame` 12 imul inline. Old: `flowhash` symbol, 2 call
  sites (main loop, maglev). `harness.sh codegen` OK new, new probe.
- `tests/test_dramblast.c` T11: new vs transcription of HEAD 57bf217 flowhash +
  fnv_1_multi. Makefile dep on `packettool.h`.

### 7.2 Equivalence (before any rig time)

| input | count | mismatches | other |
|---|---|---|---|
| pktgen tuples (lcores 48-51, ctr 1..2^22) | 16777216 | **0** | 0 zero keys, 16777216 distinct, digest 439c6931f083e325 |
| random frames (random bytes, version 4 or raw, IHL 0..15, proto 6/17/raw) | 10000000 | **0** | 3555174 hashed nonzero, 1111165 of those IHL<5 |

`tests/` 32/32: nix gcc 10.3 (`nix develop`) and system gcc 11.4. Teeth: mutant with
signed XOR → 16773118 tuple mismatches, 8454144 distinct (= §5.1 exactly); mutant
masking last byte → 1775915 random mismatches, tuples 0 (why random block needed).

### 7.3 Predictions, written before rig data

- probe hash phase ≤ 20 ticks/pkt (from 49, §3.2).
- loop α≈0: −25 to −31 ticks/pkt where not generator-capped. Cap 93.28 Mpps: q≥5 may
  cap after change; q≤4 discriminate.
- α 0.9: loop drops by about same absolute ticks (hash flat in α, §3.4).
- maglev loop drops too.
- forwarded/unmapped consistent: no new unmapped packets.

### 7.4 A/B

Block 1 (`harness.sh ab`, mode dramblast, 36 launches, 03:13-03:40Z): q 6→1, per q
`old`, `new`, `old_p9`, `new_p9` (`-P 0.9`), `oldprb`, `newprb` (`-S 16p`). Block 2
right after (`MODE=maglev harness.sh ab`, same out dir, 12 launches, 03:40-03:49Z):
`mag_old`, `mag_new`. `ab` takes mode per block, not per arm: hence 2 blocks. 48
launches, 0 failed, 0 unmapped in all 48. Bins (sha256 first 16): old 766e8f08013c0aff,
new 69306a2dfcd91d53, old-prb 9e559ea2b979d857, new-prb b049a917d717d568; built
`nix develop`, default flags (`march` icelake-server); old = worktree of 57bf217.

Steady Mpps / loop ticks per pkt (All-poll = loop in every run):

| q | old α≈0 | new α≈0 | Δ | old α 0.9 | new α 0.9 | Δ | mag_old | mag_new | Δ |
|---|---|---|---|---|---|---|---|---|---|
| 6 | 93.28 / 135 | 93.28 / 135 | 0 (cap) | 28.57 / 448 | 29.41 / 435 | −13 | 55.95 / 226 | 75.81 / 167 | −59 |
| 5 | 89.61 / 117 | 93.28 / 112 | −5 (cap) | 23.95 / 447 | 24.70 / 433 | −14 | 50.76 / 208 | 64.34 / 164 | −44 |
| 4 | 71.52 / 118 | 83.27 / 101 | **−17** | 19.23 / 447 | 19.79 / 435 | −12 | 40.83 / 207 | 51.65 / 163 | −44 |
| 3 | 53.60 / 118 | 61.98 / 102 | **−16** | 14.52 / 448 | 14.96 / 435 | −13 | 30.77 / 207 | 38.65 / 164 | −43 |
| 2 | 35.79 / 118 | 41.27 / 102 | **−16** | 9.66 / 454 | 9.93 / 442 | −12 | 20.48 / 209 | 25.79 / 165 | −44 |
| 1 | 18.06 / 118 | 20.77 / 102 | **−16** | 4.67 / 477 | 4.82 / 463 | −14 | 10.28 / 212 | 12.96 / 166 | −46 |

α≈0 Mpps +14-16% at q≤4. q=5 new at 93.28 cap, q=6 both capped (new B 21 vs 33:
slack goes to smaller bursts, not ticks, §0.1). Probe arms: Δloop 0, −11, −19, −17, −17,
−15 (q 6→1). α 0.9 exit occupancy old = new at q≥4 (499961036 both), ±2/±1150/±18000
at q 3/2/1 (prefill race, §6.2): same key set.

### 7.5 Probe, hash phase (ring, mark cost subtracted)

| q | old ticks | new ticks | old work / mem_w / other | new work / mem_w / other | insns old → new | IPC old → new |
|---|---|---|---|---|---|---|
| 6 | 46.7 | 34.6 | 39.6 / 4.8 / 2.3 | 26.9 / 6.7 / 0.9 | 147.8 → 71.5 | 3.17 → 2.07 |
| 5 | 44.9 | 31.7 | 38.6 / 4.6 / 1.6 | 25.6 / 5.4 / 0.5 | 146.6 → 69.7 | 3.27 → 2.21 |
| 4 | 45.1 | 31.2 | 38.6 / 4.7 / 1.6 | 25.4 / 5.4 / 0.3 | 146.7 → 69.5 | 3.27 → 2.24 |
| 3 | 45.3 | 31.2 | 38.7 / 4.7 / 1.6 | 25.4 / 5.4 / 0.2 | 146.5 → 69.5 | 3.26 → 2.24 |
| 2 | 45.4 | 31.2 | 38.7 / 4.8 / 1.6 | 25.4 / 5.5 / 0.2 | 146.6 → 69.5 | 3.25 → 2.24 |
| 1 | 45.7 | 31.4 | 38.9 / 5.0 / 1.6 | 25.4 / 5.6 / 0.2 | 146.6 → 69.5 | 3.22 → 2.23 |

Hash p99 50 → 35. Other phases ±2 (rx −1.5, tx −2, find ±0.5 at q≤4). Phase sum
−17 to −19 at q≤4 = hash −14 plus rx/tx −3-4 (layout or less cache pressure; not
separated).

- **Instructions halved (−77/pkt), time −30%.** New loop latency-bound: IPC 3.3 → 2.2.
  Per packet 12 dependent `imul` (3 cyc) + `xor` = ~48 cyc chain; 1st step's `imul`
  folds into constant (basis × prime), so 13 steps, 12 `imul`. Overlap across packets
  only by OOO; scheduler holds dependent chain of ~4 packets.
- Scratch microbench (not in repo; L1-resident headers, 64-pkt bursts, rdtsc): this
  loop 23.3 ticks/pkt; 4-packet interleaved chains (same 13-byte FNV-1, same keys)
  15.6. §4.1's 15.3 matches interleaved shape, not straight-line inline. Rig adds ~6
  mem_wait (first touch of mbuf + header line) and ~2 on top.
- maglev −43 to −47 (q6 −59 artifact, §7.8), ~3x dramblast's −16: old maglev also
  called out-of-line flowhash → 3 fnv calls per packet, then dependent hashmap lookup per packet. Guess
  (no maglev probe): shorter per-packet instruction stream lets OOO overlap more
  lookup misses. Not measured.

### 7.6 Predictions vs result

| prediction | result | verdict |
|---|---|---|
| hash ≤ 20 ticks/pkt | 31.2-31.7 (q≤5), 34.6 q=6 | **FAIL** (−14, not −29) |
| loop α≈0 −25 to −31 where not capped | −16/−17 at q≤4 (probe −15 to −19) | **FAIL**, ~55-60% of predicted |
| α 0.9 same absolute drop | −12 to −14 (vs −16 α≈0) | PASS roughly (80%) |
| maglev loop drops | −43 to −47 (q6 −59, artifact §7.8) | PASS (bigger than dramblast) |
| no new unmapped | 0 unmapped all 48 runs; same α 0.9 exit occupancy | PASS |

Why predictions missed: prediction took §4.1 microbench ratio (15.3/46.2) onto rig
hash (49). Ratio came from interleaved-shape bench on L1 headers; rig hash = chain
latency + ~5-6 mem_wait that code shape cannot remove.

### 7.7 Verdict

**Keep.** Bit-identical keys (0 / 26.8M mismatches), −16/−17 loop ticks α≈0 at every
uncapped q (+14-16% Mpps), −12 to −14 at α 0.9, −43 to −47 maglev (q6 −59 = position
artifact, §7.8), no regression anywhere, sign same in all 22 nonzero-Δ pairs (α≈0 q≤5,
α 0.9, maglev, probe q≤5). One pass here; replicate with reversed order in §7.8 agrees
±2. Effect 5-15x §6.4 pass-to-pass drift (≤1). Revert = restore `packettool.{c,h}` (tests T11 then fails to compile:
revert `tests/` hunks too).

Next lever inside hash (not done here): interleave 2-4 packets' FNV chains (bench 23.3
→ 15.6 on L1). Keys unchanged; T11 guards it.

### 7.8 Verification (independent verifier, 2026-09-23)

Data: `/users/sohamb/sweeps/reflect/step1_rep/`. Same `step1/bin/` binaries (SHA256SUMS
OK). Own rebuild: new from working tree, old from `git archive 57bf217`; `.text`,
`.rodata`, `.data` identical to `step1/bin/` all 4 bins. Whole-file sha differs (debug
paths only).

Keys, own reference (not T11's): HEAD `packettool.c` + `hash.c` compiled as-is,
`objcopy --redefine-sym flowhash=old_flowhash`, linked vs new inline; old -O0/-O2 × new
-O0/-O2/-O3:

| input | count | mismatches |
|---|---|---|
| pktgen tuples | 16777216 | 0 |
| random frames (half all bytes ≥ 0x80; version 0x4X any IHL or raw; proto 6/17/0/255/raw) | 25000000 | 0 |
| grid: byte 14 all 256 × byte 23 all 256 × fill 00/7f/80/ff/5a/random | 393216 | 0 (192 nonzero = 16 IHL × 2 proto × 6) |

Bytes read (guard page after frame): max offset 77 both (IHL 15), 37 at IHL 5, 33 at
IHL 0; no growth. Only change: non-IPv4 frame reads byte 23 (old: byte 14 only);
inside 60 B Ethernet minimum, harmless. Mutants (signed XOR, fixed IHL 5, proto
constant 17): T11 fails 3/1/1 checks. `tests/` 32/32 nix gcc 10.3.0, system gcc 11.4.0.
Rig: α 0.9 exit occupancy − prefill = 499961036 − 483183820 = 16777216 at q≥4, both
arms, both passes: every generator flow a distinct key on real traffic.

Codegen: new `flowhash` symbol gone, 0 `call` to flowhash/fnv in `l2fwd_main_loop`,
`maglev_process_frame`; imul 16 / 12 (old 4 / 0). `harness.sh codegen` OK, both builds.

Replicate: arm order REVERSED (new before old), `harness.sh ab` dramblast block (new,
old, new_p9, old_p9; 24) then `MODE=maglev` block (mag_new, mag_old; 12), q 6→1,
04:01-04:28Z. 36 launches, 0 failed, 0 unmapped all 36. pktgen-monitor: 36 = 36 + 0,
1177 / 1183 in-span samples 93.28, rest above-line-rate artifact, nothing quarantined.

Δloop ticks/pkt (new − old), pass 1 = §7.4, pass 2 = replicate; ΔMpps in parens:

| q | α≈0 p1 | α≈0 p2 | mean | α 0.9 p1 | α 0.9 p2 | mean | maglev p1 | maglev p2 | mean |
|---|---|---|---|---|---|---|---|---|---|
| 6 | 0 (0.00) | 0 (0.00) | 0 cap | −13 (+0.84) | −13 (+0.90) | −13 | −59 (+19.86) | −24 (+8.29) | −41.5 * |
| 5 | −5 (+3.67) | −5 (+3.66) | −5 cap | −14 (+0.75) | −15 (+0.76) | −14.5 | −44 (+13.58) | −44 (+13.45) | −44 |
| 4 | −17 (+11.75) | −16 (+11.77) | −16.5 | −12 (+0.56) | −14 (+0.62) | −13 | −44 (+10.82) | −44 (+10.78) | −44 |
| 3 | −16 (+8.38) | −17 (+8.57) | −16.5 | −13 (+0.44) | −14 (+0.44) | −13.5 | −43 (+7.88) | −44 (+8.01) | −43.5 |
| 2 | −16 (+5.48) | −16 (+5.50) | −16 | −12 (+0.27) | −13 (+0.27) | −12.5 | −44 (+5.31) | −44 (+5.22) | −44 |
| 1 | −16 (+2.71) | −16 (+2.72) | −16 | −14 (+0.15) | −14 (+0.14) | −14 | −46 (+2.68) | −47 (+2.69) | −46.5 |

Sign: 34 / 34 nonzero per-pass Δ negative (17 q-pairs, 2 passes); α≈0 q6 0 both. Pass
agreement ±2 ticks everywhere except maglev q6. Per-arm absolute: old α≈0 q4 71.52 /
71.57 Mpps, new 83.27 / 83.34, loop ±1 across passes. Order reversal moved nothing:
no position bias in dramblast block.

\* maglev q6 = position artifact, not code. First launch of each maglev block (after
dramblast block) slow in both passes, whichever arm: step1 mag_old_q6 init 19 s, loop
226 (rep 208); rep mag_new_q6 init 42 s, loop 184 (step1 167). Same 2 runs: perf IPC
0.12 / 0.13 vs 1.29 / 1.38 for other arm. Opposite arms first → per-pass Δ −59 / −24,
mean −41.5 ≈ q≤5's −44. Cause not measured (guess: 8 GiB THP allocation right after 1G
hugetlb block, compaction / partial THP). pktgen-monitor's "mag_new inits tighter" lead
= this artifact, retracted by it. Fix for later blocks: 1 discarded maglev warm-up
launch before first measured one.

Corrections to §7.1-7.7: §7.7 "24 uncapped pairs" → 22 nonzero-Δ pairs (fixed there).
§7.5 "13th step folds" → 1st step's imul folds (fixed). §7.7 "maglev −43 to −59" → −43 to −47 (fixed there;
q6 −59 artifact). Rest of §7.4-7.5 numbers re-derived from raw logs and ring dumps
(`analysis.py ab`, `analysis.py probe`): match. Verdict: **keep**, two passes.
