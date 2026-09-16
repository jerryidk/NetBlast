# DRAMBlast: static analysis and async-port feasibility

Covers two questions:

1. What can be improved in `l2fwd/libsashstore/dramblast.c` by inspection?
2. Is it feasible to port `docs/cas_kht.hpp`'s `find_batch_inline` — the
   *asynchronous* batch API — into the l2fwd datapath, given that it forces
   packets to be tracked across RX bursts?

Correctness and performance are kept separate, per request.

---

## 1. Scope and method

Everything below is static inspection of the source, plus a functional test
harness under `tests/` that compiles `dramblast.c` unmodified and runs it. **No
performance measurement was taken and the l2fwd application was not run.** Every
number in §3 and §5 is an analytical estimate; each carries its assumption
inline and none should be quoted as a measurement.

Files read: `l2fwd/libsashstore/dramblast.{c,h}`, `l2fwd/main.c`,
`l2fwd/libsashstore/{conshash,hash,hashmap,maglev,packettool,sashstore}.{c,h}`,
`pktgen/pktgen.c`, `docs/cas_kht.hpp`, the meson files, `l2fwd/run.sh`,
`docs/results.json`.

Modelling assumptions used throughout §5:

| Quantity | Value | Source |
|---|---|---|
| Core clock | ~3.4 GHz | target class; `docs/results.json` was gathered elsewhere |
| Loaded DRAM latency | ~80 ns ≈ 270 cycles | typical server DDR4 under load |
| Line-fill buffers per core | 10–12 | Intel client/server norm |
| Table | 2^29 slots × 16 B = 8 GiB, 1 GiB hugepages | `l2fwd/run.sh` `-c 536870912` |
| RX burst | 64 | `MAX_PKT_BURST`, `main.c:50` |
| End-to-end budget | ~150 cycles/packet | `docs/results.json` dramblast ≈ 19.9 Mpps single core |

**Terminology.** The request named "NOAGGR". That token does not appear in
`cas_kht.hpp`. The aggregate/non-aggregate axis there is the `KV` template
parameter — `Item` (overwrite) versus `Aggr_KV` (accumulate), `cas_kht.hpp:1140-1143`
and `update_cas`. "Regular insertion" therefore maps to `Item`, which is what
`dramblast.c` already implements. Everything below assumes that reading; if a
different one was intended, §4 and §5 need revisiting.

### The test harness

`tests/` builds `dramblast.c` as-is (two shim headers stand in for the only DPDK
headers it includes; the harness builds the table itself rather than calling
`dramblast_init`, which needs hugepages). `make` in that directory currently gives:

```
pass 27   fail 0
```

It also runs clean under `-fsanitize=address,undefined`. Where a finding below
quotes a failure count, that is what the harness reported *before* the fix; the
tests are now regression guards.

---

## 2. Correctness findings

**All findings in this section have been fixed.** The harness now reports
`pass 27 / fail 0` with no expected failures, runs clean under
AddressSanitizer + UndefinedBehaviorSanitizer, and `l2fwd` builds and links
against DPDK 21.11. Each finding below records what it was and what changed.

| | Finding | Fix |
|---|---|---|
| C1 | SIMD key mask selected value lanes | `dramblast.h` mask → `0b01010101` |
| C2 | Out-of-bounds vector lane read | read the value from memory, not the snapshot |
| C3 | Empty-slot test correct only by accident | follows from C1 |
| C4 | Backend LUT `int8_t` sign-extended to −1 | `LookUpTable` → `int64_t` |
| C5 | `inline` without `static`: no link at `-O0`/sanitizers | `static inline` |
| C6 | `v == 0` meant three different things | added `dramblast_result_t.status` |
| C7 | 32-bit hash silently caps capacity | `-c` guard at 2^32 |
| C8 | `aligned_alloc` size not a multiple of alignment | round up via `dramblast_alloc64()` |
| — | `-m` prefix match, unwired `sashstore`, "dropped" that wasn't | see call-site notes |

