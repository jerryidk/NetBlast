# The NetBlast forward path and the `userspace-ice` reflectors

*2026-09-17. Read-only comparison; no code in either tree was modified.*

Companion script: [`../l2fwd/compare_userspace_ice.py`](../l2fwd/compare_userspace_ice.py),
which derives every number quoted here. Sibling tree: `/users/sohamb/userspace-ice`,
imported onto this box at 02:01 on 2026-09-17.

---

## Summary

NetBlast's `l2fwd` prints **5 cycles per packet** for its no-lookup forward path.
`userspace-ice` reports **33–44 cycles per packet** for its reflectors. The gap
looks like a factor of seven, and it is not one: **the two numbers measure
different regions of the same loop.**

- NetBlast's figure comes from an `rdtsc` bracket that opens *after*
  `rte_eth_rx_burst` has returned and closes *before* `rte_eth_tx_burst` is
  called (`l2fwd/main.c:309`–`:359`). The entire DPDK PMD — descriptor polling,
  mbuf refill, Tx descriptor writes, the doorbell — is outside it.
- `userspace-ice`'s figure is whole-process `cycles:u` divided by packets
  forwarded (`analysis/scripts/refpool_symmetric_tables.py:101`). It includes
  every one of those things, because `userspace-ice` has no PMD to hide them in:
  it is a VFIO driver that open-codes the ring itself.

Put NetBlast on the same axis by deriving cost from its measured throughput:
**65.85 Mpps on one core at a pinned 2.100 GHz = 31.9 cyc/pkt.** Against
`userspace-ice`'s 33.0–36.3 cyc/pkt for its C targets at a *measured* 2.95 GHz,
the two are within about 10% of each other.

| | cyc/pkt | what it covers |
|---|---|---|
| NetBlast `-m none`, bracketed | 5 | MAC rewrite only |
| NetBlast `-m none`, whole loop | **31.9** | everything the core does |
| `userspace-ice` C, b=256 | **33.0** | everything the core does |
| `userspace-ice` C, b=64 | **36.2** | everything the core does |
| `userspace-ice` Rust, worst | 43.5 | everything the core does |

The residual ~27 cyc/pkt between NetBlast's bracketed 5 and its whole-loop 31.9
is the PMD and the poll loop — **84% of the per-packet cost**, and precisely the
part `userspace-ice` writes by hand and counts.

**Three practical consequences.**

1. NetBlast's printed `Cycle per fwd packet` must never be compared against a
   `userspace-ice` cyc/pkt. They are not the same measurement, and the
   comparison inflates the driver by ~6x. Note the shape of the error: **the 5
   was never wrong, the axis was.** §5.29 verified that 5 cyc/pkt is real by
   rebuilding the instrument, and that verification still stands. A bad number
   and a good number on the wrong axis look identical from outside, and
   "correcting" the 5 would have destroyed a sound measurement while leaving the
   actual error untouched.
2. A reflect is semantically a specialised forward, and once measured the same
   way the two cost the same. That is the expected result, and it is a useful
   cross-check on both rigs rather than a finding about either.
3. **No DPDK campaign exists in the `userspace-ice` import on this box.**
   `dpdk/l2_reflect.c` is present and the runner supports `--no-dpdk`, but all
   four live campaigns in `docs/CURRENT_DATA.md` §1 contain the eleven driver
   variants only. Any remembered DPDK-versus-driver number from that tree cannot
   be sourced here.

---

## 1. The question

The two projects sit on opposite sides of the DPDK boundary but do
near-identical work to each packet, and both report a cycles-per-packet figure.
Those figures differ by roughly 7x. Either the hand-written driver is far more
expensive than a DPDK forwarder — which would be a real and interesting result —
or the two figures are not comparable. This document settles which.

## 2. What the two programs are

`l2fwd/main.c` is a **DPDK application**. The NIC appears in exactly two places:

```c
      unsigned nb_rx =
          rte_eth_rx_burst(portid, queueid, pkts_burst, MAX_PKT_BURST);
```
<sub>`l2fwd/main.c:302`</sub>

```c
        uint16_t nb_tx = rte_eth_tx_burst(portid, queueid, pkts_burst, nb_rx);
```
<sub>`l2fwd/main.c:361`</sub>

Descriptor rings, buffer recycling and doorbells are all behind the PMD.

`userspace-ice/variants/c-*` are **a driver**. There is no DPDK. `main.c` opens a
VFIO group on a BDF, maps BAR0, allocates DMA memory, talks to the admin queue
for VSI/LPORT/parent-TEID, builds the LAN queue contexts, installs a MAC switch
rule, and then runs the datapath itself in `run_rx_reflect`
(`variants/c-ptrarith/ice_lanq.c:633`). Descriptor layouts, the DD/EOF/RXE status
bits, the RS bit and the tail register are all open-coded.

There are ten C variant trees. They are the *same datapath* with one thing
changed: how a pool slot is named. `c-ptrarith` names it by index and recomputes
the address on every use —

```c
static inline struct pkt_buf *pkt_pool_get_entry(const struct pkt_mempool *pool, uint32_t idx)
{
    return (struct pkt_buf *)(pool->base + (size_t)idx * ICE_PKT_BUF_ENTRY_SIZE);
}
```
<sub>`variants/c-ptrarith/ice_dma.h:17`</sub>

— while `c-refpool` names it by `struct pkt_buf *`. `diff -u
c-ptrarith/ice_lanq.c c-refpool/ice_lanq.c` is entirely `uint32_t buf_idx` →
`struct pkt_buf *buf` and `ICE_PKT_BUF_INVALID_IDX` → `NULL`. The `c-safe-*`
trees add bounds and overflow checks on top. None of them changes the loop's
shape, which is why they all land within 7 cyc/pkt of each other.

The `c/` directory is an uninitialised submodule (`git submodule status` shows a
leading `-` on `7aee001c…`), so the live C is `variants/c-*` and `dpdk/`.

## 3. Why the two figures are not the same quantity

This is the whole finding, and it is visible in eight lines of each tree.

**NetBlast brackets a sub-region.** The timer opens after the Rx burst has
already completed:

```c
      unsigned nb_rx =
          rte_eth_rx_burst(portid, queueid, pkts_burst, MAX_PKT_BURST);

      port_statistics[portid][lcore_id].rx_cnt += 1;
      if (nb_rx > 0) {
        port_statistics[portid][lcore_id].rx += nb_rx;

        uint64_t start = rte_rdtsc();
```
<sub>`l2fwd/main.c:302`–`:309`</sub>

and closes before the Tx burst begins:

```c
        port_statistics[portid][lcore_id].hash_tsc += (rte_rdtsc() - start);

        uint16_t nb_tx = rte_eth_tx_burst(portid, queueid, pkts_burst, nb_rx);
```
<sub>`l2fwd/main.c:359`–`:361`</sub>

Between those two reads, in `-m none`, the program does this and nothing else:

```c
          for (uint16_t j = 0; j < nb_rx; j++) {
            unsigned dst_port = l2fwd_dst_ports[portid];
            uint64_t mac = 0xff;
            l2fwd_mac_updating(pkts_burst[j], dst_port, mac);
          }
          port_statistics[portid][lcore_id].fwded += nb_rx;
```
<sub>`l2fwd/main.c:351`–`:356`</sub>

Five cycles is an honest number for that loop. It is not a number for forwarding
a packet.

**`userspace-ice` divides the whole run.** Its `Point` class takes `cycles:u`
over the entire benchmark and packets over the entire benchmark:

```python
    def __init__(self, run: dict, counters: dict):
        self.mpps = float(run["sw_tx_mpps"])
        self.gbps = float(run["sw_tx_l2_gbps"])
        self.wall = float(run["active_seconds"])
        self.pkts = float(run["sw_rx_mpps"]) * 1e6 * self.wall
        self.cycles, runtime_ns = counters["cycles:u"]
        self.instructions, _ = counters["instructions:u"]

    cyc_pkt = property(lambda s: s.cycles / s.pkts)
```
<sub>`userspace-ice/analysis/scripts/refpool_symmetric_tables.py:89`–`:101`</sub>

There is no bracket. Every cycle the process burns — including the descriptor
work below — is in the numerator.

## 4. The work that is inside one figure and outside the other

`userspace-ice` counts all of the following per packet. NetBlast does the
equivalent work inside the PMD and excludes every cycle of it from its printed
figure.

**Rx: rearm the descriptor before the packet is even processed.**

```c
        replacement_buf_idx = pkt_pool_pop_idx_noinit(pool);
        if (unlikely(replacement_buf_idx == ICE_PKT_BUF_INVALID_IDX)) {
            fprintf(stderr, "[my_ice] reflect pool underflow during immediate rearm\n");
            return -1;
        }

        completed_buf_idx = rx_pkt_buf_idxs[idx];
        rx_pkt_buf_idxs[idx] = replacement_buf_idx;
        rxd->read.pkt_addr = htole64(reflect_data_iova(d, replacement_buf_idx));
        rxd->read.hdr_addr = 0;
        rxd->read.rsvd1 = 0;
        rxd->read.rsvd2 = 0;
```
<sub>`variants/c-ptrarith/ice_lanq.c:536`–`:547`</sub>

A pool pop, an IOVA recomputation, and four descriptor stores — per packet.

**Tx: build the descriptor, manage the RS bit, recycle the parked buffer.**

```c
    q->tx_pkts_since_rs++;
    if (q->tx_pkts_since_rs >= TX_RS_THRESH) {
        cmd |= ICE_TX_DESC_CMD_RS;
        tx_record_rs_slot(q, idx);
        q->tx_pkts_since_rs = 0;
    }

    txd = &q->tx_desc[idx];
    txd->buf_addr = htole64(reflect_data_iova(d, buf_idx));
    qw1 = ((uint64_t)ICE_TX_DESC_DTYPE_DATA << ICE_TXD_QW1_DTYPE_S) |
          ((uint64_t)cmd << ICE_TXD_QW1_CMD_S) |
          ((uint64_t)len << ICE_TXD_QW1_TX_BUF_SZ_S);
    txd->cmd_type_offset_bsz = htole64(qw1);

    old_buf_idx = q->tx_pkt_buf_idxs[idx];
    if (unlikely(old_buf_idx == ICE_PKT_BUF_INVALID_IDX))
        die_msg("missing tx parked slot");
    pkt_pool_push_idx(pool, old_buf_idx);
```
<sub>`variants/c-ptrarith/ice_lanq.c:420`–`:438`</sub>

Ring position *is* the completion signal: the buffer previously parked in this
descriptor slot is pushed back to the pool as the new one takes its place. There
is no completion callback, because there is no PMD to provide one.

On top of that, per batch: a deferred Rx tail publish (`flush_rx_rearmed_tail`,
`:560`), a release fence and doorbell write (`tx_ring_doorbell`, `:466`), and
`tx_update_free` walking head against next-to-use to recover credit.

That ledger is the 27 cyc/pkt. It is not overhead the driver adds; it is work
NetBlast also does and does not count.

## 5. The measurement

`l2fwd/compare_userspace_ice.py` derives NetBlast's whole-loop cost from
throughput and reads the `userspace-ice` figures out of that project's campaign
CSVs. Output:

```
NetBlast l2fwd, -m none, q=1
  throughput           65.85 Mpps (core-limited; generator offers 93.28)
  clock                2.100 GHz pinned
  bracketed cyc/pkt    5 (main.c:309-359, excludes the PMD)
  whole-loop cyc/pkt   31.9  = 2100 / 65.85
  PMD + poll share     26.9 cyc/pkt (84%)

userspace-ice, 20260813-190253-all11-fp-off-10s-3rep, q=1 (whole-process cycles:u / packets)
  target                            b=64     b=256     GHz
  --------------------------------------------------------
  c-refpool                         36.2      33.0    2.95
  c-unsafe-refpool                  36.3      33.0    2.96
  c-safe-refpool                    36.9      33.5    2.95
  unsafe-ptrarith                   37.4      34.8    2.95
  unsafe-refpool                    37.7      33.6    2.95
  c-baseline                        38.4      36.1    2.95
  c-safe-refpool-overflow           38.4      34.2    2.95
  safe-refpool-bounds               38.9      34.8    2.95
  safe-idxs                         41.3      37.8    2.95
  safe-refpool-overflow             42.0      36.1    2.95
  safe-refpool-errors               43.5      37.9    2.95

  range across all targets and batches: 36.2 - 43.5 cyc/pkt
  NetBlast whole-loop, same axis:       31.9 cyc/pkt
```

Two things about the sources.

- NetBlast's 65.85 Mpps is from `INVESTIGATION.md` §5.20 (`docs/INVESTIGATION.md:2308`).
  It is **core-limited, not generator-limited**: the generator offers 93.28 Mpps
  (`:2307`) and q=1 falls well short of it, so this is a genuine single-core
  cost. `c-baseline` in the table above is `c-ptrarith`, which carries no variant
  label in the result CSVs by design.
- **That throughput predates the generator problem, and this was checked rather
  than assumed.** The whole force of this comparison is that two independently
  measured numbers land within 10% of each other, so a contaminated throughput
  figure would move the conclusion. The node1 generator became unusable at
  roughly 21:45 on 2026-09-16 (§5.28, and the re-baseline it blocks at
  `docs/INVESTIGATION.md:3556`). `git log -S"65.85" -- docs/INVESTIGATION.md`
  returns exactly one commit, `fc6bdac` at **2026-09-14 21:47:05 +0000** — about
  two days earlier. The supporting artefacts agree: `docs/analysis_output.txt` is
  2026-09-14 22:01 and `docs/matrix.svg` is 2026-09-15 00:00. The figure is
  clean, and that commit timestamp also clears an earlier degradation window on
  2026-09-13 that is tracked outside this repository.

  The method is worth reusing. The check is a **commit search for the value
  itself**, not a file mtime: `git log -S` finds when the number entered the tree,
  which survives a file being regenerated, moved or reformatted, whereas an mtime
  only says when something was last written and vanishes if the raw artefacts are
  cleaned up. Earlier provenance claims in this project had to rest on timestamps
  because the raw `.perf` files were gone. Whenever a figure under suspicion lives
  in a tracked file, reach for `git log -S` first.
- The `userspace-ice` clock is **measured, not assumed**. `docs/CURRENT_DATA.md`
  §1 states "CPU pinned at 3 GHz"; the script recomputes it as `cycles:u` divided
  by the counter's scheduled runtime and gets 2.95 GHz consistently. The
  comparison uses the measured value.

## 6. Structural differences that do *not* explain the gap

These are real differences between the two programs, listed so that nobody
reaches for them as the explanation. Each is either small or cancels.

**The L2 rewrite has opposite semantics.** `userspace-ice` reflects — it moves
the original source MAC into the destination field and writes the local MAC into
source, preserving the sender:

```c
    new_word0 = (word0 >> 48) |
                ((word1 & UINT64_C(0x00000000ffffffff)) << 16) |
                ((uint64_t)ctx->local_mac01 << 48);
    word1 = (word1 & UINT64_C(0xffffffff00000000)) |
            (uint64_t)ctx->local_mac25;
```
<sub>`variants/c-ptrarith/ice_lanq.c:616`–`:620`</sub>