### C1 — the SIMD key mask selects value lanes instead of key lanes · **critical**
**Fixed:** `dramblast.h:51` now defines `DRAMBLAST_SIMD_KEY_MASK 0b01010101`. The harness's four XFAILs are now passes.


`dramblast.h:51` defines `DRAMBLAST_SIMD_KEY_MASK 0b10101010`. The reference has
`KEYMSK = 0b01010101` (`cas_kht.hpp:61`).

`dramblast_kv_t` is `{uint64_t k; uint64_t v;}` (`dramblast.h:8`), so a
64-byte bucket of four entries lays out as

```
lane   0    1    2    3    4    5    6    7
      k0   v0   k1   v1   k2   v2   k3   v3
```

Keys are in the **even** lanes. `0b10101010` selects the odd ones, so
`_mm512_mask_cmpeq_epu64_mask` at `dramblast.c:155` compares the stored
**values** against the search key. Two independent confirmations in the
reference: `cas_kht.hpp:1038` converts a match position to a slot index with
`_bit_scan_forward(ept_cmp) >> 1`, which is only meaningful for even lanes; and
`cas_kht.hpp:611` reads the value at `bucket[offset+1]`, one lane past the key.

**Measured** (`tests/`, T2): with the shipped mask, **0 of 512** inserted keys
are found. With the identical kernel and the mask corrected to `0b01010101`,
**512 of 512** are found with the right value. Same result for the update case
(T4) and for keys that spill across buckets (T5).

Consequences:

- Every lookup reports a miss, so every packet falls through to
  `dramblast_insert_one` in `dramblast_process_frames` (`dramblast.c:222`). The
  dramblast column of `docs/results.json` is measuring insert, not lookup.
- It is not only a missed hit — it returns **wrong answers**. T6 builds a bucket
  from colliding keys and looks up `0x2266`, a key that was never inserted; the
  shipped kernel returns `0x8457`, which is a different key stored in lane 2.
  In l2fwd that is a packet forwarded to a MAC derived from an unrelated flow.

The fix is one character in `dramblast.h:51`. **Nothing else in this report is
worth measuring until this lands**, because the find path currently does no
useful work.

### C2 — out-of-bounds vector lane read · high
**Fixed:** `dramblast.c` now reads `bucket[offset + 1]` from memory through a `uint64_t *`, matching `cas_kht.hpp:611`. That removes both the undefined lane read and the stale-snapshot race.


`dramblast.c:162`: `result->v = cacheline[(offset + 1)]` with
`offset = __builtin_ctz(key_cmp)`. Under C1's mask `offset ∈ {1,3,5,7}`, so
`offset+1` reaches **8** — element 8 of an 8-element `__m512i`.

Worth being precise about what this is: `cacheline` is the vector **value**, not
a pointer into the table, so this is *not* a buffer overrun into the next
bucket — it is an undefined lane read. Measured across optimisation levels with
a standalone probe:

| `-O` | `cacheline[8]` returns |
|---|---|
| `-O0`, `-O1` | lane 0 (index wrapped) |
| `-O2` | zero |
| `-O3` | an uninitialised **stack address** |

The library ships at `-O3` (`buildtype=release` in `l2fwd/meson.build`), the
worst of the three. Correcting C1 puts `offset` in `{0,2,4,6}` and makes
`offset+1` valid, so this is fixed by the same change. Separately, note that the
reference re-reads `bucket[offset+1]` from memory (`cas_kht.hpp:611`) whereas
this reads the pre-load snapshot, so under concurrent update it can return a
stale value.

### C3 — the empty-slot test is right by accident · medium
**Fixed** as a consequence of C1: `ept_cmp` now tests the key lanes, so it is correct by construction rather than by coincidence. The residual sentinel ambiguity is addressed under C6.