NetBlast rewrites for forwarding — it stores an 8-byte word over `dst_addr` and
overwrites `src_addr` with the local port MAC, **discarding** the original
source:

```c
static inline void l2fwd_mac_updating(struct rte_mbuf *m, unsigned dest_portid,
                                      uint64_t mac) {
  struct rte_ether_hdr *eth = rte_pktmbuf_mtod(m, struct rte_ether_hdr *);
  *((uint64_t *)&eth->dst_addr.addr_bytes[0]) = mac;
  rte_ether_addr_copy(&l2fwd_ports_eth_addr[dest_portid], &eth->src_addr);
}
```
<sub>`l2fwd/main.c:260`–`:265`</sub>

Both touch the same 16 bytes and both are a handful of instructions. The
semantic difference is real; the cost difference is not.

**Scale and configuration.**

| | NetBlast `l2fwd` | `userspace-ice` C |
|---|---|---|
| Queues | RSS, `-q N` queues/port, one lcore each (`main.c:96`) | exactly one Rx, one Tx, no RSS, single thread |
| Ports | multi-port with a dst-port map | one BDF |
| Ring depth | 4096 / 4096 | 2048 / 2048 |
| Burst | `MAX_PKT_BURST` 64 | 128 default, 256 max |
| Tx refusal | frees the mbuf, counts `tx_dropped` — **packet lost** (`main.c:364`) | never drops; marks `stalled`, `usleep(50)`, retries |
| Reporting | 32 one-second Mpps samples | one end-of-run Mpps/Gbps line |

Both figures above are single-core, so the queue-count difference does not enter.

**Two NetBlast observations worth recording** — neither affects this comparison,
both are surprising on first reading:

- `l2fwd_simple_forward` (`main.c:267`), which uses `rte_eth_tx_buffer` and
  `dst_port`, is **defined and never called**. The live loop transmits back out
  `portid`, the port it received on — so at port level NetBlast is also a
  reflector, despite its forwarder-shaped MAC rewrite.
- In `maglev` and `dramblast` modes a packet whose lookup misses increments the
  `dropped` counter but is **still transmitted**, because `:361` sends the whole
  `pkts_burst` of `nb_rx` regardless. That counter does not correspond to a
  packet leaving the system.

## 7. Caveats

- **Different clocks** (2.100 vs 2.95 GHz). cyc/pkt is nominally
  clock-normalised, but a DRAM stall costs more *cycles* at the higher clock, so
  the `userspace-ice` figures are mildly inflated relative to NetBlast's on any
  memory-bound portion. For a reflect with no table this is small, not zero, and
  it is the largest known bias in the comparison.
- **Frame size.** `userspace-ice` is measured at 64 B
  (`scripts/run_e810_cqda2_64b_mpps.sh`). NetBlast's frame size at the §5.20
  point is not confirmed here. Per-packet cost for a rewrite-and-forward is
  largely frame-size independent, but this is unverified rather than checked.
- **The 5 cyc/pkt is itself biased low by ~0.9 cyc/pkt** and quantised to 1
  cyc/pkt — `rte_rdtsc` is an unfenced `rdtsc`, so the closing read retires while
  the loop's stores are still in the store buffer (§5.29). This is irrelevant at
  the 32-versus-33 scale of this document, and would matter only if the
  *brackets* were compared, which they should not be.
- **The derived 31.9 assumes the core is the limit at q=1.** It is (65.85 against
  93.28 offered), but if a future generator offers less than ~66 Mpps the same
  arithmetic would silently produce a generator ceiling instead of a cost.

## 8. Reproducing

```bash
python3 l2fwd/compare_userspace_ice.py --ice-root ../userspace-ice
```

The script reads `results/20260813-190253-all11-fp-off-10s-3rep` from the
`userspace-ice` tree and NetBlast's §5.20 constants, which are named and
line-cited at the top of the file so they can be rechecked against
`docs/INVESTIGATION.md` when that document changes.