With the wrong mask, `ept_cmp` (`dramblast.c:176`) tests *value* lanes for zero.
It nonetheless gives the correct answer today only because `populate_lut` fills
the backend table with `0xff` and returns early (`conshash.c:21-25`), so no
stored value is ever zero and "value lane == 0" happens to coincide with "slot
empty". This is what keeps the application running at all despite C1. It breaks
the moment a zero value becomes storable. T8 in the harness documents the
residual limitation: a key stored with value 0 is indistinguishable from a miss
through the `dramblast_result_t` API.

### C4 — the backend LUT is `int8_t`, so every flow gets the same value · medium
**Fixed:** `LookUpTable` in `conshash.h` is now `int64_t[TABLE_SIZE]`, so `populate_lut`'s `0xff` reads back as `255` instead of sign-extending to `-1` (harness T10). The early `return` in `populate_lut` is left alone — every flow still maps to one backend, which is a deliberate property of this benchmark, not a bug.


`LookUpTable` is `int8_t[TABLE_SIZE]` (`conshash.h:13`) and `populate_lut` fills
it with `0xff`, then `return`s before the consistent-hashing code
(`conshash.c:21-25`). At `dramblast.c:219`,
`int64_t backend_mac_addr = dramblast_backends[client_hash % TABLE_SIZE]`
sign-extends `0xff` to `-1`, i.e. `0xFFFFFFFFFFFFFFFF`.

So every flow maps to the same pseudo-MAC, the Maglev/consistent-hashing logic
is unreachable, and no flow→backend mapping can be validated end to end.
`maglev.c:28` has the same shape. Combined with C1 this means the harness
currently has **no way to detect a wrong forwarding decision** — which is why C1
survived.

### C5 — `inline` without `static`: the code does not link at `-O0` or under sanitizers · medium
**Fixed:** all five helpers are now `static inline`. `make check-c5` reports no undefined references at `-O0` or under `-fsanitize=address,undefined`. This is what made C8 findable.


`dramblast.c:25,33,47,70,74` declare `dramblast_get_queue_sz`,
`dramblast_push_queue`, `dramblast_pop_queue`, `dramblast_prefetch` and
`dramblast_hash` as `inline` with no `static` and no `extern` declaration. Under
C99/C11 that is an *inline definition* which provides no external definition; if
the compiler declines to inline any call, the reference is unresolved.

**Measured** (`cd tests && make check-c5`):

```
--- -O0 ---
      1 undefined reference to `dramblast_get_queue_sz'
      2 undefined reference to `dramblast_hash'
      1 undefined reference to `dramblast_pop_queue'
      3 undefined reference to `dramblast_prefetch'
      2 undefined reference to `dramblast_push_queue'
--- -O2 with -fsanitize=address,undefined ---
      2 undefined reference to `dramblast_push_queue'
```

It links at `-O1`/`-O2`/`-O3` only because the callers all sit in the same
translation unit and get inlined. The practical cost is that **this codebase
cannot be built with ASan or UBSan**, and cannot be built unoptimised for
debugging. `static inline` is the intended form.

### C6 — unbounded worst case, and three meanings for `v == 0` · medium
**Fixed:** `dramblast_result_t` gained a `status` field (`DRAMBLAST_FOUND` / `_ABSENT` / `_TABLE_FULL`), with `id` narrowed to `uint32_t` so the struct stays 16 bytes. `dramblast_process_frames` now branches on status: it inserts only on `ABSENT`, and on `TABLE_FULL` reports no mapping instead of launching a second full-table scan. A stored value of 0 is now reported as `FOUND` (harness T8).


`dramblast_find_batch_sync` bounds its probe with `count >= ht->len`
(`dramblast.c:168`) and on exhaustion returns `v = 0` — the same value it
returns for "absent". `dramblast_process_frames` cannot tell them apart
(`dramblast.c:218`) and so calls `dramblast_insert_one`, which itself scans up
to `ht->len` slots (`dramblast.c:121`). Worst case is therefore two full table
walks per packet, and a full table livelocks rather than reporting failure.

Credit where due: `cas_kht.hpp` has **no probe bound at all**
(`pop_find_queue`, `cas_kht.hpp:480`, loops `while (retry)` with nothing to stop
it), so this guard is an improvement on the reference and must be carried into
any port. What is missing is only the ability to distinguish the three cases —
absent, table-full, and value-is-legitimately-zero.

### C7 — 32-bit hash truncation caps usable capacity at 2^32 · low
**Fixed:** `main.c` rejects `-c` above 2^32. The hash itself is deliberately left alone — widening it would change every index and make published DRAMHiT numbers incomparable.


`dramblast_hash` uses `_mm_crc32_u64(0, k)` (`dramblast.c:77`), whose result is
32 bits. `hash & (ht->len - 1)` therefore confines every key to the low 4 Gi
slots once capacity exceeds 2^32, silently. `main.c:430` accepts such a `-c`.
Harmless at the 2^29 used by `run.sh`. `cas_kht.hpp:114` has the same
`(uint32_t)` truncation. Note also that the seed differs from the reference: `0`
here versus `0xffffffff` in the reference's inlined path
(`cas_kht.hpp:625,660`) — irrelevant to correctness, relevant to reproducing
published numbers.

### C8 — `aligned_alloc` called with a size that is not a multiple of the alignment · low

Found only *after* C5 was fixed, because until then the code could not be built
under a sanitizer at all.

C11 7.22.3.1 requires `aligned_alloc`'s size to be an integral multiple of its
alignment. `dramblast_init` passed `sizeof(dramblast_ht_t)` (24 bytes) with a
64-byte alignment, and `dramblast_process_frames` passed
`16 * args_len` — not a multiple of 64 unless the burst happened to be divisible
by four. glibc tolerates both; it is still undefined behaviour, and ASan aborts:

```
ERROR: AddressSanitizer: invalid alignment requested in aligned_alloc: 64,
alignment must be a power of two and the requested size 0x18 must be a
multiple of alignment
```

**Fixed:** a `dramblast_alloc64()` helper rounds the size up. The harness now
runs clean under ASan + UBSan with zero diagnostics.

### Call-site issues in `main.c`

These affect how dramblast's results should be read.

- **`mac_addrs[]` is never zeroed** (`main.c:282`). Correctness rests entirely on
  `dramblast_process_frames` writing every `ret[id]`, enforced by an `exit(-1)`
  (`dramblast.c:208`). Left as is: it is correct today, and a per-burst `memset`
  is real datapath cost. **This is the one to revisit first if §5's async port
  is built**, because a partial-result API breaks the invariant silently.
- **`fwded` and `dropped` did not mean what they say.** Packets whose
  `flowhash()` returns 0 are excluded from `args` and were counted as dropped,
  but `rte_eth_tx_burst` transmits all `nb_rx` regardless, so nothing was
  actually dropped. **Fixed:** the counter is renamed `unmapped` and the display
  says "Packets unmapped". Forwarding behaviour is unchanged — only the label
  was wrong, and changing what is transmitted would have changed throughput.
- **`-m` used a prefix match**, `strncmp(optarg, "dramblast", 9)`, which would
  silently swallow any longer mode name such as `dramblast_async`.
  **Fixed:** exact `strcmp`.
- **`l2fwd_mac_updating` writes 8 bytes over a 6-byte field**
  (`main.c:256-260`), clobbering the first two bytes of `src_addr` before
  `rte_ether_addr_copy` restores them. Works, but only by ordering.
- **`sashstore` mode was unwired**: `sashstore_init()` ran but `l2fwd_main_loop`
  has no branch for it, so `-m sashstore` silently measured the `none` path and
  reported it as sashstore. **Fixed:** it now `rte_exit`s with an explanation.
  Wiring it properly is not possible without changing `pktgen`, which emits
  plain UDP rather than the memcached-style payloads `sashstore_process_frame`
  expects.

---

## 3. Performance findings

All estimates, per §1. Ordered by expected impact.

### P1 — `malloc`/`free` on the datapath

`dramblast_process_frames` calls `aligned_alloc` at `dramblast.c:203` and
`free` at `:231`, once per burst per call. Use a per-lcore array sized
`MAX_PKT_BURST`, or have the caller pass the buffer in. With P4 the array
disappears entirely.

### P2 — the find queue is deeper than the hardware can sustain

`DRAMBLAST_FIND_QUEUE_SIZE` is 64 (`dramblast.h:48`). With `fn = 64` the push
loop at `dramblast.c:140` drains `args` completely before the first pop, issuing
64 back-to-back `prefetcht1`. A prefetch issued when the fill buffers are
saturated is **dropped, not queued**, so with ~10–12 LFBs roughly the first
dozen take effect and the remainder are no-ops; the rest of the burst degrades
to demand-load MLP bounded by the reorder buffer.

The queue depth is the memory-level-parallelism knob, and it should be set near
the LFB count, not far above it. Suggested sweep: 8, 16, 32, 64.
`cas_kht.hpp:66` defaults to 8; upstream DRAMHiT uses 16–64.

### P3 — per-lcore queue state false-shares

`dramblast_queue_t` (`dramblast.h:35`) is 20 bytes padded to 24, and
`dramblast.c:281` allocates a dense `[MAX_CPU]` array of them. Two to three
lcores' `find_queue_head`/`find_queue_tail` therefore share a cacheline, and
every push and pop — several per packet — dirties a line another core is
spinning on. Needs `__rte_cache_aligned`.

It is tempting to blame the ≥6-core collapse in `docs/results.json` on this.
Resist it: maglev collapses too (cores 8 and 10), and maglev does not share this
structure, so a common external cause — NIC queue configuration, pktgen
saturation, RSS distribution — is at least as likely. Fix the false sharing
because it is a defect, then re-measure before attributing anything to it.

### P4 — the miss path throws away work it already did

On a miss the find loop has loaded the bucket, computed `ept_cmp`, and knows the
terminal `idx`. `dramblast_process_frames` then calls `dramblast_insert_one`
(`dramblast.c:86`), which re-hashes the key and re-walks from the *initial*
bucket. The reference instead jumps straight to the free slot:
`idx += _bit_scan_forward(ept_cmp) >> 1` (`cas_kht.hpp:1037-1038`).

Returning the terminal `idx` and `ept_cmp` in `dramblast_result_t` removes a
hash and a full re-probe on every miss. That is the dominant cost during table
warm-up — and, while C1 is unfixed, on *every single packet*.

### P5 — the insert prefetch has zero distance

`dramblast.c:117`: `if (!(idx & 0x3)) dramblast_prefetch(ht, idx)` prefetches
the line dereferenced on the very next loop iteration. It cannot hide any
latency. Drop it, or prefetch `idx + 4`.

### P6 — the insert probe is scalar

`dramblast_insert_one` walks one slot per iteration with a fresh `kv->k` load,
while the find path scans four slots from a single loaded cacheline. Port the
SIMD prologue of `cas_kht.hpp:1018-1038`.

### P7 — prefetch locality contradicts its own comment

`dramblast.c:60` defines `PREFETCH_T1` as L2/L3 and the comment at `:67` says T0
is "standard for items you are about to access immediately", yet `:71` uses T1.
For an 8 GiB table with dozens of lines in flight, L2 is arguably the better
choice — L1 cannot hold 64 table lines alongside packet headers — but the code
and its comment disagree, so it reads as accidental rather than chosen.
`cas_kht.hpp:986-1000` exposes this as an explicit knob; it deserves an A/B.

### P8 — table geometry is re-derived per call and per probe

`ht->len` is reloaded and the mask recomputed at `dramblast.c:82`, `:110`,
`:178-180`. Precompute one `bucket_mask` at init, as `cas_kht.hpp:114` does with
`HT_BUCKET_MASK`.

### P9 — three pointer chases per queued item

`dramblast_get_queue_sz`, `dramblast_push_queue` and `dramblast_pop_queue` each
re-derive `&ht->queues[id]` and reload head/tail/size; `get_queue_sz` sits in a
`while` condition and so reloads on every iteration. Hoist the queue pointer
once per call and keep head and tail in locals for the whole function — exactly
what the reference does (`cas_kht.hpp:575-576`).

### P10 — queue entries are half as dense as they need to be

`dramblast_queue_item_t` (`dramblast.h:28`) is 28 bytes padded to 32. `idx` fits
in 32 bits (C7 already caps capacity there) and `visit_count` in 16–24, so 16
bytes and four entries per cacheline are achievable. Each entry is touched
twice, and at depth 32–64 the queue is itself a working set.

### P11 — no `restrict`

`args`, `results` and `ret` are unqualified, so the compiler must assume result
stores alias `args[]` and `ht->table`.

### P12 — dead check on the datapath

`if (len != args_len) { printf(...); exit(-1); }` (`dramblast.c:208`) is
unreachable: `dramblast_find_batch_sync`'s loop runs until
`result_head == args_len`.

### P13 — integer division per miss

`client_hash % TABLE_SIZE` (`dramblast.c:219`) with `TABLE_SIZE 65537`
(`conshash.h:11`), not a power of two — a 64-bit division, tens of cycles, on
every miss.

### P14 — the intermediate `results[]` array is avoidable

It exists only to carry data between two loops in the same function. With P4 the
fixup folds into the find loop and the array, and P1's allocation, both vanish.

### P15 — LTO is disabled for the library

`l2fwd/libsashstore/meson.build` sets `b_lto=false` while the top-level project
sets `b_lto=true`. So `flowhash` — declared `__inline__` in `packettool.c:48`
and called once per packet from `main.c:323` — cannot be inlined into the RX
loop, and `dramblast_process_frames` stays an opaque cross-TU call.

---

## 4. What the async contract in `cas_kht.hpp` actually is

Needed to judge §5. The function to port is `find_batch_inline`,
`cas_kht.hpp:563-693`.

- **Pop one, push one.** Each input key pops exactly one *resolved* item and
  pushes one new item, so the ring sits permanently at `find_queue_sz - 1`.
- **Results belong to earlier calls.** What a call writes into `vp` are the
  keys submitted roughly one queue-depth ago. This is the whole point, and the
  whole problem for l2fwd.
- **`goto retry` (`:646`)** re-pushes a reprobing item at head and pops another
  from the tail, so one input may consume several queue entries.
- **`key_id` is the only correlation handle** (`:612`). Results are reordered
  relative to input; array position means nothing.
- **The fast path requires a full ring** (`:570`), so the first calls take the
  slow path and the pipeline has a warm-up transient.
- **`flush_find_queue` is mandatory** (`:443`) or a full queue of lookups is
  silently lost at the end.
- **Misses are dropped on the floor** (`:648,683`): `not_found++` and
  `vp.first += kp.size() - not_found`, with no `FindResult` emitted. The caller
  learns of a miss only by an id never appearing.
- **`vp_result` is initialised to `vp.second`, not `vp.second + vp.first`**
  (`:578`), inconsistent with every other write site (`:963`). A port should
  write at `values + num_values` everywhere.
- **The `__int128` CAS (`:1082`)** forces the queue entry's first 16 bytes to be
  `{key, value}` in that order, 16-byte aligned.
- **No probe bound anywhere** — see C6.

---

## 5. Async port feasibility for l2fwd

### 5.1 What the synchronous version currently leaves on the table

Two separable components.

**Pipeline ramp.** `dramblast_find_batch_sync` fills and drains within one call.
The first pop cannot retire until its line arrives, ~270 cycles after the first
prefetch issues. Amortised over 64 packets: **~4 cycles/packet**. Small.

**Serialisation against the rest of the loop.** This is the larger one. In
`l2fwd_main_loop` (`main.c:292-373`) the probe phase sits strictly between
`rte_eth_rx_burst` + `flowhash` and `rte_eth_tx_burst`. Those neighbours are
several thousand cycles of work per burst that the probe stalls cannot overlap
with. An async design moves burst *N*'s probes into the window where burst *N+1*
is being received and hashed; by the time the pops happen the lines are in
L2/L3 rather than DRAM. Recoverable stall is on the order of
**10–15 cycles/packet** under §1's assumptions.

Call the upper bound **~15 cycles/packet of ~150, i.e. ~10%.**

### 5.2 What async has to pay

| Cost | Estimate |
|---|---|
| Slot alloc + free (free-list stack, L1-resident) | ~4 cyc/pkt |
| Store mbuf pointer on submit, load it on completion | ~4 cyc/pkt |
| TX staging array instead of forwarding `pkts_burst` directly | ~2 cyc/pkt |
| Deferred packet-header touch | 0 to ~20 cyc/pkt — see below |

The last row is the one that decides the answer and the one I cannot settle
without measuring. Today `flowhash` and `l2fwd_mac_updating` touch the same
cacheline a few hundred cycles apart, so it is L1-hot. Async widens that gap to
a full burst or more. Footprint check: 64 packet headers ≈ 4 KiB, two bursts in
flight ≈ 8 KiB, plus ≤64 table lines ≈ 4 KiB, against 32–48 KiB of L1d. It
*should* survive, but random table lines evict by set, mbuf metadata adds more,
and if it does not survive the MAC write becomes an L2 hit and the whole benefit
is gone.

### 5.3 Verdict

**Roughly break-even at `MAX_PKT_BURST 64`** — an upper bound of ~15 cyc/pkt
recovered against ~10 cyc/pkt of bookkeeping plus an unquantified cache risk.
The margin sits inside the error bars of §1's assumptions, so the honest answer
is that the async port is **not clearly worth it for throughput at this burst
size**, and the case for building it is as a research artifact rather than as an
optimisation.

Where it would clearly win:

- **Small effective batches.** At burst 8 the ramp alone is ~34 cyc/pkt. If the
  workload ever runs below line rate, or `MAX_PKT_BURST` is reduced, async
  dominates.
- **High load factor.** Long reprobe chains lengthen the sync drain tail, which
  async absorbs into the next burst.
- **As the platform for a fair DRAMHiT comparison.** If the research goal is to
  reproduce the published DRAMHiT pipeline rather than to make l2fwd faster,
  then the async structure *is* the artifact, and §5.3's throughput verdict is
  beside the point.

And the precondition: **none of this is measurable until C1 is fixed.** Today
the find path returns a miss for every packet, so any sync-vs-async comparison
would be comparing two ways of doing nothing.

### 5.4 Packet buffer pool design

Per-lcore, in `struct lcore_queue_conf` (`main.c:80-86`) — whose `tx_buffer[]`
field is dead space already, allocated at `main.c:629` and never used because
`l2fwd_simple_forward` is never called.

```c
#define DB_SLOTS 256                      /* pow2 >= queue_sz + 2*MAX_PKT_BURST */
struct db_pending {
  struct rte_mbuf *slot_mbuf[DB_SLOTS];   /* 2 KiB */
  uint16_t         free_stack[DB_SLOTS];  /* 512 B */
  uint16_t         free_top;
};
```

Both arrays stay L1-resident. `args[j].id` becomes the slot id rather than the
burst index; `dramblast_result_t.id` is already `uint64_t`, so no format change.

**Use an explicit free-list, not a monotonic `seq & MASK`.** The tempting cheap
version — hand out `seq++ & (DB_SLOTS-1)` and skip the free list — is unsound
here. A reprobing entry is re-pushed at the ring head (`cas_kht.hpp:646`), so its
lifetime is *not* bounded by the queue depth; a wrapping counter can reuse a
slot that is still live, which leaks an mbuf and forwards a packet to the wrong
MAC. A LIFO free stack is exact for about four extra cycles.

### 5.5 The hazards that make this more than a library swap

- **TX decouples from RX.** Completed packets come from a staging array, not
  `pkts_burst`. In steady state N results return per N submitted, so TX burst
  size still tracks RX burst size.
- **Quiescence deadlock.** If RX goes quiet, up to `find_queue_sz - 1` packets
  sit in the ring indefinitely. The `nb_rx == 0` branch (`main.c:368`, currently
  just `rte_pause()`) must flush both queues.
- **Mempool sizing.** `nb_mbufs` (`main.c:543-550`) should gain
  `DB_SLOTS * nb_lcores` explicitly. The existing `+= 8192` slack covers it, but
  by accident.
- **The timing instrumentation stops being meaningful.** `hash_tsc`
  (`main.c:305,355`) brackets a region that, post-port, contains work belonging
  to several different bursts. The "Cycle per fwd packet" figure
  (`main.c:212-215`) would be wrong in a way that flatters or penalises async
  arbitrarily. This must be re-instrumented *before* any comparison, or the
  experiment is invalid.
- **`mac_addrs[]` staleness** (see §2 call-site notes) becomes live: with
  partial results, not every slot is written every iteration.
- **Ordering.** Results reorder across flows, but two packets of the same flow
  share a key and therefore a probe path, so per-flow order is preserved. Fine
  for L2 forwarding.

### 5.6 Insert on miss

The reference drops misses entirely (`cas_kht.hpp:648`), which l2fwd cannot do —
it needs the id both to forward the packet and to insert the flow. Recommended
shape:

1. Emit an explicit `(v = 0, id)` result for misses, as `dramblast.c` already
   does. This is a deliberate deviation from the reference and should be
   documented as such.
2. Take the MAC from the backend LUT immediately, so forwarding is never delayed
   by the insert.
3. Push the insert into a **second async queue**, ported from
   `insert_batch_inline` (`cas_kht.hpp:270-429`).

The second queue needs a threshold or periodic flush. Misses are frequent during
warm-up and rare afterwards, so in steady state the insert queue fills slowly and
entries can sit for a long time — and until an insert lands, every subsequent
packet of that flow misses again and enqueues a duplicate. Duplicates are
harmless (the insert path updates in place, `cas_kht.hpp:1025-1031`) but wasteful.

---

## 6. Recommended order, and what to measure

1. ~~Fix the correctness findings~~ — **done**; see §2. `docs/results.json`
   should be regenerated, since the dramblast column there was produced by a
   build whose find path never matched a key.
2. **Take the cheap sync wins: P1, P2, P3.** These are small, local, and may
   capture most of what the async port promises. Re-measure after each.
3. **Then build the async port** if the goal is DRAMHiT reproduction, with queue
   depth and prefetch locality as runtime knobs, and compare across several
   burst sizes and load factors rather than only at 64.

Since nothing here was profiled, the counters that would settle the open
questions: `MEM_LOAD_RETIRED.L1_MISS` / `L2_MISS` / `L3_MISS`;
`CYCLE_ACTIVITY.STALLS_L3_MISS` for exposed memory stall;
`L1D_PEND_MISS.PENDING` divided by `L1D_PEND_MISS.PENDING_CYCLES` for achieved
MLP, which directly tests P2; `SW_PREFETCH_ACCESS.T1` against actual fills for
dropped prefetches; and `MEM_LOAD_RETIRED.L1_HIT` on the MAC-write site to
settle the §5.2 cache question. Sweeps: queue depth {8,16,32,64}, burst size
{8,16,32,64}, load factor {0.1 … 0.9}.
