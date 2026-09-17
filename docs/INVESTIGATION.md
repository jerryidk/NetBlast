# Investigation: performance collapse at specific queue-pair counts

Diagnosis log + every machine modification. Pruned 2026-09-17: superseded
intermediate reasoning removed, reversals kept where they stop a bad number
coming back. Section numbers are stable — commit messages and code comments
cite them.

---

## 1. What the data shows

`docs/results.json`, Mpps (unit confirmed `l2fwd/main.c:207`):

| queues (`-q`) | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| maglev avg | 13.2 | 27.7 | 44.5 | 57.0 | 72.1 | 85.1 | 81.3 | **1.7** | 69.5 | **2.1** |
| dramblast avg | 19.9 | 39.2 | 60.0 | 77.1 | 74.2 | **1.0** | **1.0** | **1.1** | **1.0** | **1.0** |

Not a dip. Near-linear scaling, then ~70x fall to a floor. dramblast never
recovers after 6. maglev `9` = `min=1.0, max=115.0, avg=69.5` — one run
oscillating floor-to-full-speed. Intermittent failure, not a scaling limit.

Floor value quantized: `samples` is `uint32_t *` (`main.c:166`), `mpps` is
`double` (`main.c:208`). Each sample truncates. `min=1.0` means [1,2) Mpps.

---

## 2. Diagnosis

### Hypothesis 1: NUMA or hyperthread boundary — KILLED

`run.sh:22-24` selects even CPUs. Topology kills it: single socket, 28 physical
cores, 1 NUMA node, sibling of CPU *k* is *k+28*. CPUs 0-27 all distinct
physical cores. `-q 10` reaches only CPU 18. All four E810 ports `numa_node=0`.

### Hypothesis 2: queue-to-lcore mapping short by one — CONFIRMED

`run.sh:22-24` builds `-l` as **N** lcores, passes `-q N`. `main.c:513-538`
assigns one lcore per queue but skips the main lcore (`main.c:523`) — leaves
**N-1** workers for N queues.

```
$ ./run.sh 4 dramblast
Lcore 2 assigned to Port 0 Queue 0
Lcore 4 assigned to Port 0 Queue 1
Lcore 6 assigned to Port 0 Queue 2
EAL: Error - exiting with code: 1
  Cause: Not enough cores
```

Reproduced at N=1, 2, 6. **Committed `run.sh` cannot run at any core count.**
Every version in git history has the same `-l`/`-q` pairing. Not a regression.

Control: `-l 0,2,4,6,8` with `-q 4` starts clean, assigns all four queues.

### Consequence: `results.json` came from different code than HEAD — CONFIRMED

Committed code always dies, so it cannot have produced ten populated rows.

`git log -S "SKIP_MAIN" -- l2fwd/main.c` → `91d2c14`, the *same commit* that
added `docs/results.json`. Parent `7e11fc8` has no main-lcore skip and launches
`CALL_MAIN`, stats timer inside the worker loop.

Data came from N lcores serving N queues. `91d2c14` changed the threading model
and committed results from the old model in the same commit.

### Control: is the floor a crashed run? — NO

Link up, no generator, 32 s: `Minimum: 0 / Maximum: 0 / Average: 0.00`. Dead
runs report zero. The `1.0` rows are genuine forwarding at ~1-2 Mpps.

### Reproduction with corrected invocation: collapse does NOT reproduce

N+1 lcores for N queues, `main.c` untouched. Generator on `node1`, `-l 0-16`,
`-r 2097152`, `Max Flows: 16777216`, offering **93.28 Mpps / 100.00 Gbps** at
110-byte frames.

| queues | dramblast avg | committed | maglev avg | committed |
|---|---|---|---|---|
| 1 | 27.84 | 19.9 | 16.81 | 13.2 |
| 4 | 92.68 | 77.1 | 68.68 | 57.0 |
| 6 | 92.84 | **1.03** | 92.71 | 85.1 |
| 8 | 92.90 | **1.06** | 92.84 | **1.68** |
| 10 | 92.94 | **1.03** | 92.97 | **2.06** |

No collapse at any queue count, either mode. Both reach line rate and stay.
Earlier 72 Mpps sweep agrees; retained in `docs/results_reproduced.json` under
`capped_72mpps`.

### Control: generator artifact? — NO

Repeated at 2 TX cores (`-l 0-4`) instead of 8, holding offered load and flow
count constant. `-r` is per TX core, so `-r 8388608` x 2 = 16,777,216 flows,
matching the 8-core run.

dramblast agrees within 0.15% at every point. maglev worst disagreement 1.4% at
q=5, its steepest unsaturated point.

Third independent confirmation of no collapse: 72 Mpps, line rate 8 TX, line
rate 2 TX.

**Use `-l 0-4` for future runs.** Indistinguishable results, generator host load
average ~2.6 versus ~14.

### Per-queue capacity

* **dramblast** ~27.8 Mpps per queue pair, saturates link at q=4
* **maglev** ~17.1 Mpps per queue pair, saturates at q=6

Consistent with per-packet cost where burst is pinned at 64: dramblast 55-56
cycles/packet against maglev 103. 1.85x.

### A real queue-count-dependent cost

| queues | dramblast cyc/pkt | dramblast batch | maglev cyc/pkt | maglev batch |
|---|---|---|---|---|
| 1 | 55 | 64 | 103 | 64 |
| 3 | 56 | 64 | 103 | 64 |
| 5 | 82 | 15 | 103 | 64 |
| 8 | 117 | 1 | 122 | 7 |
| 10 | 178 | 3 | 131 | 4 |

Fixed offered load over more queues shrinks each queue's burst. dramblast's
per-packet cost then rises **3.2x** (55 → 178); maglev's **1.3x** (103 → 131).

Cost is per-**burst**, not per-packet: dramblast stays flat at 55-56 while burst
stays 64 (q=1-3), climbs only once burst collapses. maglev, with no per-burst
work of this kind, stays nearly flat under identical burst collapse.

Curves cross at q=7. A constant offset would not do that.

`docs/data.txt`'s original 56-core run: **4978** cyc/pkt dramblast against
**491** maglev — same asymmetry, an order of magnitude further along.

### The per-burst model, and what two reversals cost

Fitted `ticks_per_pkt = W + C/batch`.

On turbo data the fit was unstable — `C = 66` ticks over all points, `C = 387`
from the two extremes, and maglev fitted a *larger* `C` (116) than dramblast
despite having no per-burst allocation. Conclusion at the time: the data cannot
settle the attribution.

**That was true of the turbo data only.** Refitted on the pinned arm, where TSC
ticks are core cycles and there is no frequency drift to absorb:

| estimator | dramblast `C` | maglev `C` |
|---|---|---|
| least squares, all 10 points | **771** cycles/burst | 71 |
| least squares, 4 post-knee points | 739 | — |
| two-point extremes | 794 | 98 |
| `R^2` (all points) | **0.9946** | 0.68 |

Three estimators within **7%**, against a 5.9x swing on turbo data. The
instability was frequency drift, not the model. **dramblast's per-burst cost is
11x maglev's.** maglev's low `R^2` is not failure — its cost is flat, so there
is little variance to explain, which is the asymmetry stated numerically.

**Pre-registered prediction, half failed, reported not patched.** Predicted:
frequency-corrected refit shrinks `C` in both modes, more in maglev. Outcome —
maglev 116 → 71 as predicted; dramblast 66 → **771**, wrong by an order of
magnitude. The "drift absorption" explanation is discarded. The turbo fit was
not a biased estimate but an unstable one whose least-squares value happened to
land low. The prediction was also badly posed: the pinned arm is a different
dataset, not the turbo data corrected.

**Two reversals worth keeping, because both were plausibility traps.**

1. `C = 771 cycles @ 2.100 GHz = 367 ns/burst` against a published glibc
   `aligned_alloc`+`free` hot-tcache cost of 20-40 ns said the allocator could
   be at most ~11% of the cost. **Wrong reference.** This call asks 64-byte
   alignment, which in glibc 2.33 never reaches tcache and goes through
   `_int_memalign` under the arena lock. Measured directly: **462-527 cycles,
   64-73% of the per-burst cost** (§5.13). The measurement was correct and was
   compared against the wrong constant — which no measurement hygiene catches.
2. On that basis the hoist was called a *weak* test and the amplification arm
   promoted to primary. Also wrong. The hoist resolved the allocator
   immediately: 8.25 ticks/packet at burst 64 = 527 ± 48 cycles/burst. The
   "~60 cycles per pair" expectation understated by 8x (a pair costs ~509).

The earlier `C = 66` matching an allocator round trip "to the nanosecond" is the
plausibility trap this document records three times: a wrong method yielding a
right-looking number, where plausibility is what stops the checking.

### Controls carried by the hoist experiment

Decided in advance:

1. **Structural verification.** `objdump -d` the rebuilt binary, confirm no
   `call` to `aligned_alloc`/`malloc`/`free` remains in
   `dramblast_process_frames`. Source-level reasoning is not evidence about
   generated code.
2. **Signature check.** Removing a per-burst cost must reduce the measurement
   more at small batch than large. Uniform drop across all q would indicate
   something else changed.
3. **Amplification as positive control.** A variant with a second redundant
   pair per burst. If adding one produces no measurable rise, the measurement
   cannot detect removing one, and a null result is uninformative.

Control 3 converts "we saw no effect" into "we saw no effect and we know we
could have."

**Pre-registered downgrade threshold.** A genuine per-burst removal must reduce
instructions per packet proportionally to 1/batch — near-invisible at q=1, large
at q=9-10. Uniform fall across all q = code generation, not mechanism removal.
If instructions/packet at q=1 move more than **2%**, downgrade the hoist from
proof to corroboration and let the amplification arm carry the result.

### Open leads

1. **maglev probe-chain divergence.** `maglev_hashmap_insert` (`hashmap.c:27-53`)
   probes linearly up to `CAPACITY` = 512M slots; `maglev_hashmap_get`
   (`hashmap.c:55-71`) stops at the first zero key. Under concurrent CAS inserts
   a lookup can miss an existing key, re-insert it, lengthen the chain.
2. **Per-burst heap traffic in dramblast.** Resolved — see §5.13.
3. **Table backing differs between modes.** dramblast `mmap(MAP_HUGETLB |
   MAP_HUGE_1GB)` (`dramblast.c:253`); maglev `aligned_alloc(4096, ...)`
   (`maglev.c:43-44`). Both measured: dramblast takes nine 1 GiB pages, maglev
   one. maglev's table is **not** 4 KiB-backed — `AnonHugePages` reads
   8,515,584 kB during a maglev run against a 36,864 kB baseline, so it is fully
   THP-backed at 2 MiB. Real asymmetry is **1 GiB vs 2 MiB** — 8 TLB entries
   against 4096, 512x — not the 262144x earlier notes implied. Depends on THP
   being `always`, which `reserve_hugepages.sh` sets as a side effect (§3.2).
4. **False sharing in dramblast per-lcore queues.** `dramblast_queue_t`
   (`dramblast.h:35-40`) is 24 bytes, unpadded, indexed by `lcore_id` rather
   than a dense index (`main.c:333`). At 24-byte stride, lcores 2/4, 8/10,
   10/12, 16/18 share 64-byte lines. Suggestive; cannot explain 74 → 1 Mpps.

### Measurement-harness defects — quantified, worked around

Four defects in l2fwd's own reporting, found by reading then measured against the
per-second sample series in the run logs.

**1. Truncation.** `samples` is `uint32_t *` (`main.c:166`), a `double` Mpps value
stored into it (`main.c:208`). Every sample `floor()`ed. Fixed ~0.5 Mpps downward
bias: −0.3% at 93 Mpps, **−4.6% at 17 Mpps** — worst exactly where the unsaturated
per-queue slope is measured.

**2. Cold first sample.** Sample 0 is 12-19% below steady state and always sets the
reported `Minimum`. The published min/max range is a startup artifact.

1+2 give an exact model of the reported average. dramblast q=10 samples are `91.97`
then `93.28` ×30; predicted mean of floor()ed values `(30*93 + 91)/31 = 92.94`;
harness reports **92.94**.

| run | sample 0 | steady (median of warm) | reported avg | error |
|---|---|---|---|---|
| dramblast q=1 | 23.30 | 28.62 | 27.84 | -2.71% |
| dramblast q=10 | 91.97 | 93.28 | 92.94 | -0.36% |
| maglev q=1 | 12.89 | 17.62 | 16.81 | **-4.60%** |
| maglev q=10 | 90.34 | 93.28 | 92.97 | -0.33% |

**3. Interval assumed exactly 1 s.** `double t_s = timer_period / rte_get_timer_hz()`
(`main.c:205`) is integer division, denominator exactly 1.0 even when the real
interval drifts longer. A long interval inflates the rate — how maglev q=10 reports
`Maximum` 95 Mpps on a link whose ceiling is 93.28.

**4. Batch size integer-divided.** `main.c:196` computes
`(agg.rx - prev_agg.rx) / (agg.rx_cnt - prev_agg.rx_cnt)`, both `uint64_t`,
truncates. Harmless at large batch (true 64.9 → 64, ~1.4%); at small batch a true
1.9 → 1, a 47% error. Also a single one-second delta, not a run mean — why the
series reads batch 1 at q=8 and batch 6 at q=9. The per-burst term `C/batch` has
almost all its leverage at small batch, so the regressor is least trustworthy
exactly where the model most depends on it. Future fits must report batch as a
`double` accumulated over the whole run.

**Pattern: three of four are integer truncation applied to a non-integer** —
`samples` (`main.c:166`/`:208`), `t_s` (`main.c:205`), batch (`main.c:196`). Each
degrades gracefully and stays invisible at large magnitudes, which is why all three
survived. Assume any other derived statistic in this harness has the same shape
until checked. Also: no bounds check on `SAMPLE_SIZE` against `TOTAL_SAMPLES`
(`main.c:208`).

**Worked around without re-running.** The per-second `"%.2f Mpps"` lines
(`main.c:207`) print *before* truncation, so full precision is recoverable from
existing logs. `extract_results.py` records `steady_mpps` — median of warm samples,
excluding sample 0 — immune to all three. All headline figures use it.

Corrected per-queue capacity: **dramblast** ~27.8 Mpps/queue pair (q=1 rises 27.84
→ 28.62); **maglev** ~17.4 (16.81 → 17.62). maglev's corrected series is
near-perfectly linear — 17.62, 34.99, 52.34, 69.59, 86.94 (increments 17.37, 17.35,
17.25, 17.35) — which the biased data obscured.

### "Cycles per forwarded packet" is TSC ticks, not core cycles

`main.c:305`/`:355` use `rte_rdtsc()`. This CPU reports `constant_tsc` /
`nonstop_tsc` with TSC pinned at 2.1 GHz nominal (`rte_get_tsc_hz: 2100000000`)
while cores boost to 3.7 GHz under `powersave` with `no_turbo=0`. So "Cycle per fwd
packet" is **TSC ticks, i.e. elapsed time**.

**Retraction: `scaling_cur_freq` cannot be used for this, on this box.** Sampled
while a sweep busy-polled CPUs 0-14, an *idle* core read **3.63-3.70 GHz** —
indistinguishable from busy cores — while `perf` counted **506,165 cycles/s** on
that same core. Under `intel_pstate` active mode, `scaling_cur_freq` reports the
P-state *request*, not delivery; `cpuinfo_cur_freq` does not exist in that mode. A
median over cores would not rescue it: every core read the same wrong number, so
the median is wrong too, and looks stable and plausible.

Working method, `sweep.sh:111`, counts cycles directly:

    sudo perf stat -e cycles -C <worker cpus> -x, -- sleep 8

Cycles / wall-time / core *is* delivered frequency here: DPDK busy-polls, workers
sit at 100%, no populate phase. Worker cores only — lcore 0 runs the stats loop.

**Resolved by configuration, not correction.** §3.4b pins every core to
2,100,000 kHz — this SKU's `base_frequency` *and* exactly the invariant TSC rate.
**TSC ticks are core cycles**, the 1.762x factor is 1.000, the caveat disappears
rather than being estimated away. Verified by perf: 33,482,579,855 cycles over 8 s
on 2 cores = **2.0927 GHz**; an independent peer session measured 2.0843 GHz; sweep
instrumentation reports 2095 MHz.

| comparison | frequency held? | verdict |
|---|---|---|
| dramblast vs maglev at fixed q | yes, same core count | clean |
| dramblast q=1 vs q=10 | no, 1 vs 10 busy cores | inherits drift; 3.2x is an upper bound |
| throughput in Mpps, any axis | n/a — packets over wall-clock | unaffected |

Figures label the axis "TSC ticks per forwarded packet (2.1 GHz)", not "cycles".

Also: the generator does not match its own documentation. `docs/data.txt` (deleted
in `db23607`, recoverable via `git show f56827b:docs/data.txt`) describes an 8-core
generator; `pktgen/run.sh` uses `-l 0-2`, yielding `rte_lcore_count()-1` = 2 workers
split 1 TX / 1 RX (`pktgen.c:308`).

---

### Pre-registered: what the pinned re-baseline must show

Written **before** the 2.1 GHz sweep, against turbo-era `linerate_2tx_instr`.

**A units correction that inverts the naive expectation.** A peer session predicted
per-packet cost would *fall* at 2.1 GHz, memory latency being fixed in ns.
Right in *core cycles*, which is what that session measures. Backwards here:
`rte_rdtsc()` counts the invariant TSC, 2.1 GHz in **both** conditions, so a tick is
a fixed quantity of *time*. So ticks per packet must **rise or stay flat, never
fall.** If any point falls, the rig is wrong, not the chip.

With `r = 3.7/2.1 = 1.762` the clock ratio (an upper bound):

| the cost is... | scales by | because |
|---|---|---|
| CPU-bound work (instructions retired) | up to `r` | fewer instructions per unit time at a lower clock |
| memory / DMA latency | `1.0` | fixed in nanoseconds, unaffected by core clock |

1. **Every point rises or holds.** Rig check; failure invalidates everything below.
2. **dramblast's excess scales like CPU work.** Its q=10 excess over its own q=1
   plateau is `185 − 58 = 127` ticks. Allocator → scales near `r`, landing near
   `127 × 1.7 ≈ 216`. Memory stalls in disguise → stays near 127.
3. **maglev's plateau scales less than dramblast's excess**, being the more
   latency-exposed.

**Falsifier:** dramblast's excess growing by less than maglev's plateau does — the
cost would be latency-bound, the allocator the wrong candidate, and the hoist should
not be run as specified.

**AMENDMENT 1, before the turbo arm ran: `r` is a function of q.** Delivered
single-core turbo measures **3.65-3.68 GHz**, so `r ≤ 1.74` even at q=1; **3553 MHz**
was observed at high core counts, `r ≈ 1.69`. Testing against a fixed 1.762 would
make genuinely CPU-bound work look sub-`r` at exactly the high queue counts where
the per-burst cost lives, killing a correct hypothesis. Criterion 2 is evaluated
against `r(q)` from the turbo arm's own recorded `freq_mhz`. **The absolute target
"~216 ticks" is withdrawn**; the test is `excess_pinned / excess_turbo` against that
queue count's measured `r(q)`.

**AMENDMENT 2: the comparator changes.** Pinned against the older
`linerate_2tx_instr` is not a single-variable contrast — `irqbalance` stopped,
C-states disabled, THP `defrag` → `madvise`, cpuset partition introduced in between,
none measured. Evidence it matters: pinned q=2 dramblast reads 105 ticks against the
old turbo arm's 58, ratio **1.81 — above the `r ≤ 1.762` ceiling**, impossible under
a clean contrast. **Pinned-vs-turbo is made against the new turbo arm;
`linerate_2tx_instr` is retained only as historical record.**

**AMENDMENT 3: q-for-q is the wrong axis.** At 2.1 GHz the forwarder is ~1.74x
slower and stays oversubscribed to a higher queue count: pinned dramblast is still
at `batch=64` and ~103 ticks at q=4 where the turbo arm's bursts had already shrunk
(66 ticks, rising). Comparing at equal `q` compares different burst sizes — the one
variable the hypothesis is about. Fix, and a sharper test: plot **core cycles per
packet against 1/burst-size**, both arms and modes, converting each turbo point by
its own `r(q)`. Under `cycles/packet = P + C/B` this is a straight line, intercept
`P`, slope `C`. The test is a **collapse**: all-CPU-bound work puts both arms on the
same line, since core cycles are clock-invariant for CPU work. Any vertical
separation is precisely the memory-latency fraction. Needs no assumption about `r`.

**AMENDMENT 4, post-hoc — flagged as such. Criterion 1 is mis-specified.** A
violation in the data made me notice, not foresight: at q=9 the pinned arm reads 149
ticks against the older turbo arm's 164, which criterion 1 declares impossible. "A
lower clock cannot make anything finish sooner" holds only *at equal work*. At q=9
the pinned arm ran `batch=14` against the turbo arm's `batch=6`, so it does **less
work per packet**. Criterion 1 is valid only at equal burst size — the low-q cells
where both are pinned at `batch=64`. Reported in both forms: as written (which it
fails) and restricted to equal-burst cells (its sound form). The unrestricted form
stays in the output, because a pre-registration quietly narrowed after it fails is
worth nothing.

**Control the collapse test requires: are the arms instruction-matched?** The gap is
read as the memory-latency fraction, valid only if both arms execute the same work.
A peer session checked this on its own arms — having just called the equivalent
result its strongest — and found a deterministic **0.19 instructions per key**
asymmetry between machine states, reproducible to four decimals across four trials.
Origin unestablished. At its IPC that is ~0.073 cycles/op against a gap precision of
~0.08 cycles/op: *the same order as the effect*, turning "twelve points on zero" into
"zero to within the precision at which the arms are matched". **Before any gap in
the collapse figure is read as latency, `perf stat -e instructions,cycles` must be
run on a matched configuration in each arm.**

**What pinning costs.** More reproducible and *less representative at the same time*.
Production runs turbo; throughput drops ~93 → ~56 Mpps. A peer session found its
tuning parameter looked 4x less valuable pinned than at turbo, and warned anyone
tuning on a pinned rig would pick a value too shallow for production. Mitigation:
run **both arms instrumented** — pinned for the mechanism, turbo for the
representative number — and report turbo as the headline. The turbo arm also supplies
the per-q delivered frequency `linerate_2tx_instr` never recorded.

---

## 3. Machine modifications

Both nodes treated identically. Source changes on branch
`fix/sweep-invocation-and-measurement`: `d68a884` (run.sh core list), `7ebf038`
(Mpps samples as double), `455eede` (pinned 2-queue-pair generator).

### 3.1 Enable the IOMMU (persistent, required a reboot)

`vfio-pci` needs an IOMMU; `/sys/kernel/iommu_groups/` was empty despite ACPI DMAR
tables being present.

`/etc/default/grub`, **line 11 only**:

```diff
- GRUB_CMDLINE_LINUX_DEFAULT=""
+ GRUB_CMDLINE_LINUX_DEFAULT="intel_iommu=on iommu=pt"
```

Placed near the top rather than appended: the file carries an Emulab warning that a
trailing slicefix block re-assigns `GRUB_CMDLINE_LINUX` and strips anything added
after it. GRUB concatenates both variables, so this survives slicefix and leaves
serial-console and `emulabcnet` settings intact.

Verified post-reboot on both nodes: 133 IOMMU groups, `DMAR: IOMMU enabled`.

Backup: `/etc/default/grub.bak-netblast`.
Revert: `sudo cp /etc/default/grub.bak-netblast /etc/default/grub && sudo update-grub`, reboot.

### 3.2 Hugepages (runtime, resets on reboot)

```
sudo ./scripts/reserve_hugepages.sh 16 4096     # 16 x 1 GiB + 4096 x 2 MiB = 24 GiB
```

Explicit arguments rather than the script's defaults (`32` 1 GiB + `40000` 2 MiB =
~112 GB of 125 GB), which would starve maglev of the 8 GiB of *ordinary* heap it
needs. 24 GiB covers dramblast's 8 GiB table plus DPDK's `-m 2000`.

**Side effect that is a measurement confound:** the script also flips

```
/sys/kernel/mm/transparent_hugepage/enabled : madvise -> always
/sys/kernel/mm/transparent_hugepage/defrag  : madvise -> always
```

This decides whether maglev's `aligned_alloc` table lands on 4 KiB or 2 MiB pages,
and `defrag=always` makes THP allocation synchronously compact memory, which can
stall a forwarding core. Left at the script's values so reproduction matches the
original runs; must be controlled before trusting any maglev-vs-dramblast comparison.

Revert: reboot.

### 3.3 NIC binding (runtime, resets on reboot)

```
nix develop . -c bash scripts/bind-dpdk-devices.sh vfio-pci enp23s0f0
```

Binds **only** `0000:17:00.0` (the 100 GbE experiment link) to `vfio-pci`, both
nodes. Drops `10.10.1.1`/`10.10.1.2`, so `ssh node1` stops working — use the control
path `node1.no-link-e810.gpu-coherence-pg0.utah.cloudlab.us`.

Revert: `sudo dpdk-devbind.py --bind=ice 0000:17:00.0 && sudo ip link set enp23s0f0 up`.

### 3.4 `/etc/modules-load.d/vfio-pci.conf`

Created, then found unnecessary — `vfio_pci` is built into this kernel. Harmless
no-op, recorded so it is not mistaken for load-bearing config.
Revert: `sudo rm /etc/modules-load.d/vfio-pci.conf`.

### 3.4b Measurement-stability configuration (runtime, resets on reboot)

Applied after a contention artifact was traced to the scheduler co-locating other
processes onto the DPDK polling cores (§2, maglev q=7). Coordinated with the other
benchmarking session on this host — all six are machine-wide.

| # | Change | Reason | Revert |
|---|---|---|---|
| 1 | `no_turbo=1` | removes the 800 MHz - 3.7 GHz clock swing | `echo 0 > .../intel_pstate/no_turbo` |
| 2 | `scaling_min=max=2100000` on all CPUs | pins at base frequency | restore `800000`/`3700000` |
| 3 | C-states disabled | removes wake-latency variance | `echo 0 > .../cpuidle/state*/disable` |
| 4 | `irqbalance` stopped | it migrates IRQs onto busy cores mid-run | `systemctl start irqbalance` |
| 5 | `nmi_watchdog=0` | removes a periodic per-core interrupt | `echo 1 > /proc/sys/kernel/nmi_watchdog` |
| 6 | THP `defrag` `always` -> `madvise` | stops synchronous compaction stalls | restore `always` |

1-3 via this repo's `scripts/constant_freq.sh 2.1GHz`.

**Why 2.1 GHz.** This SKU's `base_frequency` *and* exactly the invariant TSC rate,
so TSC ticks equal core cycles and the tick-to-cycle correction disappears rather
than having to be measured.

**Verified by measurement, not sysfs.** perf counted 33,482,579,855 cycles over 8 s
on two busy cores = **2.0927 GHz delivered**, within 0.3% of nominal. THP re-checked
after change 6 and still applies: `AnonHugePages` rises 122,880 → 8,509,440 kB during
a maglev run, so maglev's table keeps its 2 MiB backing.

**What it costs.** Every absolute number under it describes a machine nobody deploys
on. Ratios survive. Absolute throughput drops ~1.67x.

**Caveat on comparing across the change.** Cycles per operation is
frequency-invariant only for compute-bound code. For memory-bound code
`cycles_per_op = compute_cycles + memory_stall_ns x frequency`, so the stall
component shrinks *in cycles* at a lower clock.

### 3.4c CPU isolation via cpuset (runtime, resets on reboot)

Frequency pinning does nothing about the scheduler co-locating other work on the
DPDK polling cores — which produced the false maglev q=7 result (§2). Fixed with
cgroup v2 cpusets rather than `isolcpus`, so no reboot is needed.

    bench.slice  (TOP-LEVEL, partition root)  ->  0-23
    system.slice / user.slice / init.scope    ->  24-27,52-55

Housekeeping is physical cores 24-27 *and* siblings 52-55, so nothing in
housekeeping shares a physical core with a benchmark core. Siblings of the benchmark
cores (28-51) are unused by both sets.

Applied with `systemctl set-property --runtime`, so it does not survive a reboot.
Revert by setting `AllowedCPUs=` (empty) on the three slices.

*`bench.slice` must be top-level, not under `system.slice`.* cgroup v2 cpusets are
hierarchical: a scope beneath a restricted `system.slice` can never exceed its
parent's effective cpuset, whatever `AllowedCPUs` is passed. It is silently confined
to housekeeping instead of failing.

*Slice restrictions alone are not sufficient.* They confine userspace; kernel threads
live in the root cgroup. `cpuset.cpus.partition = root` on `bench.slice` makes those
CPUs exclusive and removes them from the root cgroup's effective set:

    /sys/fs/cgroup/cpuset.cpus.effective:   0-55  ->  24-55

(The stronger `isolated` partition type postdates this 5.15 kernel.)

**Consequence for launching.** A plain `sudo ./build/l2fwd` inherits the shell's
cpuset and is confined to housekeeping — it still runs, silently, on the wrong cores.
`sweep.sh` therefore launches into the partition:

    sudo systemd-run --scope --slice=bench.slice -p AllowedCPUs=0-23 -- ./build/l2fwd ...

Verified end to end: `freq=2095MHz`, `hp1g=16->7`, root cgroup effective set `24-55`.

**Diagnostic trap.** `pgrep -f 'l2fwd.*dramblast'` matches the
**`sudo`/`systemd-run` wrapper**, whose command line contains the same strings and
which legitimately lives in the shell's cgroup. A correctly-isolated process looked
unisolated. Use `pgrep -x l2fwd`.

### 3.5 Repository additions

| File | Purpose |
|---|---|
| `docs/INVESTIGATION.md` | this log |
| `l2fwd/sweep.sh` | **the corrected invocation** — sweep driver, N+1 lcores per N queues |
| `l2fwd/extract_results.py` | parses sweep logs into `results_reproduced.json` |
| `l2fwd/plot_sweep.py` | regenerates the two figures from the two JSON files |
| `docs/results_reproduced.json` | measured sweep data, both load conditions |
| `docs/queue_sweep_reproduction.png` | committed vs measured, per mode |
| `docs/per_packet_cost.png` | cycles/packet and RX burst size vs queue count |
| `l2fwd/set_clock.sh` | switches between the pinned and turbo arms, changing **only** the core clock |
| `l2fwd/check_arms.py` | evaluates the pre-registered criteria against the two arms |
| `l2fwd/plot_clock_arms.py` | the two-arm figure, including the collapse test |
| `docs/clock_arms.png` | pinned vs turbo: saturation knee, and cost vs burst size in core cycles |
| `l2fwd/fit_burst_model.py` | fits `cycles/packet = P + C/B` per condition and decomposes P and C into CPU work and exposed stall using the two clock arms |
| `docs/burst_model.png` | the fit: core cycles per packet against 1/burst, all arms |
| `l2fwd/run_matrix.sh` | the experiment matrix after the clock arms — crossover, allocator and pipeline-depth blocks |
| `l2fwd/libsashstore/backing.{c,h}` | run-time page-backing selection shared by both modes, so `-B` can put either on the other's page size |

Source changes to `main.c`, `dramblast.c`/`.h`, `maglev.c` add three run-time knobs
— `-B` (page backing), `-A` (allocator pairs per burst), `-Q` (prefetch pipeline
depth). All default to as-shipped behaviour. They are passed in **argv, never the
environment**: these runs launch through `sudo systemd-run`, which strips the
environment, so an env-var knob would silently fall back to its default and report a
plausible wrong number — a failure mode a peer session lost a dataset to on the same
night.

`set_clock.sh` touches *only* `scaling_min/max_freq` and `no_turbo`; C-states,
`irqbalance`, `nmi_watchdog` and THP `defrag` stay as §3.4b left them in **both** arms,
making pinned-vs-turbo a single-variable contrast. It writes max before min when
pinning and min before max when releasing, because the kernel rejects a
`scaling_min_freq` above the current `scaling_max_freq`: the wrong order fails
silently on the affected cores and reads exactly like success. It verifies by reading
back every core for the same reason.

### 3.6 Fix to `run.sh` — APPLIED as commit `d68a884`

```diff
--- a/l2fwd/run.sh
+++ b/l2fwd/run.sh
@@ -17,8 +17,8 @@
 fi

 # 3. Generate the even core ID list (e.g., 4 cores -> 0,2,4,6)
-# Multiply (NUM_CORES - 1) by 2 to get the maximum core ID needed
-MAX_CORE=$(( (NUM_CORES - 1) * 2 ))
+# N queues need N worker lcores PLUS the main lcore (main.c:523 skips it)
+MAX_CORE=$(( NUM_CORES * 2 ))
 # Use seq to generate a comma-separated list jumping by 2
 CORE_LIST=$(seq -s, 0 2 $MAX_CORE)
```

`N=4` goes from `0,2,4,6` (4 lcores, 3 workers, dies) to `0,2,4,6,8` (5 lcores, 4
workers, runs). Verified against live line-rate traffic: `./run.sh 4 dramblast`
assigns all four queues and reports 92.68 Mpps. Note this changes what the argument
*means* — it reads as "number of cores" but has always been passed straight to `-q`
as the queue count, and the two differ by the main lcore. Renaming to `NUM_QUEUES`
would be honest but is a larger edit.

`docs/results_reproduced.json` carries `min`/`max`/`avg` Mpps plus `cycles_per_pkt`,
`rx_batch`, `rx_missed`, `fwded` per queue count. Regenerate the figures with
`nix develop .. -c python3 plot_sweep.py` from `l2fwd/`.

`docs/performance_plot.png` and `docs/results.json` left untouched — they are the
evidence under investigation.

---

## 4. Open questions

Updated 2026-09-14. Resolved items struck through with what resolved them.

1. ~~Generator: run as committed or scaled to 8 cores?~~ **RESOLVED** — scaled to
   `-l 0-16` (8 TX + 8 RX), reproducing `docs/data.txt`'s 16M flows exactly at
   100 GbE line rate. `pktgen/run.sh` still carries the 2-worker `-l 0-2`.
2. ~~Test the allocator attribution by hoisting the `aligned_alloc`/`free` out of
   `dramblast_process_frames`.~~ **SUPERSEDED, and the reasoning was wrong.** The
   estimate making the allocator look like the whole per-burst cost came from
   turbo-era data whose delivered clock was never recorded; refitted pinned it is
   ~645 cycles against 20-40 ns for a hot-tcache round trip, so a plain hoist would
   move ~10%. **(Wrong twice over, per §5.13: the per-burst cost is ~718 on the
   binary every later arm used, and the tcache reference does not apply to a
   64-byte-aligned request at all. The hoist was run, and it worked.)** §5.9 replaces
   it with an amplification sweep (`-A N`) plus a pipeline-depth sweep (`-Q`).
3. May `main.c` be modified for the measurement fixes (`samples` as `double`, bound
   `SAMPLE_SIZE`)? Still worked around in post-processing. The harness still produces
   biased numbers for anyone reading its output directly.
4. ~~Add per-queue-count frequency sampling.~~ **RESOLVED** — `sweep.sh` measures the
   delivered clock per run with `perf`, never sysfs. The turbo arm's clock is a
   machine constant here — 2993 MHz across a tenfold change in busy cores — because
   idle states are disabled, so all 56 cores always count as active.
5. ~~Optionally pin frequency for a clean absolute cycle count.~~ **RESOLVED** in
   §3.4b, and it is what made the per-burst model fit at all. The earlier conclusion
   that the model "does not hold up" was a statement about uncontrolled frequency.
6. Reproduce against `7e11fc8` or against a corrected HEAD invocation? Still open.
7. ~~Apply the one-line `run.sh` fix.~~ **RESOLVED** as commit `d68a884`.

### Still genuinely open

- **What the smallest distinguishable step of this rig is, everywhere else.** §5.13
  records a result that stood for six hours, survived peer review, and was entirely
  an artefact of `Cycle per fwd packet` printed as an integer. §5.19 audits the
  matched-burst claims; nothing else has been swept.
- ~~**What the ~645 cycles of per-burst work actually are.**~~ **RESOLVED by §5.13
  and §5.14.** Of the 718 cycles the refactored binary shows, roughly **500**
  (462-527) is the `aligned_alloc`/`free` pair and **~165** the prefetch pipeline's
  per-fill ramp. Still open, narrower: why a once-per-burst allocation moves the
  *per-packet* coefficient at all. Cache and TLB pollution from the allocator's chunk
  walking is the obvious candidate, unmeasured.
- **Why the refactored binary is 6% faster per packet** (§5.10). Identical behaviour,
  same sign at every queue count, so it is codegen; which change has not been chased.
- ~~**Cross-core page-table contention on 4 KiB pages.**~~ **ANSWERED by §5.18**, from
  counters already in the logs. Walks per packet flat at 0.99; walk *duration* rises
  19-28% from one core to six or ten. Which shared structure is contended remains
  unmeasured.
- **Whether any of this transfers off this machine.** Every number is from one Xeon
  Gold 5512U with idle states disabled and the uncore locked at 2.5 GHz. The *shape*
  should transfer; the crossing point is a property of this silicon.

---

## 5. The per-burst cost, measured rather than argued (2026-09-14)

Taken frequency-pinned and cpuset-isolated per §3.4b/§3.4c, against the node1
generator holding 93.28 Mpps continuously since 2026-09-12.

### 5.1 The rig reproduces across days

Cold re-run of the pinned `dramblast` q=1 point, two days after the original:
**15.62 Mpps and 101 TSC ticks per packet** against 15.63 and 101 before. Machine
state re-verified rather than assumed: `no_turbo=1`,
`scaling_min = scaling_max = 2100000` on all 56 cores, `bench.slice` still holding
CPUs 0-23 as a `root` partition. Everything below is a comparison between conditions
measured hours apart, so that matters.

### 5.2 A confound found before it was measured: the two modes do not use the same page size

| mode | allocation | pages | TLB entries for 8 GiB |
|---|---|---|---|
| dramblast | `mmap(MAP_HUGETLB \| MAP_HUGE_1GB)` (`dramblast.c`) | 1 GiB | 8 |
| maglev | `aligned_alloc(4096, ...)` (`maglev.c:43`) | 2 MiB via THP | 4096 |

Measured, not inferred. With a maglev run live, `/proc/meminfo` reported
`AnonHugePages: 8513536 kB`, the process's `smaps_rollup` `AnonHugePages: 8331264 kB`
alongside `Private_Hugetlb: 2048000 kB` (DPDK's `-m 2000`, not the table). THP is
`always` on this host, so maglev's plain `aligned_alloc` is silently promoted to
2 MiB pages in full.

8 GiB on 2 MiB pages is 4096 pages against a ~2048-entry L2 STLB, so uniformly random
lookups miss most of the time and take a walk; 8 GiB on 1 GiB pages is 8 entries and
never misses. **Every per-packet cost difference between the modes was a difference
in algorithm and in address translation at once.** §5.6 and §5.12 price it.

### 5.3 The two clock arms, and what the disabled C-states did to them

Identical binary, cpuset, irqbalance, THP and C-state configuration; the arms differ
only in `no_turbo` and the governor limits. The turbo arm's delivered clock, measured
per run, is a machine constant here:

    q=1..10, dramblast and maglev:  2992-2994 MHz, every run

Ten runs, one to ten busy worker cores, one MHz of spread. A core doing nothing
measured 2.993 GHz too. Cause: **idle states are disabled machine-wide** (`POLL`,
`C1`, `C1E`, `C6` all `disable=1` on all 56 cores), so every core spins unhalted and
the package never goes quiet. All-core turbo is pinned near 2.99 GHz regardless of
load — nothing like the 3.7 GHz nominal. So

    r = 2993 / 2094 = 1.429

not the 1.762 the original pre-registration assumed. Recorded as a **condition of the
experiment**, not fixed — fixing it would invalidate the pinned arm too. It is why
AMENDMENT 1 was right to replace a constant `r` with a per-run measured one.

### 5.4 The model, and the instrument

    cycles per forwarded packet  =  P  +  C / B

`P` irreducible per-packet, `C` paid once per RX burst and amortised over the `B`
packets it returned. The queue-pair sweep is an unusually clean instrument: offered
load held at line rate while queue count rises, so the same packet stream is divided
over more queues and `B` falls with nothing else changing. That sweeps `1/B` over a
sixteenfold range using only a command-line argument.

`B` is l2fwd's own `Average rx batch sz` = `rx / rx_cnt`, with `rx_cnt` incremented on
every poll *including empty ones* (`main.c:301`, before the `nb_rx > 0` test), so it
is packets per poll attempt — the right denominator for a per-poll cost.

`fit_burst_model.py` fits by least squares and cross-checks the slope against a
two-point estimator using only the extreme bursts. The two share no algebra, so
agreement is evidence about the model rather than about convergence.

### 5.5 Result: dramblast has a large per-burst cost; maglev has none

| arm | mode | P (cycles/packet) | C (cycles/burst) | R² | two-point C |
|---|---|---|---|---|---|
| pinned 2.094 GHz | dramblast | 95.2 | **644.5 ± 41** | 0.968 | 638.5 (0.9% away) |
| pinned 2.094 GHz | maglev | 163.4 | −44.4 ± 35 — **not resolved** | 0.166 | — |
| turbo 2.993 GHz | dramblast | 105.0 | **684.4 ± 55** | 0.951 | 638.9 (6.7% away) |
| turbo 2.993 GHz | maglev | 200.5 | −26.9 ± 26 — **not resolved** | 0.117 | — |

dramblast's slope is resolved at ~16 standard errors, two independent estimators
agreeing to 0.9% pinned. maglev's is indistinguishable from zero in both arms — not a
weak result but a strong one: its cost per packet barely moves while its burst
collapses 64 → 10 (168 → 159 ticks, a **fall** of 5%), whereas dramblast's rises
101 → 171 over the same range.

Crossover follows directly: dramblast is cheaper while `644.5 / B < 163.4 − 95.2`,
i.e. while **B > 9.4 packets** (9.5 on the refactored coefficients §5.10 says to use).
Above that dramblast wins by up to 40%; below it, it loses. That is the whole shape of
the queue-count dependence in one inequality.

### 5.6 What the two arms decompose it into — the headline

A cost measured in core cycles at two clocks separates CPU work from memory stall:
instructions retire in a fixed number of *cycles*, a DRAM access takes a fixed number
of *nanoseconds*. Writing `X(f) = W + T·f`:

| | CPU work | exposed stall | memory-bound share |
|---|---|---|---|
| dramblast, per packet | 72.4 cycles | 10.9 ns | **24%** |
| maglev, per packet | 77.0 cycles | 41.3 ns | **53%** |
| dramblast, per burst | 551.5 cycles | 44.4 ns | **14%** |

**The two modes do essentially the same CPU work per packet — 72.4 against 77.0,
within 6%. The entire per-packet performance difference is exposed memory latency.**
maglev eats 41.3 ns per packet; dramblast's software prefetch pipeline hides all but
10.9 ns of the same access.

And the cost of that hiding is **not** waiting: `C` is 551 cycles of executed work
against 44 ns of stall — 86% CPU work. This kills the hypothesis that the per-burst
cost is *predominantly* unhidden DRAM latency at the start of a short burst.

> **Read with §5.13 and §5.14.** About 500 of those 551 cycles are `_int_memalign`
> and `free`. But the pipeline ramp is **not** dead: §5.14 measures it at ~165 cycles
> per fill and shows it is latency rather than work, since shortening the pipeline
> eightfold costs 4.1% more instructions against 18.5% more cycles. An aggregate "86%
> executed work" conceals a smaller latency-bound component; it does not exclude one.

Decomposed on the **fitted coefficients**, not point by point, deliberately: the arms
never sit at the same burst size where an excess exists — at q=5 turbo is at burst 30
while pinned is still at 64. Differencing at fixed `q` mixes the clock change with a
burst-size change. `P` and `C` are free of burst size by construction.

### 5.7 Two errors found in this session's own analysis code

**(a) `check_arms.py` computed the CPU-bound fraction with the wrong formula.** It
used `((t_pinned/t_turbo) - 1) / (r - 1)` — a linear interpolation of the tick ratio
between 1 (all memory) and `r` (all CPU). Both endpoints correct, but the ratio is a
ratio of two linear functions of the work fraction, not a linear one, so every
intermediate value was wrong. It read maglev's plateau as 35.2% CPU work where the
correct figure is 43.6%. Correct form, now used:

    cpu fraction = (1 - t_turbo/t_pinned) / (1 - f_pinned/f_turbo)

Found because `fit_burst_model.py`, working in core cycles, was already correct and
the two disagreed. Two independent routes to one number caught it.

**(b) `extract_results.py` destroyed four historical conditions.** It rebuilt
`results_reproduced.json` from scratch on every invocation, so running it against a
directory containing only the new arm's logs silently deleted every condition whose
log directory no longer existed. Recoverable from git; one — the 2026-09-12 pinned arm
— was lost and is superseded rather than recovered. The extractor now merges, and only
rewrites a condition when logs for it are found.

### 5.8 Instructions per packet, and a scope caveat that must travel with them

| mode | burst 64 | burst 8-10 | implied per-burst |
|---|---|---|---|
| dramblast | 400.8 | 496.0 | ~870 instructions/burst |
| maglev | 285.1 | 315.3 | ~358 instructions/burst |

**Not commensurable with the tick counts above; must not share a table without saying
so.** `perf` counts the whole worker core including the DPDK RX/TX driver path;
`Cycle per fwd packet` brackets only the hash region (`main.c:305`-`:355`, with
`rte_eth_rx_burst`/`rte_eth_tx_burst` outside).

Differencing the modes removes it — same DPDK, queues, driver — leaving roughly **512
extra instructions per burst** for dramblast's batched path. Against 551 cycles of
per-burst CPU work that implies IPC near **1.0 for the per-burst path specifically**
— not for the forwarder, which §5.14 measures at 3.96 overall. An IPC of 1.0 for
several hundred instructions of chunk-splitting under an arena lock is exactly what
§5.13 found that work to be.

**The equal-work control, with its limits.** §5.6 assumes both arms execute the same
work, checked at matched burst rather than assumed:

| mode | burst | pinned insn/pkt | turbo insn/pkt | difference |
|---|---|---|---|---|
| dramblast | 64 | 400.9 | 400.9 | **+0.00%** |
| maglev | 64 | 285.6 | 285.8 | +0.08% |
| dramblast | 30 | 416.6 | 419.0 | +0.57% |
| dramblast | 8 | 496.0 | 530.4 | **+6.95%** |

At burst 64 and 30 the arms agree to well under a percent and `P` rests on those
cells. **At burst 8 they do not agree**, and that cell is the longest lever on `C`.
Likely cause: the arms reach burst 8 at different queue counts — q=10 pinned against
q=8 turbo — differing in worker-core count and empty-poll rate, and the burst-size
*distribution* behind an equal mean need not match. So `C` carries additional
systematic uncertainty of several percent beyond the ±41 cycles of fit error; the
turbo arm's burst-4 point is also non-monotone (535.6 insn/pkt against 552.1 at burst
7). `C` is resolved well enough to distinguish "large" from "zero", not well enough
to support a precise value, and no conclusion depends on one.

(This control exists because a peer session found a reproducible 0.19
instructions/key asymmetry between its own machine states. That number is **not**
imported here; what transferred was the check, not the constant.)

### 5.9 What is now queued, and what each run can refute

Surviving candidates: the allocator and the batching machinery. `run_matrix.sh` runs
both; they cannot mimic each other:

- **`-A n`** sets `aligned_alloc`/`free` pairs per burst: `-1` hoists the buffer to a
  per-lcore allocation made once at init, `0` as shipped, `n > 0` adds `n` extra
  pairs. `C` must be linear in `n` if the allocator is the cost, and the slope is what
  a pair costs on this machine — replacing the 20-40 ns literature figure.
- **`-Q depth`** sets prefetch pipeline depth (64 as shipped). A burst of `B` can only
  fill `min(B, depth)` slots, so if `C` is a pipeline ramp it must fall as depth falls
  while `P` rises. If `C` is the allocator, depth cannot touch it.
- **`-B backing`** puts each mode on the other's page size, plus 4 KiB which neither
  ships with. Under §5.2's TLB reading this should move `P` and leave `C` alone.

### 5.10 The control that had to be run, and the review that had to happen

Three knobs make the binary a variable, so the flag-free build must reproduce the
build it replaced. It does not, quite.

| | P (cycles/packet) | C (cycles/burst) | at burst 64 | at burst 8 |
|---|---|---|---|---|
| dramblast, pre-refactor | 95.2 | 644.5 ± 41 | 105.3 | 175.8 |
| dramblast, refactored | 87.7 | 717.6 ± 42 | **98.9 (−6.1% fitted, −3.3% measured)** | 177.4 (+0.9%) |
| maglev, pre-refactor | 163.4 | not resolved | 162.7 | 157.8 |
| maglev, refactored | 163.0 | not resolved | 162.4 (−0.2%) | 158.5 (+0.4%) |

**maglev is unchanged**, which matters more than it looks: its allocation mechanism
genuinely changed (`aligned_alloc(4096, 8 GiB)` → explicit `mmap` plus
`MADV_HUGEPAGE`), and on this host `defrag=madvise`, so the new path takes
synchronous compaction where the old did not. Measured: 99.98% THP coverage against
99.3%. A real difference in what the kernel did, worth 0.2% of run time — set aside
having been measured rather than argued away.

**dramblast is cheaper at a full burst and unchanged at a short one.** Evaluating the
two *fits* at burst 64 gives 105.3 → 98.9, −6.1% — but that differences two
extrapolations. Measured directly at matched burst 64 and matched queue count, the
five paired differences are −3, −4, −4, −3, −3 ticks: **−3.4 ticks, −3.3%** (§5.19).
Half the size, same sign; the direct measurement is the one to believe. Behaviour is
identical (the push-loop bound is 63 either way), so this is codegen, most plausibly
register allocation around a bound that changed from a compile-time constant to a
loaded field. Small, systematic, same sign at every queue count, so **every later
condition is read against the refactored control, never against the pre-refactor
arm.** Without this run a 6% instrument artefact would have been reported as a
page-size effect.

#### What an adversarial review of the new code found

**The harness could not verify the thing the experiment is about.** `sweep.sh` samples
`hugepages-1048576kB/free_hugepages` per run, which distinguishes 1 GiB from not-1 GiB
and nothing else. It cannot tell a 2 MiB arm from a 4 KiB one. Both are *advisory*:
`MADV_HUGEPAGE` may be declined under fragmentation and `MADV_NOHUGEPAGE` can fail,
leaving a complete, plausible, wrongly-labelled dataset and no error anywhere. The
crossover block could have run entirely on 4 KiB pages while reporting itself as 2 MiB.

Rather than edit the harness mid-experiment, page backing is now sampled from outside,
from the process's own `smaps_rollup`, so the label is checked against the kernel for
every run. The 99.3%/99.98% figures came from that sampler on its first run.

> **Not true as written — see §5.16.** The sampler filtered on `Rss > 1 GiB`, and
> hugetlb pages never appear in `Rss`. Every 1 GiB-backed run was skipped: the
> as-shipped dramblast control, the whole allocator block and the whole depth block.
> The THP arms, which this paragraph was written about, were covered throughout.

**A guard against the compiler had itself been compiled away.** The amplification arm
adds N `aligned_alloc`/`free` pairs per burst; a store into the scratch buffer was
supposed to stop the compiler discarding them. GCC 11 deletes that store — a dead
store to an object about to be freed — and disassembly shows it gone while the pairs
survive only because the compiler happened not to apply `-fallocation-dce`. The arm as
built is valid and was measured with that binary. But the failure mode if a later
toolchain applies it is not a crash or noise: it is the allocator round trip reporting
**zero cycles**, a clean and publishable number and exactly the answer the experiment
exists to rule out.

So `check_codegen.sh` asserts, after every build and before any measurement, that the
alloc/free calls are still reachable and that software prefetch instructions are still
present at all. The second is the more important assertion: the whole
dramblast-versus-maglev result is a claim about prefetching, and a build with
prefetches optimised out would measure a different algorithm and still produce a
well-behaved dataset.

Smaller findings, fixed after the measurements so the dataset corresponds to a single
committed tree: `-Q 1` passed validation then read an uninitialised queue slot and
used the unmasked result as a table index; `-A` with any negative value silently
selected the hoisted arm while printing the value back; the hoisted buffer's size was
a bare literal decoupled from `MAX_PKT_BURST`.

### 5.11 An independent check on the memory share, and what its disagreement means

§5.6 infers the memory-bound share from how a cost responds to the core clock.
Checkable directly: the PMU counts cycles stalled with an L3 miss outstanding
(`CYCLE_ACTIVITY.STALLS_L3_MISS`) and cycles with a page-table walk in flight
(`DTLB_LOAD_MISSES.WALK_ACTIVE`). At a full 64-packet burst:

| mode | ticks/pkt | IPC | memory share, two clock arms | memory share, L3-stall counter | page-walk cycles/pkt | LLC load misses/pkt |
|---|---|---|---|---|---|---|
| dramblast (1 GiB pages) | 101 | 3.03 | 17.5% | **0.9%** | 0.0 | 0.007 |
| maglev (2 MiB THP) | 168 | 1.45 | 56.4% | **51.9%** | 25.6 | 0.879 |

**For maglev the two methods agree** — 56.4% against 51.9%, from a frequency sweep and
a hardware counter sharing no assumptions. The strongest corroboration in this
document.

**For dramblast they disagree by 20x, and the disagreement is the result.**
`STALLS_L3_MISS` counts cycles in which *no* micro-operation executes while an L3 miss
is outstanding. dramblast is essentially never in that state — 0.9 cycles per packet
out of 101, at IPC 3.03 — because the prefetch pipeline has run ahead and queued other
work. Yet the clock-arm method still finds 17.5% of the cost failing to scale with
core frequency.

Both are right and measure different things. The stall counter measures *exposed
latency*. The clock-arm method measures everything whose duration is fixed in
nanoseconds — exposed latency **and** any throughput limit in the memory hierarchy (a
mesh at its own fixed 2.5 GHz, fill-buffer occupancy, DRAM bandwidth). **The prefetch
pipeline does not remove dramblast's memory traffic, it converts that traffic from
latency into throughput.** The two measurements agreeing for maglev and diverging for
dramblast is how the two regimes are told apart.

One caveat before reading a counter as a miss rate: dramblast records 0.007 LLC load
misses per packet while reading a random 64-byte line out of an 8 GiB table every
packet, impossible at face value. `LLC-load-misses` counts *demand* loads; dramblast's
lines arrive via `_mm_prefetch`, so the demand load finds them resident. The DRAM
traffic is real and this counter cannot see it. maglev shows 0.879 — one line per
lookup, as the algorithm says.

### 5.12 The crossover: how much of the gap was ever about page size?

Each engine run on 1 GiB, 2 MiB THP, and 4 KiB pages — the last a configuration
neither ships with, so a two-point swap becomes a three-point trend.

Every arm's backing checked against the kernel rather than trusted from the flag: the
4 KiB arms recorded 0.00 GiB `AnonHugePages` and the 2 MiB arms 8.00 GiB, both modes,
from each process's own `smaps_rollup` (`verify_backing.py`). The 1 GiB arms are
covered by the hugepage pool count, 16 → 7 free pages for the 8 GiB table.

> **One exception, found later (§5.16):** maglev on 1 GiB — the arm this comparison
> rests on — was not sampled by the watcher during its measurement runs. Re-verified
> afterwards by re-launching the same binary with the same flag (9.95 GiB
> `Private_Hugetlb`, zero THP): strong evidence that the flag does what it says, not
> evidence about those specific runs. The pool count did cover them at the time.

Cost at q=1 — one worker, full 64-packet burst — in core cycles per packet. (Shipped
dramblast appears at 101, 98.9, 100.4, 99.5 across this document, not inconsistently:
101 is the **pre-refactor** binary, everything from §5.10 onward the **refactored**
one, 3.3% cheaper at a full burst; the rest is ±1-tick print resolution plus small
reproducible variation with queue count. Every comparison is made within one binary.)

| | 1 GiB | 2 MiB THP | 4 KiB |
|---|---|---|---|
| dramblast | **97.8** (shipped) | 101.8 (+4.0) | 116.7 (+19.0) |
| maglev | 153.6 (−14.0) | **167.6** (shipped) | 209.5 (+41.9) |
| gap | 55.9 | 65.8 | 92.8 |

**The page-size confound is real and it is a fifth of the story.** As-shipped gap
69.8 cycles/packet; both on 1 GiB, 55.9 remain — **80% of the difference survives**.
A genuine confound, carried by every earlier mode-versus-mode comparison, but not the
explanation.

**The more interesting number is how differently the engines respond.** 1 GiB → 4 KiB
costs dramblast 19.0 cycles and maglev 55.9 — nearly 3x. So the prefetch pipeline is
also hiding *page-walk* latency: the walk is triggered by the prefetch, early, and
completes underneath subsequent work. That is why the gap widens as pages shrink,
55.9 → 92.8.

**Walk cycles are not walk cost.** maglev on 2 MiB spends 25.6 cycles/packet with a
walk in flight, yet removing walks entirely by moving to 1 GiB saves only 14.0 — so
even the engine with no software prefetching overlaps ~45% of its page-walk time.
dramblast on 2 MiB spends **27% of all its core cycles** with a walk outstanding and
pays 4.0 cycles/packet for it. `WALK_ACTIVE` is an occupancy measure, not a cost;
quoting it as one would overstate the page-size effect by 6x.

#### Where the per-burst model stops applying

On 4 KiB pages the per-packet cost rises with **queue count** at a constant 64-packet
burst: 117, 118, 118, 120, 126, 129 ticks across q=1..6. No per-burst term can express
that. The cause has to be a shared resource; the obvious candidate is the page table
itself, whose own working set contends for the LLC. The effect tracks page size as
that predicts: +12 ticks over six cores on 4 KiB, +4 on 1 GiB, +2 on 2 MiB.

So the two-parameter model quietly stops applying there. R² falls 0.974 → 0.933 →
0.867 across 1 GiB, 2 MiB, 4 KiB, and the fitted intercept stops meaning "per-packet
cost". **The fit still returns confident-looking values.** That is why the comparison
above is made at q=1, which carries neither the per-burst term nor the contention term.

maglev on 4 KiB never reaches line rate at any queue count (84.3 Mpps at q=10), so it
stays oversubscribed and its RX burst never leaves 64. Every point sits at the same
burst size, so no slope is measurable — a result about the configuration, not missing
data, and the analysis now says so instead of printing an unfittable line.

### 5.13 What the per-burst cost is made of — and a reversal

Two candidates survived §5.6: the `aligned_alloc`/`free` round trip
`dramblast_process_frames` performs once per burst, and the batching machinery.
Separated by `-A N` and `-Q depth`, both predictions registered in
`analyse_matrix.py`'s docstring and committed before the runs.

#### The reference was wrong, not the measurement

§5.6's earlier conclusion downgraded the allocator by comparing against a published
20-40 ns for a hot allocator round trip. That figure describes `malloc`'s tcache fast
path. **This code never reaches it.** The binary links glibc 2.33 out of the nix
store, where `aligned_alloc` resolves to `__libc_memalign`, nine bytes — a bare `jmp`
into `_mid_memalign`. `_mid_memalign` relays to plain `malloc` only when the requested
alignment is at most `MALLOC_ALIGNMENT`, which is 16 on x86-64. This call asks for
**64**. So it goes to `_int_memalign`: 453 bytes of code that allocates oversized,
computes the aligned address, splits the chunk and frees the leader — with the arena
lock held throughout.

A **correct measurement compared against the wrong reference**, which no measurement
hygiene catches, because nothing in the pipeline is wrong. The check that catches it is
to read the implementation actually being called. The evidence was already pointing the
right way: §5.6 established the per-burst cost is 86% executed instructions, and
several hundred instructions of chunk-splitting under an arena lock is exactly that.

The 64-byte alignment also appears unnecessary: `dramblast_result_t` is two `uint64_t`s
written sequentially.

#### What the allocator actually costs here

Five arms, each a full ten-run queue sweep, differing only in round trips per burst.
Compared at a **matched burst of 64 and a matched queue count**. Both halves matter:
adding pairs makes the forwarder slower, so it reaches different burst sizes; and at a
matched burst the cost still varies slightly with queue count, reproducibly, so the
difference is taken at each queue count then averaged.

| pairs per burst | matched q | tick differences vs hoisted | cycles per burst |
|---|---|---|---|
| 1 (as shipped) | 1, 2, 3, 4 | 7, 9, 7, 10 | **527 ± 48** |
| 3 | 1, 2, 3, 4 | 24, 25, 24, 27 | 1596 ± 45 |
| 5 | 1, 2, 3, 4 | 40, 40, 40, 42 | 2586 ± 32 |
| 9 | 1, 2, 3, 4 | 72, 73, 71, 75 | 4645 ± 55 |

**The one round trip the shipped code performs costs about 500 cycles.** Two estimators
sharing no algebra: 527 ± 48 at matched burst and queue count, 462 ± 44 by differencing
the two fitted per-burst coefficients. They agree to 1.0σ. Quote it as **462-527
cycles, roughly 240 ns**.

So the allocator is **64-73% of the per-burst cost**, against the "at most ~11%" this
document previously claimed. The remainder is measured directly by the hoisted arm at
**256 ± 13 cycles per burst**. The amplification arms give an incremental pair at
**509 cycles** (consecutive slopes 495 for 3→5, 515 for 5→9); extrapolating down to
one pair predicts 562 against 527 measured — 0.7σ, **not resolved**.

#### The version of this section that stood for six hours, and why it was wrong

The earlier reading took the comparison at **q = 1 alone**. It reported the shipped
pair at **447** cycles, an incremental pair at **510.78**, the two consecutive slopes
agreeing to **0.00 cycles**, and the line's intercept at **0.0** — written up as a
*structural* check (*k* pairs cost exactly *k* times one pair) and then, when the
shipped pair came out 64 cycles below the line, as an allocator **load effect**. A
peer reviewer pushed hard on the framing and improved it, and the improved version was
still built on sand.

None of it survives. `l2fwd` prints "Cycle per fwd packet" as an **integer**, so at a
64-packet burst **one printed tick is 64 cycles per burst**.

- Tick differences at q=1 are 24, 40, 72. `40 − 24 = 16` and `72 − 40 = 32`, exactly
  twice it, and the pair counts 3, 5, 9 have gaps of 2 and 4. So after *any* common
  scaling the two "independent" consecutive slopes are identically equal and the
  intercept identically zero. **The agreement was arithmetic, not measurement.** Using
  each run's own measured frequency already splits them to 511.18 and 510.92.
- 447 against 511 is **one tick**. And 7 is the smallest of the four matched tick
  differences (7, 9, 7, 10); q=1 is where the gap happens to be least. The "load
  effect" was a single integer, chosen — unknowingly — from the low end.

The error class, named: **precision claimed beyond the instrument's resolution, where
the excess precision then generated a mechanism.** The defence is not more careful
reasoning about the numbers; it is asking what the smallest distinguishable step of the
instrument is *before* interpreting a difference. Here that step is 64 cycles per
burst. Caught by asking a second agent to re-derive the headline numbers from the raw
logs without any analysis code, which reproduced 447 and 510.78 exactly and then said
it could only reproduce them from a single run each.

**Three explanations were written for that one printed tick**, and the sequence is the
lesson:

1. *The shipped pair must cost more*, its `free` sitting a batch away from its
   `alloc`. Sign backwards; retracted the same day.
2. *The first pair is intrinsically cheap and later ones cost 511.* Ruled out: the
   model would then miss the 3-, 5- and 9-pair arms by +64 each, a constant, so the
   discount does not persist into them.
3. *An allocator load effect* — pair cost depending on how many are in flight. This
   survived peer review and stood for six hours.

All three explained one tick. The 63.8-cycle gap is 64 cycles. Note account 3
*correctly refuted* account 2 using the constant +64 miss — and that constant was the
tick itself, visible in plain sight as the same number three times, read as a physical
constant rather than as the quantisation it was.

#### Three things that had to be got right, and one that was got wrong

**The line is tested, not asserted.** The earlier draft quoted R² = 0.988 for
per-burst cost against pair count. With three points, two parameters and an x-range
doing all the work, R² is near-uninformative. The regression is now weighted by each
arm's own standard error and tested with χ², and R² is deliberately not printed.

**One arm is excluded, and the exclusion is stated.** The 9-pair arm is slow enough
that only two of its ten runs left a 64-packet burst (R² 0.62, standard error a
quarter of the value). Dropped with the reason given. Third condition here where a fit
returned confident parameters in a regime where the model had stopped applying — the
others being the 4 KiB crossover arm (§5.12) and maglev's per-burst slope.

**The two estimators disagree and the disagreement is reported.** An incremental pair
costs ~509 cycles at matched burst and 365 from the weighted fit. The reason is visible
in `P`, not constant across the arms (89.3, 87.7, 96.9, 99.5): the matched-burst route
multiplies the whole per-packet difference by 64 and charges that drift to the
per-burst term, while the fit separates them but pays with a model. An incremental pair
costs **between 365 and 511 cycles**; that is as far as this data goes. `P` *is* flat
across the two arms carrying the headline — 89.3 hoisted against 87.7 as shipped — so
the structural check holds exactly where the claim lives.

### 5.14 The prefetch pipeline depth: the model was the wrong shape

The per-burst cost decomposed into an allocator part (462-527 cycles) and a remainder
of ~200, supposed to be the prefetch pipeline's fill-and-drain ramp. `-Q` shortens the
pipeline: `dramblast_queue_depth` sets `find_queue_size`, the push loop bounds against
it (`dramblast.c:142`), and the four arms run at depth 8, 16, 32 and the shipped 64.

#### The result that needs no model

Five queue counts in every arm stayed at a 64-packet burst:

| queue depth | cycles/packet | instructions/packet | IPC |
|---|---|---|---|
| 8 | 119.0 ± 0.32 | 414.0 | 3.48 |
| 16 | 106.8 ± 0.37 | 411.9 | 3.86 |
| 32 | 102.2 ± 0.58 | 405.0 | 3.96 |
| 64 (as shipped) | 100.4 ± 0.75 | 397.8 | 3.96 |

Depth 64 → 8: **instructions per packet rise 4.1% while cycles per packet rise 18.5%**,
IPC falls 3.96 → 3.48. A shallower prefetch pipeline does not make the forwarder do
meaningfully more work; it makes it wait. Load-bearing, and it uses no fit. A model in
cycles alone cannot tell work from waiting — only a second counter can, which is why
the instruction counter has been read alongside the cycle counter since §5.8.

It also prices the design decision: depth 64 → 32 costs 1.8 cycles/packet; → 8 costs
18.6. Returns are nearly exhausted by 32.

#### Why the fitted C did something impossible

| depth | P | C | R² |
|---|---|---|---|
| 8 | 115.8 | 375.4 ± 49 | 0.882 |
| 16 | 97.5 | 646.5 ± 18 | 0.994 |
| 32 | 91.2 | 733.7 ± 18 | 0.995 |
| 64 | 87.7 | 717.6 ± 42 | 0.974 |

Depth 8's `C` is **375, below the ~500-cycle allocator pair** — and that pair runs once
per burst at every depth, because the buffer is sized by burst length not queue depth
(`dramblast.c:243`). A per-burst term cannot be smaller than a cost the burst pays
unconditionally. At 1.5σ not a falsification, but the model announcing something is
wrong with it.

What is wrong is the shape. The pipeline fills and drains `ceil(B/Q)` times per burst.
At the shipped `Q = 64` the burst never exceeds 64, so *per burst* and *per fill* name
the same event and the model quietly puts the ramp in `C`. At depth 8 a 64-packet burst
fills eight times, so the ramp moves into `P`. Nothing trades places; the same cycles
change which event they belong to. `ceil(B/Q)` is a step function, so a straight line in
`1/B` is mis-specified wherever `B > Q` — most of the depth-8 arm and none of the
shipped one. This also retires §5.13's worry that `P` moved across the allocator arms.

#### Calibrating the ramp, and a prediction that could have failed

Before the depth-32 arm finished, the ramp was calibrated on depths 8 and 16 at matched
burst 64 — two points, one parameter — giving **165 cycles per pipeline fill**, with the
depth-32 prediction and its falsification bounds written down first (Appendix A). One
of depth-32's five burst-64 points was on screen when the arithmetic was done; the
other four were not.

Stated as an excess over the depth-64 arm:

    predicted by the ramp   2.58 ± 0.14
    measured                1.80 ± 1.14
    null (no effect)        0.00

0.7σ from the prediction, 1.6σ from the null. **Inconclusive.** The first version said
3.1σ from the null and declared the model confirmed, using only within-sweep scatter,
which cannot see anything drifting between sweeps hours apart. §5.15 measured that drift
at 0.45 cycles/packet — the larger term here. Including it doubled the error bar and
removed the verdict. The shallower arms were never close to the floor: depth 8's excess
is 18.6 ± 1.0 (18σ), depth 16's 6.4 ± 1.1 (6σ). Only depth 32 sat near the noise, so
the remedy was repeats.

#### Deciding it: three interleaved repeats

Both arms re-run three times, alternating rather than in two blocks so drift lands on
both equally; `q=1..5` only. The statistic is the **paired** difference within each
repeat, so common drift cancels. Pairing is **queue count by queue count**, not arm mean
against arm mean — the repeats do not all reach burst 64 at the same queue counts, so
arm means would silently compare different queue sets.

| repeat | matched q | tick differences | mean |
|---|---|---|---|
| 1 | 1, 2, 3, 4, 5 | 3, 2, 1, 1, 4 | 2.200 |
| 2 | 1, 2, 3, 4 | 3, 2, 2, 2 | 2.250 |
| 3 | 1, 2, 3, 4 | 3, 1, 2, 2 | 2.000 |

Excess **+2.14 cycles per packet**, with two error bars:

    between-repeat (3 repeat means, 2 dof)      95% [+1.82, +2.47]
    every matched-q difference (13, 12 dof)     95% [+1.61, +2.69]

    ramp model predicts  +2.58        null predicts  0.00

**The null is excluded by both.** Depth 32 really does cost more than depth 64 at a
matched burst. The model's *point prediction* is inside the conservative interval and
just outside the tighter one — 2.58 against an upper bound of 2.47 — and the measured
excess is 17% below it, so the ramp model has the right sign and roughly the right size;
calling this a clean confirmation overreads it. The verdicts were written into
`analyse_matrix.py` before the data existed, including ones that would have gone against
the model, and including the instruction not to pick the nearer hypothesis if the
intervals excluded both.

#### One model across all four arms, and where it stops being a measurement

Fitting `W + ramp·ceil(B/Q)/B + K/B` to all forty points at once:

    W     =  91.4 ± 2.0    steady per-packet work
    ramp  =   166 ± 25     one pipeline fill
    K     =   477 ± 30     per burst
    residual rms 6.25 cycles over a 98-172 range

`K` is striking: nothing in this fit knows about the allocator, yet it lands within 1σ
of the ~500 cycles the amplification sweep measured directly. **That agreement does not
survive a sensitivity check, and it is reported anyway.** Refitting without the depth-8
arm, which the step model describes worst (residual rms 9.1 against 3.3-5.0), moves `K`
to 589 ± 30 — a 2.6σ shift. A parameter that moves that far when the noisiest arm is
dropped is not a measurement. What this fit determines is the ramp, which barely moves
(166 ± 25 with all four arms, 142 ± 31 without depth 8) and agrees with the 165 from
the separate matched-burst route.

#### What the depth sweep settles

- The prefetch pipeline's benefit is **latency hiding, not work reduction**.
- The ramp costs about **165 cycles per fill**, by two independent routes.
- The remaining ~271 cycles of the shipped per-burst cost is **not** all ramp: at the
  shipped depth the ramp is paid once per burst and is already inside that number. Ramp
  + allocator ≈ 500 + 165 = 665 of the 718 measured, leaving of order 50 cycles of
  genuinely per-burst work (the RX and TX burst calls) unattributed — inside this
  instrument's resolution and not to be treated as a measured quantity.
- The linear burst model is valid only while `B ≤ Q`. For the shipped build that is
  every burst, so §5.5-§5.13 are unaffected. For any future arm that shortens the queue
  the step model must be used instead.

### 5.15 The repeat arm: the error bar everything else should have been measured against

The shipped condition re-run at the end of the matrix — same binary, cpuset,
invocation, hours later with every other arm in between. Until this ran, every error
bar came from *within* one sweep.

| | matched-burst points | mean | rms | worst |
|---|---|---|---|---|
| dramblast | 7 | −1.71 | 2.98 | −6 |
| maglev | 6 | +0.00 | 1.53 | −3 |

Only queue counts landing on the *same* burst in both runs are compared.

**The floor is not one number, and it matters which one is used.**

    burst 64          11 points   rms 1.17   (dramblast alone: 0.45)
    smaller bursts     2 points   rms 5.52

At burst 64 the forwarder is oversubscribed and the operating point is pinned by
offered load. At the small bursts a saturated link produces, burst size is an *outcome*
rather than a setting and wanders between runs; largest single discrepancy 6
cycles/packet at burst 9. Every matched-burst claim here is at burst 64, so 1.17 — or
0.45 for dramblast specifically — is the floor they must clear. Quoting the pooled 2.42
would inflate the error on claims made exactly where the rig is most stable while hiding
that small-burst comparisons are twice as noisy as the pooled figure suggests.

| claim | size at burst 64 | multiple of the floor |
|---|---|---|
| the `aligned_alloc`/`free` pair | 7.0 cycles/packet | 6× |
| depth 64 → 8 | 18.6 | 16× |
| depth 64 → 32 | 1.8 (2.14 from §5.14's repeats) | 2× (5× on the repeats) |

The first two are results. The third is why **one sweep per depth could not decide the
ramp model.**

The fitted per-burst coefficients also reproduce: dramblast 718 then 689 (0.6σ), maglev
−36 then −26 (0.3σ). maglev's per-burst cost coming back negative and consistent with
zero a second time independently confirms §5.5.

**What this changes retrospectively.** Every sigma quoted before this arm ran used
within-sweep scatter and was optimistic by however large the between-sweep drift is.
Applied where it changes a conclusion — §5.14's single-sweep depth-32 test moved from
"confirmed at 3.1σ" to inconclusive — and claims standing at six times the floor or
better are unaffected.

### 5.16 The backing verifier was verifying the wrong half of the matrix

`page_watch.sh` samples each live `l2fwd`'s `smaps_rollup` from outside and
`verify_backing.py` reads its log. The watcher's filter was `Rss > 1 GiB`, to skip the
moment between launch and table allocation. **Hugetlb pages are not counted in `Rss`.**
They appear only in `Private_Hugetlb`. So every run whose table is on 1 GiB pages was
skipped: dramblast as shipped (the control), the whole allocator block, the whole depth
block. A live sample shows the shape:

    rss=24364  thp=0  hugetlb=10436608      (dramblast, as shipped)
    rss=8412576  thp=8388608  hugetlb=2048000   (maglev, as shipped)

24 MB of RSS against 10.4 GB of hugetlb. The log never looked broken, because the
maglev and 2 MiB/4 KiB arms, whose pages *do* land in `Rss`, kept writing lines.

`verify_backing.py` then turned a gap into a pass. It judges the rows it finds, so an
arm with no samples produced no complaint, and the script printed *every arm got the
backing it claims* having never looked at the arm that carries the result. **Absence of
evidence was being reported as evidence.** It now carries an explicit list of arms that
must be present, fails on a missing one, and fails on one sampled too thinly to judge.
Run that way it immediately named a second unchecked arm — maglev on 1 GiB.

With the filter corrected, all six arms verify:

| `-B` | mode | samples | THP GiB | Hugetlb GiB | wanted |
|---|---|---|---|---|---|
| 1g | maglev | 9 | 0.00 | 9.95 | 1 GiB hugetlb |
| 4k | dramblast | 131 | 0.00 | 1.95 | 4 KiB |
| 4k | maglev | 134 | 0.00 | 1.95 | 4 KiB |
| as-shipped | dramblast | 144 | 0.00 | 9.95 | 1 GiB hugetlb |
| as-shipped | maglev | 234 | 8.00 | 1.95 | 2 MiB THP |
| thp2m | dramblast | 127 | 8.00 | 1.95 | 2 MiB THP |

**What that table can and cannot claim.** The 1.95 GiB of hugetlb in the 4 KiB and
2 MiB rows is DPDK's own reservation, not the table — the right control: had `-B`
silently fallen back, the table's 8 GiB would show there too. But the rows differ in
*when* they were taken. The 4 KiB and 2 MiB rows were sampled during the runs that
produced the measurements; the as-shipped dramblast row during the depth-repeat block;
the maglev-on-1-GiB row by re-launching that arm afterwards for twenty seconds — a
re-verification, not a contemporaneous one.

### 5.17 The fixes, and a self-inflicted detour worth recording

Four defects from the adversarial review (§5.10) held back until every arm was measured,
so the dataset corresponds to one committed tree: commit `96e6eea`, binary md5
`ecd94f3ecf801b4daaef7f3d3b188788`, archived at `~/sweeps/l2fwd.measured`. Nothing in
the matrix ran the fixed code.

The one that matters is **F4**. The obvious way to stop the compiler deleting a pair it
can prove dead — a store into the buffer before freeing it — *does not work*. GCC 11
removes a store to an object about to be freed. Disassembly showed the store gone and
the `aligned_alloc`/`free` pair surviving only because the compiler happened to be
conservative about the call. `-fallocation-dce` is on by default at `-O2` and one
toolchain bump from eliding the pair outright, at which point the arm would report that
an allocator round trip costs nothing. The buffer's pointer now escapes into an empty
`asm` with no memory clobber; a clobber would force spills around the loop and change
the cost being measured.

The other three are validation: `-Q 1` left the queue empty at the first pop and fed an
unmasked index into a 512-bit load; `-A` accepted any negative value and all of them
silently selected the hoisted arm; the hoisted buffer's size was a bare `64` repeated
from `main.c`'s `MAX_PKT_BURST`, now tied to it by a `_Static_assert`.

All four verify: the tree builds clean under `-Werror`, `check_codegen.sh` still finds
both `aligned_alloc` calls, the `free` and all four software prefetches, and `-Q 1`,
`-Q 2`, `-A 99`, `-A -5` are each rejected with the intended message.

**The detour.** Building them nearly did not happen, because `meson` was "not found",
and earlier `matplotlib` and `numpy` had been "not installed" — written up as another
session having removed them from a shared machine, a dependency-free SVG plotter
written to work around it, and a commit message recording the claim. All wrong. This
project's toolchain lives in a nix dev shell: `nix develop` provides meson 0.60.3,
ninja 1.10.2, matplotlib 3.5.1 and gcc 10.3.0, and `l2fwd/README.md` says so on line 8.
Running from a plain shell makes every one of them look uninstalled, and makes
`/usr/bin/gcc` (11.x, system glibc) look like the project compiler — which is what it
linked against before failing on `GLIBC_PRIVATE` symbols and revealing the mistake. The
SVG plotter is kept, because a figure that needs no environment is one fewer thing to be
wrong about. The lesson: *"the tool is missing" is a claim about the environment, and it
was made without checking the environment.*

### 5.18 The 4 KiB core-count term, answered without running anything

§5.12 left an open question: on 4 KiB pages the per-packet cost rises with the **number
of queues** while the burst stays pinned at 64, and the burst-cost model has no term for
how many cores are running. Nothing needed re-running — the page-walk counters were
recorded alongside every run from §5.9 onward and answer the sharp form: **more walks,
or slower walks?** Every queue count below is at a matched 64-packet burst.

| | q=1 | | q=6 or 10 | | |
|---|---|---|---|---|---|
| | cyc | walk cyc | cyc | walk cyc | walks/pkt |
| dramblast, 4 KiB (q=1→6) | 117 | 107.3 | 129 | 127.6 | 0.99 → 0.99 |
| maglev, 4 KiB (q=1→10) | 210 | 101.7 | 214 | 129.8 | 0.99 → 0.97 |
| dramblast, 1 GiB (control) | 98 | 0.00 | 102 | 0.01 | 0.00 |

**Walks per packet are flat** — 0.99 everywhere, one walk per lookup — and so are
instructions per packet (398.7 → 397.1 dramblast; 285.8 → 286.0 maglev). What rises is
**walk occupancy**: 19% over six cores for dramblast, 28% over ten for maglev. The same
binary on 1 GiB pages takes no walks and shows no core-count effect — the control the
argument needs.

So the term that breaks the model is **contention for shared page-table structures,
measured in the duration of a walk rather than in how many happen.** Which structure is
not settled here.

**How much reaches the cost, at `q = 1 → 6`:**

    dramblast   walk cycles +20.3   cost +12   59% reaches the cost
    maglev      walk cycles +11.8   cost   0    0% reaches the cost

**Opposite direction** from §5.6's prefetch result, consistently: a pipeline hides
latency it *issued early*; it cannot hide latency added underneath it. An engine already
spending half its cycles waiting has slack to absorb more waiting; one retiring at
nearly four instructions per cycle has none. dramblast wins on the memory access it can
prefetch and loses on the contention it cannot.

This question stood open for a day and was answered in ten minutes from counters already
on disk. The `.perf` sidecar next to every run exists precisely because which counter
matters changes as an investigation moves.

### 5.19 Every claim, counted in ticks

`l2fwd` reports "Cycle per fwd packet" as an **integer**, so at a 64-packet burst the
smallest distinguishable step is one tick — 64 cycles per burst. Each row below is
recomputed identically — matched burst of 64, matched queue count, paired then averaged
— so the table is comparable across rows and independent of how each result is quoted
elsewhere. It regenerates with the rest of the analysis.

| claim | n | ticks | sem | status |
|---|---|---|---|---|
| engine gap, as shipped | 5 | 64.40 | 1.63 | safe |
| engine gap, matched 1 GiB | 5 | 52.00 | 1.30 | safe |
| maglev: 2 MiB → 4 KiB | 6 | 44.17 | 0.98 | safe |
| dramblast: 1 GiB → 4 KiB | 5 | 19.40 | 1.33 | safe |
| depth 64 → 8 | 5 | 18.60 | 0.68 | safe |
| maglev: 2 MiB → 1 GiB | 5 | −12.40 | 0.68 | safe |
| the allocator round trip | 4 | 8.25 | 0.75 | safe |
| depth 64 → 16 | 5 | 6.40 | 0.51 | safe |
| the `-B`/`-A`/`-Q` refactor | 5 | −3.40 | 0.24 | **3-5 ticks, no spare digits** |
| dramblast: 1 GiB → 2 MiB | 4 | 3.00 | 0.41 | **3-5 ticks, no spare digits** |
| depth 64 → 32 | 5 | 1.80 | 0.37 | **1-2 ticks, approximate only** |
| the repeat arm (should be 0) | 5 | −0.20 | 0.20 | below one tick |

**Every headline survives.** The allocator result — the one that was wrong — is 8.25
ticks measured properly rather than the 7 it showed at the single queue count it was
read from.

**Three rows needed the text changed.**

- **The refactor was quoted at 6.1%**, differencing two *fits* at burst 64. Measured
  directly it is 3.4 ticks, **3.3%**. The conclusion is untouched; the number was
  inflated by the model.
- **dramblast on 2 MiB versus 1 GiB is 3 ticks.** Real, not a quantity to quote to two
  significant figures.
- **depth 64 → 32 is 1.8 ticks**, which is why §5.14 needed six repeats.

**The repeat arm coming out below one tick is the intended result** and sets the scale.

One caution about the standard errors: they describe the scatter of numbers all rounded
the same way, so a tight `sem` on a one-tick difference is not evidence of anything. The
status column is computed from the tick count alone.

**What would remove the limit.** `main.c:217` divides a cumulative cycle total by a
cumulative packet count and prints the integer quotient. Both operands are already
`uint64_t`; emitting the ratio as a float, or the two totals, gives roughly four more
significant digits for a one-line change. Deliberately not done, because it would change
the binary the whole dataset was taken with. First thing to do before the next campaign.

### 5.20 The floor arm: what the forwarder costs with no lookup at all

Every per-packet number here is read out of one timed region in `main.c` — an
`rte_rdtsc()` before the per-packet loop and another after, accumulated into
`hash_tsc` — and until now nothing said how much of that region is *not* the lookup.
The control was already in the program and had never been run. `-m none`
(`main.c:438`) takes the forwarding loop's third branch (`main.c:351-356`), which
writes the destination MAC exactly as the engines do and skips only the lookup:

```c
        } else {
          for (uint16_t j = 0; j < nb_rx; j++) {
            unsigned dst_port = l2fwd_dst_ports[portid];
            uint64_t mac = 0xff;
            l2fwd_mac_updating(pkts_burst[j], dst_port, mac);
          }
          port_statistics[portid][lcore_id].fwded += nb_rx;
```

No new measurement code: same `sweep.sh`, same counters, one different argument. The
`trio` block in `run_matrix.sh` sweeps all three modes under one tag, back to back.

**Result 1: the packet path is not what limits this system.** With no table the
forwarder reaches the offered 93.28 Mpps at **two** queue pairs and holds it to ten.
dramblast needs seven, maglev nine. A single worker carries 65.85 Mpps on its own,
against 16.02 dramblast and 10.78 maglev.

**Result 2: the lookup is essentially the whole per-packet cost.** At q=1, all three
at a full 64-packet burst:

| arm | cycles/packet, timed region | instructions/packet |
|---|---|---|
| no hash table | 5 | 102.5 |
| dramblast | 98 | 399.2 |
| maglev | 166 | 285.6 |

95% of dramblast's per-packet cost and 97% of maglev's is the lookup itself. Every
engine-versus-engine number here is a comparison of lookups, not of harnesses — the
licence §5.12 and §5.13 were assuming without having checked it.

**Result 3, not anticipated: what the timed region does not contain.**
`rte_eth_rx_burst`, the TX buffer and the driver all sit *outside* the timestamp pair.
At one queue the single worker busy-polls at 100%, so cycles per packet is delivered
clock over delivered rate, and the difference from the timed region is the invisible
part:

| arm | MHz/Mpps = cycles/pkt total | timed | outside |
|---|---|---|---|
| no hash table | 31.8 | 5 | 26.8 |
| dramblast | 130.8 | 98 | 32.8 |
| maglev | 194.3 | 166 | 28.3 |

Three arms whose delivered rates differ sixfold agree on **29 ± 6 cycles per packet**
of RX, TX and driver. They did not have to agree: an artefact of the subtraction would
scale with the quantity subtracted. It also reconciles result 1 — how an arm costing
five cycles per packet inside the region still needs two cores to hold line rate.

**Result 4, a correction rather than a finding.** Fitted like every other arm, the
floor has a per-burst term of its own: `C = 32 ± 12` cycles/burst, `P = 8.3 ± 2.6` —
the two timestamp reads plus loop entry, charged to every burst in every arm. Against
dramblast's 717.6 that is 4%. Quoted as a bound: the fit is poor (R² 0.45) because this
arm runs down to two-packet bursts where the printed integer is 5 to 24 and §5.19's
quantisation is a large fraction of the value. Nothing else moves, because every other
comparison is a *difference* and the instrument cancels in a difference.

**What this arm does not license.** It prices the region, not the machine. The 5
cycles/packet is the MAC write and loop overhead only; driver and NIC costs are the 29
cycles above.

### 5.21 The report was reorganised, and now quotes the tree rather than describing it

`docs/report.html` had been grown one findings card per experiment, in the order the
experiments happened. Three failures of arrangement, not of measurement: the crossover
control (§5.12) sat two thirds down, *after* the results depending on it; the
instrument's resolution (§5.19) was in the final section, after every number it
qualifies had been read; and retractions were interleaved with live claims.

Now six numbered sections, each the precondition for the next: fix the instrument,
price the floor, make the engines comparable, find what depends on queue count, take
that cost apart, say what it is worth. Corrections that are properties of the rig are
collected at the end; corrections belonging to a particular number stay with it.

The page also quoted `C = 645` in one section and `718` in another without saying these
are the pre-refactor and refactored builds. They agree to 1.2σ, so nothing was wrong,
but a reader had no way to know a difference taken *across* the two would not have been
legitimate. The page now says so where the two first meet.

Claims about the code now show the code. `make_report.py:snip()` quotes real lines out
of the working tree at generation time, with real line numbers, anchored on exact
substrings; a missing or ambiguous anchor aborts the build rather than emitting a wrong
quotation. Six snippets: the lcore arithmetic that produced the fake collapse, the
integer quotient that sets the resolution, the forwarding loop's third branch, the two
table allocations that differ in page size, the per-burst `aligned_alloc`, and the
prefetch fill. A paraphrase of what a function does cannot be checked against the tree,
and §5.13 records a claim about `aligned_alloc` that rested on a published constant
rather than on the call actually being made.

### 5.22 What the table is doing, and a counter that undercounts by a hundredfold

**What the table is.** Both engines hash the packet's flow key, look the hash up in a
table of 2^29 sixteen-byte entries (8 GiB, `sweep.sh` `CAPACITY`, `dramblast.h:8-11`),
take the value as the destination MAC, and on a miss consult a static backend table and
insert. A connection tracker, one lookup per packet.

**What makes it the workload is its size, not its algorithm.** 2^29 entries against the
generator's 16.8M flows is 3% occupancy, so the live set is ~268 MB against this part's
52.5 MiB of L3 (`lscpu`), and the index is a hash, so there is no locality. Every packet
should be one random DRAM read. At q=1 and a full burst, per packet:

| arm | LLC-load-misses | cycles stalled on an L3 miss | cycles/packet |
|---|---|---|---|
| no hash table | 0.000 | 0.0 | 5 |
| dramblast | 0.007 | 0.8 | 98 |
| maglev | 0.853 | 85.5 | 166 |

maglev is the predicted thing: ~0.85 misses per packet, 85 of its 166 cycles stalled.

**dramblast's 0.007 is not a hit rate.** The same table and flows would require a 99.3%
hit rate in a cache a fifth the size of the live set, arithmetically impossible for
hash-distributed access. As bandwidth it is worse: 0.007 × 93.26e6 × 64 B is 42 MB/s of
fill traffic for a workload randomly touching 268 MB at 93 Mpps. `LLC-load-misses`
counts demand loads, and dramblast's lines are brought in by `_mm_prefetch`
(`dramblast.c:67`, issued in the find loop at `dramblast.c:140-150`) ahead of the load
that consumes them.

**The code says precisely how.** `dramblast_prefetch` issues `PREFETCH_T1`
(`dramblast.c:71`), which fills **L2 and not L1** — the comment directly above it
describes `PREFETCH_T0`, which is not what the line does. So the line arrives in two
steps and *neither* is a demand load that misses the LLC: the DRAM fill is done by the
prefetch, which is not a load at all, and the L2 → L1 move is done by the
`_mm512_load_si512` gather, which is a load but hits in L2. That accounts for 0.007
exactly, with nothing left over.

> **§5.26 corrects the instruction named here:** the shipped `PREFETCH_T1` constant is
> inverted against the ISA encoding and actually emits `prefetcht2`. The mechanism is
> unchanged — T1 and T2 both land in L2 on this part.

Same fact §5.11 recorded from the other side. **On a software-prefetched path, both
`LLC-load-misses` and `stalls_l3_miss` measure exposure, not traffic.** Neither can be
read as "how much memory this engine touches".

**The consequence is a framing.** The experiment is not about hashing; it is about
servicing one random DRAM access per packet at 93 Mpps. The engine gap is two
strategies for that access — wait for it, or issue it early and find other work. §5.12
exists because the thing being translated is 8 GiB. The per-burst cost of §5.13/§5.14
exists because issuing early requires batching. And the burst-size crossover is where
the price of batching passes the latency batching hides.

**Figure fixes made at the same time**, recorded because they were invisible defects
rather than cosmetic preferences. Three rotated y-axis captions were anchored at the top
of their plot area; `rotate(-90)` makes text run *upward* from its anchor, so all three
ran off the top of the viewBox and were clipped — "core cycles / packet" by 102 px of
its 124. Now centred on the plot area and anchored in the middle. The page-backing
chart's row labels and its "(nnn in page walks)" annotations overran both margins. The
three-engine chart's series were labelled at the ends of their lines, which cannot work
when all three converge at the offered load; both that chart and the matrix panel now
carry a separate key. All found by extracting every `<text>` from the generated SVG and
checking its extent against the viewBox — the only method that works on a host with no
rasteriser.

### 5.23 Validating a raw PMU encoding against a workload with a known answer

`perf` has no JSON event file for this part (family 6 model 207, Emerald Rapids /
Raptor Cove), so `perf list` shows only the ~171 architectural events and every
interesting counter must be raw-encoded. §5.9 records what goes wrong when that is done
by assumption: the generic `dTLB-load-misses` alias read zero on a core where the raw
encoding read non-zero, and a counter that silently reads zero is indistinguishable from
a real result of zero.

The cheap method: point the candidate encoding at a workload whose answer is known in
advance.

```
cpu/event=0x48,umask=0x01,name=l1d_pend_miss_pending/
cpu/event=0x48,umask=0x01,cmask=0x01,name=l1d_pend_miss_pending_cycles/
cpu/event=0x48,umask=0x02,name=l1d_pend_miss_fb_full/
```

The probe is a dependent pointer chase through a 128 MiB randomly-permuted cycle. Each
load's address comes from the previous load's result, so **exactly one L1D miss can be
outstanding at a time** — memory-level parallelism 1.00 by construction, not by
measurement. Over 400M chased loads on one pinned core:

| counter | value |
|---|---|
| cycles | 67,643,556,510 |
| instructions | 3,007,427,737 (IPC 0.04) |
| `l1d_pend_miss_pending` | 64,096,891,488 |
| `l1d_pend_miss_pending_cycles` | 63,479,999,439 |
| `l1d_pend_miss_fb_full` | 16,372,232 |

`pending / pending_cycles` = **1.0097** against a ground truth of 1.00 — the encoding
counts what it claims. Two corroborations from the same run: `pending_cycles / cycles` =
93.9%, which is what a serial chase must look like; and `fb_full` is negligible, which
it must be when one miss is outstanding. All six events ran together without
multiplexing.

The general point outlives the encoding: **an unverified raw encoding is a measurement
of unknown provenance, and the cheapest way to verify one is a workload whose answer you
already know.** A chase gives occupancy 1; a streaming read gives a known miss count per
cache line; an empty loop gives zero.

Note the distinction the ratio hides, since it decides what a figure's axis means:
`pending / pending_cycles` is average occupancy *while any miss is outstanding*;
`pending / cycles` is occupancy over all cycles. On a batched, software-prefetched loop
those differ substantially, and §5.22's result is precisely the regime where quoting the
wrong one inverts the conclusion.

### 5.24 The PMU counters were never multiplexed, now checked rather than assumed

`sweep.sh` carried the claim that its six events fit the PMU without multiplexing. True
when written, never checked again, and nothing downstream could have noticed: a
multiplexed counter is scaled up to a full-window estimate before `perf` prints it,
numerically indistinguishable from a measured one. Every PMU-derived result here would
have quietly become extrapolation.

**Audited.** Across all 310 `.perf` sidecars, 1,860 counter readings, six events each:
**every reading is 100.00% enabled.** The dataset is clean.

**Why it fits — two retractions, because the first two answers were both wrong.** The
original reasoning was that with SMT a logical CPU gets four general-purpose counters,
`cycles` and `instructions` take fixed-function ones, and the four raw events fill the
budget exactly, so one more raw event would silently multiplex all of them. Measured
directly on CPU 26, shipped set plus extras:

| raw events | enabled |
|---|---|
| 4 (the shipped set) | 100.00% on all six |
| 5 | 100.00% on all seven |
| 6 | 74.43 – 87.61%, and 61.95% on the last |
| 7 | 55.43 – 78.14% |

Two further checks: making the SMT sibling busy (a spinner on CPU 54) changes nothing,
and neither does asking perf to count on both siblings — so static partitioning is not
the mechanism either.

**"The ceiling is five" is wrong too, in exactly the same way.** That table varies one
thing — the count — and reads a rule off it. The extra events happened to be two umasks
of `MEM_LOAD_RETIRED` (0xd1). Holding the count fixed and varying *which* events:

| set | raw | enabled |
|---|---|---|
| shipped + `0xd1/01` + `0xd1/02` | 6 | 61.97 – 87.61% |
| shipped + `0xd1/01` + `0xc4/00` | 6 | **100.00%** |
| shipped + `0xc4/00` + `0xc5/00` | 6 | **100.00%** |
| shipped + `0xd1/01` + `0xd1/02` + `0xd1/04` | 7 | 55.61 – 77.95% |
| shipped + `0xc4/00` + `0xc5/00` + `0xd1/01` | **7** | **100.00%** |

**Seven raw events schedule at 100%; six multiplex.** What predicts the outcome is
whether two events share a restricted family: two `0xd1` umasks together collide, one
does not, and mixing families is fine at seven. Same shape the peer session isolated
independently — three `L1D_PEND_MISS` (0x48) umasks with two
`OFFCORE_REQUESTS_OUTSTANDING` (0x20) umasks fail, four 0x48 umasks alone are fine.
Per-event counter restrictions over-constraining each other, not a budget.

**And the premise underneath all of it was never true here.** The peer session tested
*upward*, which neither of us had done — every table above starts from a set that fails
and works down. They measured **eight** generic raw events at 100.00% enabled, alongside
their five-event set that collides. So "with SMT a logical CPU gets four general-purpose
counters" — the sentence this section was built on, in its original form and in both
corrections — **is simply not true of this host.**

That premise was never written down as a finding by either session. It was inherited as
background knowledge, and inherited knowledge does not get phrased as a claim, so it
never entered the set of things under test. Both wrong ceilings were downstream of it.
Each of us revised the *conclusion* twice while the thing generating it sat underneath,
unexamined. The spinner-on-the-sibling check was the closest either came, and it failed
instructively: it tested whether the partitioning was *dynamic*, which presupposes
partitioning exists.

**The dangerous shape is a true measurement plus a generalisation that predicts it —
most dangerous when the generalisation is inherited rather than derived, because then it
is never written as a claim, never cited, and never tested.** Operationally: when a
conclusion keeps needing revision, suspect the premise that has not changed. Every
measurement involved was correct; the failure each time was the generalisation. What
broke it open was building the hardware half of the test rather than the synthetic half.
I had told the peer the ceiling was four and that they could drop an event to reach it;
they were right to keep the counter, and the correction has been sent.

**The transferable rule:** the number of events that fit is not knowable by counting
them, so it must be read off each run — which is what the guard does.

**The guard**, now in `perf_csv.py` so it can be imported and tested, drops any reading
below 99.99% enabled rather than storing it. `test_perf_guard.py` fires it two ways:

- **Synthetic**, seven checks against hand-built perf output: a clean sidecar accepted
  whole, a 41.63% reading refused *and named*, the 99.99% boundary holding on both
  sides, `<not counted>` neither stored nor read as zero, a row with no enabled column
  still parsing, and one bad reading not taking the good ones with it. One check is a
  regression pin: an `instructions` row carrying an IPC in field 5 must not be read as
  an enabled percentage.
- **Real** (`--real`), which oversubscribes the PMU on purpose: the shipped set reads
  100.00% and is accepted, six raw events read 62.36–87.61% and every one of the eight
  readings is refused. The multiplexing is produced by the hardware, so unlike a
  hand-edited percentage it cannot rot — a part with a different counter budget changes
  the test's answer instead of letting it pass forever.

Two traps found while writing it, both of which made the test pass while measuring
nothing:

- **Count per-CPU, not per-task.** `perf stat -e ... -- sleep 2` reports 100.00% enabled
  however many events are requested, because the task is off-CPU almost the whole time
  and enabled and running time are both ~zero. The first version of the real test passed
  on six oversubscribed events for exactly this reason. `-C` is required — which is also
  how `sweep.sh` measures.
- **`name=r5` is a parser error, not a name.** perf reads a bare `rNNN` as its own
  raw-event syntax.

**A third appearance of the field-index bug, and the first that defeats the guard rather
than tripping it.** A raw event spec contains commas, so with `-x,` an *unnamed* spec
splits across extra columns:

```
59304,,cpu/event=0x12,umask=0x0e/,1000954706,100.00,,
```

The event column now holds `cpu/event=0x12` and the enabled column holds a run time in
nanoseconds — comfortably above any threshold — so the reading sailed through the
multiplexing check and was stored under a truncated name with the guard never applied.
Passing `name=` in every raw spec prevents it, which `sweep.sh` does, and which is why
the 310-sidecar audit was valid. But the parser must not depend on the producer having
remembered. `parse_perf` now returns malformed rows as a third category, reported more
loudly: a multiplexed reading means the machine was busy, a malformed one means the
parser was reading the wrong columns and nothing it produced can be trusted. Two checks
catch it — a raw-spec fragment in the event column, and an enabled value outside 0–100.

**Why the guard was blind to this.** A check of the form `enabled < 99.99` can only ever
see a **low** number — and every way of getting the columns wrong produces a high one, a
non-number, or no column at all. The guard was structurally blind to its own most likely
failure mode. The peer's version was worse in one respect: their enabled parse was
wrapped in a `try/except` that substituted **100.0** on failure, so "I cannot read this"
silently became "it is fine" inside the function whose entire job is to refuse what it
cannot vouch for.

This parser had two of the four shapes open — a non-numeric enabled column was accepted,
and so was a row with no enabled column at all. The second was pinned by a *test case
asserting it*, written on the reasoning that a perf version omitting the column would
otherwise drop every counter. That reasoning was wrong, and the test made the wrong
belief look deliberate. Breaking loudly when the producer changes its output is correct
behaviour for a guard, not a cost to be designed around. Every path out of the parser is
now explicit; five new cases cover the shapes.

**The `-x,` field indices**, passed on by the peer session running the `find_batch`
campaign, who rejected two good runs before catching it:

```
0 value   1 unit   2 event   3 run_time_ns   4 enabled_pct   5 metric   6 metric_unit
```

Field **5 is the derived metric, not the enabled percentage**. On the `instructions` row
it holds the IPC, so a guard reading `f[5]` reads a healthy 1.47 IPC as "1.47% enabled"
and throws away a good run. Enabled is field 4. That is the third time one field index
has produced a wrong result here, in three disguises. The rule covering all three: **a
CSV column index is only correct relative to a producer that has not changed its mind,
so a parser must check what it is looking at rather than count.**

**Independent corroboration of §5.22, from a different program.** The same peer pointed
fill-buffer occupancy (the encoding validated in §5.23) at their own prefetch-heavy loop
and measured it flat at ~1.13 across a 64× range of pipeline depth, never approaching
the ~16 fill-buffer ceiling their hypothesis predicted — because their enqueue prefetch
is `prefetcht2`, which parks the line in L2 and allocates no L1 fill buffer. Two
programs, two counters, two sessions, same mechanism: **a software prefetch moves the
traffic out of view of the counters that watch demand paths.** One difference: theirs
issues a second, `prefetcht0` stage eight keys ahead to pull the line L2 → L1, and
dramblast has no such stage. Comparing the two-stage arrangement against a single
`prefetcht0` at the far distance, the single long-range T0 **won by 8.4%** — 19.55 →
17.91 cycles per operation at depth 63. Explicitly not transferable: dramblast's gather
consumes the line differently and its lead distance is set by a different mechanism, so
it is a reason to run the experiment rather than a prediction of its result. Running it
here would be a `-P t0|t1` runtime flag plus one sweep arm — ~twenty minutes of work and
nine minutes of machine time. Deliberately not done: every number in `docs/` was taken
with the current binary, and the control arm licensing cross-binary comparison
(`pinned2` against `pinned`) would have to be re-run to license another one.

**One layer further out, and it is a regression the hardening itself created.** The
guard refuses a bad reading by leaving the key absent from the record. Every consumer
then did `(rec.get(key) or 0) / packets`, so a *refused* counter came back as a
confident **zero** — the exact failure the guard exists to prevent, displaced one layer
outward. It matters most where a zero is also a real answer: the no-table arm
legitimately reads 0.000 L3 misses per packet in §5.22, and the 1 GiB control
legitimately takes zero page walks in §5.18, which is the control the whole core-count
argument rests on.

`per_pkt()` now returns `None` for an absent counter. The report prints an em dash, and
`walk_rows` drops such a run instead of letting it contribute a zero. On the real data
the output is byte-identical, so only what happens when a counter is missing changed.

**The fix was verified by injection, which is what had missed the bug originally.**
Blanking the L3-miss reading on the trio `dramblast` q=1 run and the page-walk counters
on a 1 GiB control run, in a scratch copy, found the repair half done: the table
declined correctly and printed its em dash, while the paragraph *underneath* it, which
argues from those three numbers, still died with `unsupported format string passed to
NoneType`. That names a formatting fault, not a missing measurement. The paragraph now
withholds itself and names the absent counter, so the page degrades into saying less
rather than into saying something untrue.

**The rule, sharpened by the peer's version of the same bug.** They found it where an
unmeasurable denominator became `0.00`, was written to a CSV, averaged into a median and
plotted. The part worth keeping: the finding that file exists to support is that
occupancy is *low*, so a fabricated `0.00` does not contradict the conclusion — **it
strengthens it**. So: a guard that refuses a value has not finished until every consumer
distinguishes "refused" from a legal reading, and **the danger is proportional to how
well the fabricated value agrees with what you expect**. A related shape from the same
tree: a short row in their parser was correctly rejected, but the rejection read "perf
did not report ['cycles']" when perf had reported it and the output *format* had changed
underneath. **A correct verdict reached for a false reason sends the next reader after
the wrong thing**, and it is invisible precisely because the verdict is right.

**What this exchange is evidence of.** Two sessions agreed repeatedly, and the agreement
was the weakest evidence in it — twice it locked an error in rather than catching one.
Both had inherited the same premise, so concurrence added no independent observation,
only confidence. The genuinely independent things shared no premise: a workload with a
ground-truth answer, a counter asked to do something its author had not predicted, and a
test that oversubscribes real hardware. Every conclusion moved by agreement subsequently
had to be retracted.

### 5.25 The literals in the report, and a checker that nearly broke one

Almost everything in `docs/report.html` is generated from `results_reproduced.json`. But
a dozen *measured* values are written into the prose as literals, and those can go stale
silently: the figure beside them regenerates, the sentence does not, and no layer
produces an error.

The prompt was a peer session's observation about their own report. Their point was
sharper than the general hazard: **their newest material was their least guarded** —
their claim-checker covered sections 1–5 while the section written that night went in
with nothing checking it. The same was true here: section 2 (the floor arm) went in the
same night, and every guard written in this investigation had been pointed at the PMU
pipeline rather than at the page.

`check_report_numbers.py` re-derives each literal and fails on drift. All ten currently
match. Two things it caught, neither the thing it was written to find.

**It nearly made a correct number wrong.** The page says dramblast on 2 MiB pages
"spends 27% of every core cycle with a walk outstanding". The first version derived that
as walk occupancy over the *timed region's* cycles, got 35.7%, and reported drift. The
literal was right and the checker was wrong: "every core cycle" includes the RX/TX path
outside the `rdtsc` pair, so the denominator is `pmu_cycles`, against which 27% is
26.9%. Believed at face value it would have sent someone to "correct" an accurate number
— worse than having no checker, because a checker carries authority. Every claim now
names its denominator, and where two are plausible both are printed side by side.

**Its own matcher went stale before its first run.** Each check first asserts the literal
still appears on the page, so a check cannot pass vacuously against rewritten prose. One
matcher failed — not because the prose had changed, but because the generated HTML wraps
at about 78 columns and the phrase straddled a line break. It now matches against
whitespace-normalised text.

**A checker is itself an unverified claim about what the document says**, and should be
injected against before it is trusted. `--self-test` fires all four branches against a
scratch copy:

1. an unchanged copy passes;
2. drifting the *data* under unchanged prose fires the affected claims and only those;
3. changing the *prose* reports the phrase absent rather than skipping the check
   silently;
4. a missing page is a hard exit, not ten failed checks.

The fourth exists because a peer session ran their equivalent against a scratch *data*
directory while the page path still resolved relative to the script, so the page was
never opened, every literal reported missing, and twelve failures looked exactly like a
guard doing its job. **An injection that fails for the wrong reason is
indistinguishable from one that works.** Both paths here now come from one argument.

Writing the self-test produced one more instance of the same family: `load()` rebinds the
module-level `DOCS`, so the restore step in branch 3 copied the scratch directory onto
itself.

**What the peer's tree produced from the same warning**: four literals on their
*published* page were wrong at the moment the warning was sent — including one claim
("FB_FULL never exceeds 1.5%", against a measured 1.62%) sitting under a caption naming
the file that contradicted it. Ten literals here were checked and all ten held; the
difference is not care, it is that this page's numbers are mostly generated while theirs
were mostly typed. The generated architecture makes this failure *rarer*, which is worse
for noticing.

### 5.26 dramblast's find path never finds anything (2026-09-16)

> **Status (updated 2026-09-17): fixed upstream, and this section's numbers predate
> the fix.** `DRAMBLAST_SIMD_KEY_MASK` is `0b01010101` in the tree as of master
> `2d8e93b`, which this branch is now rebased onto. Three consequences, and none of
> them is optional reading before the next measurement:
>
> 1. **Every measurement in this document through `bdff61a` was taken with the
>    shipped mask `0b10101010`**, i.e. on a find path that missed on every packet and
>    therefore inserted on every packet. That includes the saturation and latency
>    work in §5.30-§5.32. The numbers are not withdrawn -- they are correct for the
>    code that was run -- but they do not describe the code that builds today.
> 2. **A rebuild now measures the fixed path.** Anyone who builds this branch and
>    re-runs a sweep is measuring a different workload from the one behind the tables
>    below, and should not compare the two directly.
> 3. `opt/dramblast-find-mask.patch` has been **deleted**, because it is applied
>    upstream and would no longer apply. The arm scripts are inverted to match: the
>    `shipped` arm is now synthesised by editing the mask *back* to `0b10101010`, and
>    the unmodified tree is the `maskfix` arm. See `l2fwd/check_dramblast_arms.sh`
>    and `l2fwd/run_maskfix_sweep.sh`.

Asked to look over the dramblast implementation for optimization opportunities, the
first thing found was not an optimization. The SIMD find path compares the search key
against the wrong half of the cache line, so it returns a miss for every key in the
table — including keys inserted microseconds earlier.

#### The defect

A bucket is 64 bytes holding four `dramblast_kv_t`, and that type is
`{uint64_t k; uint64_t v;}` (`dramblast.h:8-11`), so as eight qwords the line is
`k0 v0 k1 v1 k2 v2 k3 v3` — **keys on the even lanes, values on the odd ones**.

```c
#define DRAMBLAST_SIMD_KEY_MASK 0b10101010
...
__mmask8 key_cmp = _mm512_mask_cmpeq_epu64_mask(DRAMBLAST_SIMD_KEY_MASK,
                                                cacheline, key_vector);
if (key_cmp > 0) {
  int offset = __builtin_ctz(key_cmp);
  result->v = cacheline[(offset + 1)];
```

`0b10101010` selects lanes 1, 3, 5, 7 — the **value** lanes. The search key is compared
against stored MAC addresses and never against a stored key.

The `+ 1` is itself the proof: it is only meaningful for an even `offset`, because the
value of the pair whose key matched at lane `2i` lives at lane `2i + 1`. A mask of
`0b10101010` can only yield an odd `offset`, for which `offset + 1` is the *next pair's
key* — and at `offset = 7` it is `cacheline[8]`, one past the end of an eight-lane
vector. The two lines cannot both be right, and the constant is the one that is wrong.

#### Confirmed by running, because the disassembly is ambiguous

The peer DRAMHiT session, asked independently, quoted the upstream `Item::find_simd`
(`kvtypes.hpp:640-670`), which uses `0b01010101` over exactly this layout and the same
`bucket[idx+1]` idiom — strong evidence, but it warned that the mask's *shape* in the
object code is build-dependent: GCC may fold it into an EVEX write-mask register or
leave the compare unmasked and apply the constant afterwards on a GPR. (This build takes
the first form: `mov $0xffffffaa,%eax`, `kmovb %eax,%k1`, `vpcmpequq %zmm1,%zmm0,%k0{%k1}`.)
The general form, after the peer corrected their own framing: those were the two ways a
*correct* mask can appear, but here the constant is plainly visible in the expected EVEX
form and is simply the wrong value. **Read the constant that is there; do not search for
the one you expect.**

A port defect rather than an inherited one. Upstream DRAMHiT has
`constexpr __mmask8 KEYMSK = 0b01010101` (`kvtypes.hpp:643`), used for both the key
compare (:649) and the empty-slot compare (:663), and the peer reports its correctness
harness passing all 28 cases on this host — hits, misses, empty keys, reprobes and a soak
— which a `0xAA` mask could not survive.

Settled by execution. `l2fwd/test_dramblast_find.c` links the real `libsashstore`,
inserts 64 keys, reads them back with a **scalar** probe that does not use the SIMD path
at all — establishing independently that the inserts are really there — and then looks
the same 64 keys up:

| arm | present (scalar readback) | find hits | hits correct |
|---|---|---|---|
| shipped, mask `0b10101010` | 64 | **0** | 0 |
| mask `0b01010101` | 64 | **64** | 64 |

One character. Nothing else differs between the two columns.

#### Why nothing caught it

The forwarder cannot tell the two apart. `populate_lut` fills the entire backend table
with `0xff` and returns before reaching the Maglev code (`conshash.c:22-26`), so a hit
and a miss produce the *same destination MAC*. `dramblast_process_frames` reacts to a
miss by consulting that LUT and calling `dramblast_insert_one` (`dramblast.c:282-291`),
which finds the key already present and updates it. Every packet is forwarded, to the
right place, with the right address, and the packet counters are identical. No error
path, no dropped packet, no log line — the only observable is speed.

That means **every dramblast number in this document was measured on a 100%-miss
workload.** Not a poor hit rate: exactly zero by construction. §5.22 is most directly
revised. Its prefetch-attribution explanation of the 0.007 `LLC-load-misses` stands. But
it framed dramblast as "one random DRAM read per packet", and the shipped code does a
read *and* a write: the insert every miss triggers stores through the same line the find
just examined, turning a clean read into a read-for-ownership and a later writeback.

#### What it costs

`l2fwd/bench_dramblast_path.c` drives the real `dramblast_process_frames` in bursts of
64, as `main.c:337` does, over a table at the rig's 3% occupancy.
`l2fwd/check_dramblast_arms.sh` builds each arm from a copy of the real source with
exactly one edit, and interleaves the arms across repeats. Nine repeats, TSC ticks per
packet, median:

| arm | median | vs shipped |
|---|---|---|
| as shipped | 52.9 | — |
| + find-mask fix | 35.7 | **−17.3** |
| + hoisted result buffer (`-A -1`) | 30.3 | −22.6 |
| + `prefetcht0` | 34.6 | −18.4 |
| + `prefetcht1` instead of `prefetcht2` | 35.6 | −17.3 |
| + find-loop state hoisted into locals | 35.3 | −17.7 |
| + no vector spill on the hit path | 36.8 | −16.1 |

Figure: `docs/dramblast_arms.svg`.

**Scope, stated because it limits every row.** A 256 MiB table on 2 MiB THP with no NIC,
not the rig's 8 GiB on 1 GiB pages behind a 100 Gbps port. It is several times this
part's 52.5 MiB L3, so the access is still a DRAM access and the
read-versus-read-modify-write comparison is still right, but the absolute ticks are not
comparable to the rig's ~98 and the ordering is indicative rather than final.

**A second limit, and it is mine.** Taken on the shared housekeeping cpuset (cores
24-27,52-55), because the attempt to run inside `bench.slice` was refused by this
session's own permission settings. An earlier pass of the identical sweep, run while two
peer sessions were compiling on that set, read 178 ticks with a standard deviation of 60
against 53 on a quiet machine — 52-55 are the hyperthread siblings of 24-27, so a
compile on 52 halves core 24. The numbers above were re-taken after the peers moved off,
use a median, and have standard deviations of 0.25-1.1. The ranking was identical in both
passes.

#### Three candidates the measurement killed

All three were read straight off the disassembly and all three looked convincing there.

- **The prefetch hint constants are inverted, and correcting them buys almost nothing.**
  `dramblast.c:60-64` defines `PREFETCH_T0 0, PREFETCH_T1 1, PREFETCH_T2 2,
  PREFETCH_NTA 3`. The ISA encoding is the exact reverse — `_MM_HINT_T0 = 3,
  _MM_HINT_T1 = 2, _MM_HINT_T2 = 1, _MM_HINT_NTA = 0` (`xmmintrin.h:42-45`). So the
  shipped `PREFETCH_T1` emits **`prefetcht2`**, confirmed in the object code
  (`prefetcht2 (%r14,%rax,1)`), and `PREFETCH_NTA` would emit `prefetcht0`. **§5.22 is
  wrong on this point.** Correcting to a true `prefetcht1` changes nothing measurable
  (35.6 against 35.7), expected since T1 and T2 both land in L2 on this part. Going to
  `prefetcht0`, which also fills L1, is worth about 0.9 ticks: an interleaved
  seven-repeat paired test gives **−0.95 ± 0.24**, real but small. The inverted table is
  worth fixing for honesty regardless, because the source says one thing and the silicon
  does another.
- **Hoisting the find loop's state into locals does nothing.** The shipped loop reaches
  the queue through three helpers that each re-derive `&ht->queues[id]`, and the object
  code reloads `ht->len` inside the push loop (`mov 0x8(%rbx),%rdx`) and recomputes the
  mask every iteration, because a store through `dramblast_queue_item_t *` may alias the
  header. `opt/dramblast-hoist.patch` reads all of it once. Measured: 35.3 against 35.7,
  inside the scatter. The loop is not issue-bound at this occupancy.
- **Removing the vector spill on the hit path makes it slightly worse.**
  `cacheline[(offset + 1)]` subscripts an `__m512i` by a variable, which GCC implements
  by storing all 64 bytes to the stack (`vmovdqa64 %zmm0,0x40(%rsp)`) and reloading 8 — a
  wide-store-narrow-load forwarding stall on every hit. Reading the value from the table
  line instead measured 36.8 against 35.7: **worse**. Recorded as a refuted hypothesis.

#### The flow hash, which is not dramblast's but is on its path

`flowhash()` (`packettool.c:110`) calls `fnv_1_multi()` three times over 8, 1 and 4
bytes; `fnv_1_multi` (`hash.c:12`) is a byte-at-a-time loop carrying its state through an
`imul`, so thirteen bytes are thirteen 3-cycle multiplies in series. Neither function is
inlined into the caller — `libsashstore/meson.build` sets
`override_options: ['b_lto=false']` on the whole library, so `main.c` is built with
`-flto=auto` and the entire lookup library is not.

`l2fwd/bench_flowhash.c`, TSC ticks per packet:

| | throughput | latency |
|---|---|---|
| FNV, as shipped | 34.0 | 65.4 |
| CRC32C over the same 13 bytes | 5.6 | 34.0 |

The throughput column is the forwarder's regime — `main.c:322-333` hashes every packet of
a burst with no dependency between them, so the hashes overlap. The first version of this
benchmark measured only the latency column and would have overstated the saving by 2x;
the number to quote is **~28 ticks per packet**. CRC32C needs no justification as the
comparison arm: dramblast already runs `_mm_crc32_u64` on this hash's output
(`dramblast.c:78`). Caveat: changing the flow hash changes which flows collide, so it
changes the measured workload and not merely its speed.

#### One more build-configuration finding

DPDK 21.11's `libdpdk-libs.pc` carries `-march=nehalem` in its `Cflags`, and meson
appends dependency flags *after* project arguments, so every translation unit is compiled
as `... -mavx512f -mavx512dq -march=native ... -march=nehalem`, and the last `-march`
wins. Verified with `gcc -Q --help=target`: the effective target is
`-march=nehalem -mtune=nehalem`, with `-mbmi2` and `-mfma` **disabled** and
`-mavx256-split-unaligned-load/store` **enabled**, against `-march=cooperlake` for
`-march=native` alone. The explicit `-mavx512f`/`-mavx512dq` survive, which is why the
AVX-512 code compiles at all and why this was invisible. One visible consequence in the
find loop: `bsf` where a BMI build would use `tzcnt`.

Not measured, no change proposed. Recorded because the entire dataset was taken from a
binary tuned for a 2008 microarchitecture, and because re-ordering the flags would change
that binary — the same reason §5.19 gives for not yet widening the printed cycle counter.

#### Repository additions

- `l2fwd/test_dramblast_find.c` — the insert-then-find correctness harness.
- `l2fwd/bench_dramblast_path.c` — per-packet cost of the real
  `dramblast_process_frames`.
- `l2fwd/bench_flowhash.c` — FNV against CRC32C, throughput and latency.
- `l2fwd/check_dramblast_arms.sh` — builds every arm from a copy of the real source with
  one edit each, interleaves them, writes the TSV.
- `l2fwd/opt/dramblast-hoist.patch` — the loop-hoisting change, kept although it measured
  flat, so the refutation is reproducible.
- `l2fwd/plot_dramblast_arms.py` — `docs/dramblast_arms.svg`.

**Superseded 2026-09-17.** When this section was written nothing under
`l2fwd/libsashstore/` had been modified and the find-mask fix was deliberately left
unapplied, so that every arm above measured a one-character edit against an otherwise
untouched tree. That is no longer the state of the repository: master `2d8e93b` applies
the fix, and this branch is rebased onto it. The measurements above stand as taken --
all of them with the shipped mask `0b10101010` -- but the tree they were taken against
is now the `shipped` *arm* rather than the tree, and a rebuild measures the fixed path.

Two further changes of the same kind landed upstream in `2d8e93b` and are worth naming,
because each one silently turns an arm above into a duplicate of its own baseline if the
scripts are not inverted with it:

- **The vector spill on the hit path is gone.** `result->v` now reads from the table
  line, not by subscripting the `__m512i`. The old `novecspill` arm is therefore the
  tree's behaviour; it has been replaced by a `vecspill` arm that re-introduces the
  subscript, so the refuted hypothesis stays reproducible in the same direction.
- **`opt/dramblast-hoist.patch` has been regenerated** against the new loop body. Two of
  its four hunks no longer applied, and a third applied only with fuzz against changed
  context, which is how a patch quietly lands somewhere it was not meant to. It is kept,
  per the note above, so the flat result stays reproducible.

### 5.27 Every resource that could be the limit, and how each one is measured

Everything above is a **cost**: cycles or ticks per packet, attributed to a piece of
code. A cost does not say whether the machine has headroom left in the resource that
cost is drawn from, and therefore cannot answer whether an optimization is worth making
— if a shared resource is already at its ceiling, removing work elsewhere buys nothing.

So: every resource that could plausibly be the limit, a ceiling for each, and the
instrument that measures its utilisation. The enumeration comes first deliberately — a
bottleneck that was never on the list cannot be found by refining the measurement of one
that was.

| resource | ceiling | source of the ceiling |
|---|---|---|
| offered load | 96.15 Mpps | 100 Gbps / (110 B frame + 8 B preamble + 12 B IFG) |
| NIC PCIe link | 31.5 GB/s one way | `lspci`: Gen4 x16, 16 GT/s x 16 lanes x 128b/130b |
| DRAM bandwidth | 307.2 GB/s | `dmidecode`: 8 populated channels x DDR5-4800 x 8 B |
| L1D fill buffers | 100% of cycles | `L1D_PEND_MISS.FB_FULL` is already a cycle count |
| miss parallelism | — (reported as a count) | no fixed ceiling; the count is the result |
| page walker | 100% of cycles | `dtlb_walk_active` is already a cycle count |
| last-level cache | 52.5 MiB | `lscpu`, one instance |
| core issue width | 6 slots/cycle | Golden Cove allocation width; top-down normalises it |
| core count | 23 workers | `bench.slice` owns CPUs 0-23, one is the main lcore |

Two have a measured ceiling as well as a nominal one, and the measured one is what
utilisation should be divided by: DRAM's achievable streaming bandwidth is well below
307.2 GB/s (§5.30 measures it at 184.2).

| resource | instrument |
|---|---|
| offered load | `l2fwd`'s own `Packets received` and `RX-Missed (Dropped)` — if the NIC is not dropping, the forwarder is keeping up and nothing on this list is the limit |
| PCIe | forwarded bytes/s against the link |
| DRAM bandwidth | `uncore_imc_{0..7}/cas_count_read,cas_count_write` x 64 B |
| miss parallelism | `L1D_PEND_MISS.PENDING / PENDING_CYCLES` — mean misses in flight while any is |
| fill buffers | `L1D_PEND_MISS.FB_FULL / cycles` |
| address translation | `dtlb_walk_active / cycles` |
| LLC | `stalls_l3_miss / cycles`, `LLC-load-misses` |
| core issue | top-down level 1 (`retiring`, `bad-spec`, `fe-bound`, `be-bound`) and level 2 (`mem-bound`, `fetch-lat`, `heavy-ops`, `br-mispredict`) |

Top-down is the load-bearing addition. It partitions **every** issue slot into one of
four fates, so unlike any single counter it cannot miss a bottleneck by not having been
asked about it. Level 2 then splits the backend into memory and core — precisely the
distinction §5.11 and §5.22 could only approach indirectly.

The events are split across six groups, each collected in its **own** `l2fwd` run,
because a group large enough to hold them all would multiplex and silently scale every
value (§5.24). Expensive — one run per group per queue count — which is why the default
queue list is short. `saturation.sh` re-checks the enabled percentage on every run and
marks the summary line `MULTIPLEXED`. `L1D_PEND_MISS.PENDING`/`PENDING_CYCLES` and
`FB_FULL` are deliberately in *different* groups: all three are event 0x48, and three
simultaneous 0x48-family events collide in the event scheduler.

#### The axis is core count, and why that is the discriminating one

A per-core resource — issue slots, fill buffers, the page walker — holds its utilisation
roughly flat as cores are added, because each core brings its own. A shared resource —
DRAM bandwidth, the memory controller, the NIC, the LLC — climbs towards its ceiling and
then flattens the throughput curve. Plotting utilisation against worker count separates
the two classes without modelling either, and the resource whose curve reaches the
ceiling first is the answer.

This also gives the §1 collapse a second look it has never had: it was diagnosed as an
invocation defect (§2) and did not reproduce once corrected, but no measurement has ever
shown what the forwarder runs *out of* at high queue counts.

#### Pre-registered: what each instrument can refute

Written before the runs, as §5.9 and Appendix A were.

- **If DRAM bandwidth is the limit**, its utilisation rises with core count and flattens
  where Mpps flattens. At 93 Mpps with one 64 B line touched per packet this is only
  ~6 GB/s of compulsory read traffic against a 307 GB/s nominal ceiling — about 2% — so
  the honest prediction is that **DRAM bandwidth is not the limit**, and a measurement
  showing otherwise would mean the access pattern moves far more than one line per
  packet. The find-mask defect (§5.26) is exactly such a mechanism, since every packet
  also writes, so the shipped and fixed builds should differ here.
- **If fill buffers are the limit**, `FB_FULL/cycles` is high and the measured MLP sits
  at a hard ceiling regardless of queue depth — which would mean the depth-64 prefetch
  pipeline of §5.14 cannot keep 64 lines in flight, and would explain why its returns are
  exhausted by depth 32.
- **If the core is the limit**, top-down shows `retiring` high and `mem-bound` low. §5.14
  measured 398 instructions/packet at IPC 3.96, close enough to the 6-wide allocation
  limit that this is live, and would make instruction count — the flow hash of §5.26, at
  ~34 of ~98 ticks — the thing worth attacking.
- **If nothing is saturated**, the forwarder is offered-load-bound and the per-packet
  costs measured here are latency that is not being hidden rather than capacity that has
  run out. `RX-Missed` decides this directly, no PMU needed.

Not mutually exclusive; the interesting outcome is a crossover.

#### Repository additions

- `l2fwd/counter_groups.sh` — the event groups and the ceilings, sourced by both scripts
  below so what is validated and what is measured cannot drift.
- `l2fwd/validate_counters.sh` — points every counter at a workload with a known answer
  before it is pointed at the forwarder.
- `l2fwd/bench_membw.c` — those workloads: a sequential stream (known bytes), a dependent
  pointer chase (MLP must be ~1), and a register-only chain (memory counters must read
  ~0).
- `l2fwd/saturation.sh` — one `l2fwd` run per event group per queue count.
- `l2fwd/analyse_saturation.py` — utilisations and `docs/saturation.svg`.

### 5.28 The saturation denominator: a runner written, and the one thing blocking it (2026-09-17)

`.dram_ceiling` carries `DRAM_ACHIEVED_GBS=` empty, and §5.27's whole table of
utilisations divides by it. (The direction of that error was itself stated backwards —
see §5.30.)

`validate_counters.sh` measures the ceiling at four cores and says so
(`validate_counters.sh:164-168`): the agent shell is confined to cpus 24-27,52-55 by
`user.slice`, and `taskset` cannot escape a cpuset. Four cores was a limit, not a choice.

**Withdrawn value: `DRAM_ACHIEVED_GBS=360.0`.** Peer session's 24-thread READ ceiling ×
a 1.582 mixed/read ratio measured at **four** cores. Retracted: the ratio was assumed
constant with thread count and untested, and the peer figure had not plateaued (+18.3%
from 16 threads). Untested.

The repair is not a better extrapolation but a measurement at the thread count `l2fwd`
actually runs at. Cores 0-23 are 24 *distinct physical* cores, since the sibling of core
`c` is `c+28`, so `bench.slice` can supply exactly that.

#### `l2fwd/dram_ceiling.sh`, and the two guards built into it

Method inherited unchanged from `validate_counters.sh` — mixed rather than read-only,
differenced over 3 against 9 passes so the buffer memset and first touch cancel, both
arms from one binary on the same cores. Two things new, each because of a defect already
paid for:

- **It refuses to run on the wrong cores.** A probe confined to the housekeeping cpuset
  does not fail; it returns a plausible wrong number, and would report the same four-core
  figure at every thread count. The script reads its own `Cpus_allowed_list` and exits
  unless it is on bench cores.
- **It sweeps thread count rather than measuring only at the top, and marks its own
  result a lower bound.** If the mixed arm grows more than 5% over the last step, the
  value written to `.dram_ceiling` is annotated `LOWER BOUND, still climbing`. That is
  precisely the defect the withdrawn 360.0 had. The provenance block names the exact cpu
  list and states that the new figure is **not** comparable to `READ_4`/`MIXED_4`, which
  came from the contended 24-27 set — the same set that produced §5.26's 178-tick reading.

The constant is therefore written by the thing that derives it.

#### The run is blocked on a session permission, and a committed script does not help

`bench_membw` was built, the bench lease taken (journal `2026-09-17T00:28:45`, released
`00:29:50` rather than held while blocked), and the run launched as:

```
benchctl run --cpus 0-23 --purpose "DRAM ceiling denominator (thread sweep)" \
  -- l2fwd/dram_ceiling.sh <outdir>
```

Denied by this session's harness permissions. The denial is on the
`sudo systemd-run --scope --slice=bench.slice` that **`benchctl` itself** performs, not
on the script being launched — so shipping the probe as a committed script does not route
around it. Since that launch is the only path onto cores 0-23, no committed script ever
can. A per-session harness permission, not a sudoers restriction.

> **Stale as of §5.30**, which records that the `benchctl` performing that `systemd-run`
> no longer exists. Left in place because the reasoning about why a committed script
> cannot route around a per-session permission is still sound and would apply again.

#### Machine-wide hazards in the *other* tree, and a question answered without touching the machine

Flagged by the scheduler session and verified here, recorded because the blast radius
reaches this project's runs even though the code does not belong to it.

The scripts — `run_tiny.sh`, `setup.sh`, `setup_hbm.sh`, `run_sweep_test.py`,
`toggle_hyperthreading.sh` — are in `/users/sohamb/DRAMHiT-migrate/DRAMHiT/scripts/`.
**None exist in NetBlast**, whose `scripts/` holds only `bind-dpdk-devices.sh`,
`constant_freq.sh`, `get-dpdk-ice.sh`, `prefetch_control.sh` and
`reserve_hugepages.sh`. Worth stating because a bare `scripts/...` path in a two-project
document sends a later reader to the wrong repository.

`run_tiny.sh` offlines cpus 28-55 mid-run by writing
`/sys/devices/system/cpu/cpuN/online`, deleting every SMT sibling of the bench cores and
changing the machine's cpu count under any affinity mask already set, while other
sessions are live. That alone justifies not running it. The *restore* path was initially
reported broken on the grounds that a bare invocation toggles rather than sets; reading it
shows the toggle does restore in the nominal sequence, because the state is 0 when it is
reached. The remaining doubt was the loop bound, `NPROC` from `lscpu`'s `CPU(s):` — if
that counts online rather than present cpus, the restore pass would iterate 0..27 and
leave 28-55 offline permanently.

Settled by the peer session without offlining a real cpu, by running `lscpu --sysroot`
against a synthetic `/sys` tree first validated to reproduce this host, then altered to
the post-offline state: `CPU(s):` stayed at 56 with the offline set on its own line.
`CPU(s):` is the **present** count, so the restore pass does cover 28-55. The method is
worth reusing — a sysfs-reading script's behaviour under a machine state you must not
create can be tested against a synthetic sysroot.

#### Repository additions

- `l2fwd/dram_ceiling.sh` — the thread-count sweep, with the cpuset guard and the
  lower-bound self-marking.
- `l2fwd/plot_dram_ceiling.py` — achieved bandwidth against thread count, with the
  nominal 307.2 GB/s peak drawn for reference and the plateaued/still-climbing verdict
  rendered on the figure rather than left in the CSV. Carries the separate legend and the
  `<text>`-extent check against the viewBox that §5.22 made standard, both exercised end
  to end on synthetic input.

### 5.29 Verifying the 5 cycles per packet, by rebuilding the instrument (2026-09-17)

§5.20 reports **5 cycles per packet** for `-m none` at a 64-packet burst, low enough to
be worth checking. Two questions, answered differently: whether the *arithmetic* is
right, settled by reading the code and the logs, and whether the *instrument* can resolve
five cycles at all, which needed a separate measurement.

Conclusion first: the arithmetic is correct, and 5 is if anything half a tick to one and
a half ticks **low**. Nothing needs retracting.

#### The arithmetic: four things checked, four clean

`main.c:217-218` prints `total_hash_duration / total_packets_fwded`.

- **Denominator deflation.** The numerator accumulates over every packet in the timed
  region, but the denominator is `fwded`, and the dramblast and maglev branches only
  increment `fwded` on a hit (`main.c:344`, `main.c:319`) while the `none` branch
  increments it for the whole burst (`main.c:357`). A dropping engine would charge its
  drops to its hits and read high, inflating §5.20's 95%/97% lookup shares. **It does not
  happen here:** `Packets dropped` is exactly `0` in all thirty trio logs, and `Packets
  forwarded` tracks `Packets received` to within one burst. Checked directly in
  `/users/sohamb/sweeps/trio/*.log`.
- **Ticks versus cycles.** Sound: `set_clock.sh show` reports the pinned arm,
  `no_turbo=1`, every core at 2 100 000 kHz, and the trio logs record a delivered
  2094-2095 MHz. The correction factor is 1.000.
- **The integer quotient.** Load-bearing: a printed 5 is any true value in [5, 6). At the
  floor that is a 20%-wide bin, which is why everything below is a ledger of biases in
  ticks rather than a corrected number.
- **A cumulative mean paired with a steady-state one.** The one real asymmetry.
  `total_hash_duration` and `total_packets_fwded` are running totals from process start,
  so the printed figure is a cumulative mean including warm-up — while `steady_mpps`,
  which the analysis divides alongside it, is deliberately the median of samples
  *excluding* the cold first one (`extract_results.py:60-65`). For `-m none` this moves
  nothing: the printed value is flat at 5 from the first interval. Where it bites is
  maglev, whose q=1 sequence decays `231 200 188 ... 167 166` over 31 intervals; holding
  the recorded intervals fixed and solving for the tail gives a steady state of **163-165
  against the 166 recorded**, about 1%. Under one tick and changes no claim, but it is an
  inconsistency in the method rather than noise.

#### The instrument: rebuilt rather than re-examined

Two properties of `rte_rdtsc()` say a pair of reads might not resolve a five-cycle
region, and neither is visible from the rig's output:

- It is plain `rdtsc` with no fence (`dpdk-21.11/include/rte_cycles.h`;
  `rte_rdtsc_precise()` is the fenced variant and is not what `main.c:309` calls).
  Disassembly of `l2fwd_main_loop` confirms it — bare `rdtsc` at `403bcf` and `403c9b`,
  no `lfence` either side. A non-serialising closing read can retire while the loop's
  stores are still in the store buffer, so the region can read **less** than the work
  costs.
- Executing `rdtsc` twice is not free, and whatever an *empty* region reads is charged to
  every burst in every arm.

`l2fwd/bench_timed_region.c` reproduces the `-m none` branch and times it with the same
unfenced pair, in floating point so truncation is out of the picture. The reproduction is
verified at the instruction level: the rig's inner loop at `403dd0-403df5` and the
benchmark's are the same eleven instructions in the same order — two dependent loads off
the mbuf, two reloads of the source MAC, and stores of 8, 4 and 2 bytes. Two attempts
were needed and both failures silently measured something cheaper than the target:

- A file-scope `static` MAC initialised in place was **constant-folded**, removing both
  reloads and merging three stores into two. A runtime-filled global restored the shape.
- Selecting the burst with `k % pool` *after* the opening timestamp charged a 64-bit
  divider to the region under test — about 35 ticks per burst, moving the headline from
  3.5 to 4.1 cycles per packet. Hoisting the index above the opening read fixed it. Same
  class of defect as §5.13's: a plausible number, internally consistent, describing the
  harness.

#### What it measures

At a 64-packet burst, on an idle bench core via `benchctl run --cpus 4`, median of 15
repetitions.

| | ticks/burst | cycles/packet |
|---|---|---|
| empty region, bare pair (the rig's instrument) | **29.95** | 0.47 |
| empty region, `lfence`-bracketed | 56.7 | 0.89 |

The floor is **29.95 ticks per burst**, stable to ±0.02 across every footprint, stride
and core it was run on. §5.20's `C = 32 ± 12` is confirmed and can now be stated as a
measurement rather than a bound.

The loop itself, read through the rig's own unfenced instrument:

| packet footprint | what has run out | cycles/packet |
|---|---|---|
| 0.1 MiB | nothing — L1/L2 resident | **3.47** |
| 2.8 MiB | past L2 | 7.04 |
| 8.4 MiB | past L2, at the L2 TLB's reach | 7.77 |
| 28.1 MiB | inside L3, past the L2 TLB | 22.02 |
| 84.4 MiB | past L3 | 21.98 |
| 421.9 MiB | past L3, DRAM + page walks | 25.48 |

**The rig's 5 falls inside that bracket**, above the issue-limited floor of the same
eleven instructions and far below a cache miss — where a DDIO-fed forwarder belongs,
since the NIC writes packet data into L3 before the core reads it and the mbuf pool is
recycled continuously.

#### The bias ledger, in ticks

| effect | direction | size |
|---|---|---|
| integer truncation (`main.c:218`) | understates | −[0, 1) |
| instrument floor, 29.95/64 | overstates | +0.47 |
| stores hidden by the unfenced closing read, 58.7/64 | understates | −0.92 |

Working backwards, a printed 5 corresponds to a true region cost of about **5.5 to 6.5
cycles per packet**. Every term is smaller than one tick, so "5 cycles per packet"
survives at the resolution the rig has, and no mechanism should be built on the
difference. The store-shadow term is uniform across arms — a property of the timestamp
pair, not of the engine between them — so dramblast's 98 and maglev's 166 are understated
in the same way and every *difference* taken here is untouched.

#### What was not changed, and why

`main.c:218` left emitting an integer. Printing a float removes the truncation term
outright, but changes the binary the whole of `results_reproduced.json` was taken with,
and re-baselining costs a full sweep against a generator that is currently unusable
(§5.28). The diff, if it is ever worth the cost:

```c
-    printf("\nCycle per fwd packet: %lu",
-           total_hash_duration / total_packets_fwded);
+    printf("\nCycle per fwd packet: %.3f",
+           (double)total_hash_duration / total_packets_fwded);
```

#### Repository additions

- `l2fwd/bench_timed_region.c` — the `-m none` branch reproduced
  instruction-for-instruction and timed with both a bare and an `lfence`-bracketed
  `rdtsc` pair, across burst sizes and packet footprints. `--pool` sizes the packet
  working set; `--csv` appends so one file holds every arm.
- `l2fwd/plot_timed_region.py` — `docs/timed_region.svg`: cycles per packet against burst
  size for each footprint, with the instrument floor drawn as `floor/b` and the rig's own
  (64, 5) marked. Separate legend and the `<text>`-extent check §5.22 made standard.

### 5.30 The DRAM ceiling, measured at last: 184.2 GB/s, and why 360.0 was wrong in method (2026-09-17)

§5.28 built `l2fwd/dram_ceiling.sh` and could not run it. The run has now happened.

#### What unblocked it

Nothing in this repository. §5.28's blocker describes a `benchctl` that no longer exists:
it was replaced with a narrower operation that writes its own pid to
`/sys/fs/cgroup/bench.slice/cgroup.procs` and `exec`s.

One practical note: `benchctl` resolves the caller's identity from the tmux session name,
so a run launched from a session named for the job (`dramceiling`) is refused against a
lease held by `nb`. `BENCH_SESSION=nb` is the fix, and the tool's own error message says
so.

#### The result

```
cpus_allowed_list: 0-23
  nthreads   read        mixed
   1        15.9        27.6
   2        31.8        55.3
   4        62.2        97.7
   8       115.3       140.9
  16       195.7       177.2
  24       228.3       184.2

mixed ceiling at 24 threads: 184.2 GB/s
growth from 16 to 24 threads: 4.0%  (plateaued: yes)
```

`DRAM_ACHIEVED_GBS=184.2`, written by the script that measured it, with the cpu list,
method and plateau verdict appended beside it. The guard at `dram_ceiling.sh:78` reported
`0-23`, so this is 24 distinct physical cores and not the housekeeping set wearing a
24-core label.

**It plateaued.** 4.0% growth over the last step, under the script's 5% threshold, so the
figure is a measured ceiling rather than a lower bound — the defect the withdrawn 360.0
carried and the reason the sweep exists.

#### Why the withdrawn 360.0 was wrong, and it is not the reason that was given

360.0 was the peer session's 24-thread READ ceiling scaled by a mixed/read ratio of 1.582
measured at **four** cores. Retracted on the grounds that the ratio was assumed constant
with thread count and untested, and that the peer's figure had not plateaued. Both right
to raise. The sweep shows the first is not merely untested but false, and false in the
strong sense that the ratio **inverts**:

| threads | read | mixed | mixed/read |
|---|---|---|---|
| 4 | 62.2 | 97.7 | **1.57** |
| 24 | 228.3 | 184.2 | **0.81** |

At four cores a mixed stream beats a read-only one; by twenty-four, read overtakes mixed
decisively. The extrapolation was not an imprecise estimate of the right quantity, it was
the wrong operation. 360.0 against a true 184.2 is nearly 2x high.

**The peer's read number was right all along.** Our independently measured 24-thread read
arm is **228.3** against the peer's `PEER_READ_24=227.53` — 0.3% apart, different session,
different probe invocation. Their non-plateau reproduces too: we read +16.7% from 16 to
24 threads on the read arm against their +18.3%. The READ arm is genuinely still climbing;
the MIXED arm, which becomes the denominator, is not. Only the ratio assumption was
broken, and "the peer's number was unreliable" would have been the easy and wrong lesson.

#### The direction of the error was backwards

`.dram_ceiling`'s header states that dividing by a lower bound **overstates** utilisation.
With the field empty, `analyse_saturation.dram_ceiling()` fell through to preference 2,
`PEER_READ_24=227.53`, labelled "a LOWER BOUND". But 227.53 is a *read* ceiling, and at 24
threads the read ceiling **exceeds** the mixed one. It was not a lower bound on the
quantity wanted; it was an over-estimate of it by 23.5%.

Every DRAM utilisation drawn against it is consequently **understated** by that factor,
not overstated. The caveat that travelled with the figures pointed the wrong way, which is
worse than no caveat, because a reader discounting in the named direction moves further
from the truth.

#### The hedge that was supposed to make this safe is not on the figure

`analyse_saturation.py`'s docstring reassures that the peer figure "is a lower bound and
is named as such, so utilisations against it read as 'at least'" (`:41`). It is not true
on the page. The label is built by splitting `CEIL_SRC` at the first comma and keeping
only the head:

```python
            put("DRAM bandwidth (of %s)" % CEIL_SRC.split(",")[0], gbs / DRAM_CEIL)
```
<sub>`l2fwd/analyse_saturation.py:242`</sub>

`CEIL_SRC` is `"peer 24-thread READ ceiling, a LOWER BOUND"`. Everything after the comma —
the entire qualifier — is discarded. The rendered label in `docs/saturation.svg` reads:

```
DRAM bandwidth (of peer 24-thread READ ceiling)
```

and the strings `least` and `lower bound` appear **zero** times anywhere in that file. So
the caveat exists in the source, in `.dram_ceiling`'s header and in this document, and in
none of the places a reader of the figure would look. It also pointed the wrong way. The
percentages are rendered bare — `"%7.1f%%"` at `:259` and `'%d%%'` at `:304` — with no "≥"
or "at least" anywhere.

The change is one line and is **not applied**, because it alters a figure whose underlying
data cannot currently be regenerated, and a relabelled plot on a stale denominator would
be worse than an honestly stale one:

```diff
-            put("DRAM bandwidth (of %s)" % CEIL_SRC.split(",")[0], gbs / DRAM_CEIL)
+            put("DRAM bandwidth (of %s)" % CEIL_SRC, gbs / DRAM_CEIL)
```

With `DRAM_ACHIEVED_GBS=184.2` present, `CEIL_SRC` becomes the short `"measured mixed
ceiling"` and carries no comma, so the split is harmless from here on and the defect is
self-closing for future runs. Recorded because it silently removed a caveat for every
figure drawn before today.

**The shape of this defect is one this document has already paid for twice.** Not a
missing caveat — a string operation that silently *ate* one, on a field whose second
clause was the entire safety argument. Compare the integer quotient of §2's
"Measurement-harness defects": `total_hash_duration / total_packets_fwded` with both
operands `uint64_t`, truncating a quantity that is not an integer with nothing downstream
able to notice. Both are apparatus quietly discarding the part that says how much to trust
the number, in a way no consumer can detect, because what arrives looks exactly like a
well-formed answer. **When a value and its qualifier travel in one string or one
expression, any operation that narrows it is a candidate for having dropped the qualifier
rather than the value.**

#### `docs/saturation.svg` is stale, and cannot be refreshed from disk

`analyse_saturation.py` will now select the new denominator as preference 1. The committed
figure does **not** reflect it: it was drawn against 227.53. It cannot simply be
re-plotted, because the raw `.perf` and `.log` files from the saturation run are not in
the tree and not in any scratchpad — regenerating requires re-running `saturation.sh`,
which needs the box and a working generator, and node1's generator has been unusable since
~21:45 on 2026-09-16. Recorded rather than quietly fixed so nobody reads those percentages
as current.

#### Cross-checked against a sibling project's defect, and clear

The DRAMHiT session hit a harness bug worth checking for here before trusting 184.2: its
perf greps matched `instructions:u`, and the `:u` suffix is one perf emits only
**outside** `bench.slice`. The probe therefore matched nothing in precisely the
environment it was written to run in — an instrument that behaves differently depending on
whether it is inside the cpuset.

This sweep ran inside `bench.slice`, so the same failure would have applied. Checked and
clear: no `:u` or `:k` suffix appears in any perf-reading code in this tree.
`counter_groups.sh` specifies everything as raw encodings —
`cpu/event=0x48,umask=0x01,name=.../` for core events and
`uncore_imc_N/event=0x05,umask=0xcf/` for the CAS counters this ceiling is built from
(`:73-74`) — and raw encodings resolve identically inside and outside the slice. The only
`cycles:u` strings in the tree are in `l2fwd/compare_userspace_ice.py`, where they are
*data*: keys looked up in another project's result CSVs.

Two further reasons to trust the number. `counter_groups.sh` already carries the
`<not supported>` guard (`:23-27`) — counters written as a plain comma list report
`<not supported>` at a run time of 0 while still claiming 100.00% enabled, the same
"absent counter read as a measured zero" trap paid for before. And the read arm reproduced
an independent session's figure to 0.3%, which a dead counter cannot do.

#### Repository additions

- `l2fwd/dram_ceiling_out/dram_ceiling.csv` — the full six-point sweep, both arms, with
  the core list per row, so the inversion above is checkable rather than asserted.
- `docs/dram_ceiling.svg` — achieved bandwidth against thread count, both arms, the
  nominal 307.2 GB/s peak drawn for reference and the plateau verdict rendered on the
  figure. Separate legend and the `<text>`-extent check (`plot_dram_ceiling.py:155`, which
  `sys.exit`s rather than warning).

### 5.31 Reading the saturation figure back, and the two resources that were never on the list (2026-09-17)

Whether §5.30's staleness matters needs the plotted values, and the raw `.perf` files are
gone — so they were recovered from the figure's own geometry, by calibrating the y-axis
against its gridline labels (`y=365` is 0%, `y=65` is 100%) and mapping each `<polyline>`
back to its legend entry by stroke colour. Values below are accurate to roughly ±0.3
percentage points, far inside every margin they are used for. A weaker source than the raw
counters, used only because those no longer exist; the re-run supersedes it.

| resource | q=1 | q=4 | q=8 | q=16 | q=23 |
|---|---|---|---|---|---|
| link rate (of line rate) | 19.9% | 66.1% | **98.0%** | 81.7% | 12.8% |
| core: retiring / slots | 60.2% | 54.7% | 47.5% | 31.5% | 15.3% |
| core: backend-bound | 18.6% | 26.1% | 24.4% | 45.2% | **73.1%** |
| core: memory-bound | 6.5% | 16.9% | 13.2% | 43.9% | **58.4%** |
| PCIe (of Gen4 x16) | 7.4% | 22.8% | 33.6% | 28.1% | 4.9% |
| DRAM bandwidth | 1.7% | 2.9% | 3.4% | 3.9% | 4.1% |
| L1D fill buffers full | 6.6% | 4.6% | 3.4% | 1.2% | 1.0% |
| stalled on L3 miss | 1.6% | 1.6% | 1.4% | 1.3% | 1.2% |
| page walker active | 1.0% | 1.0% | 1.0% | 1.1% | 1.0% |

#### The staleness does not matter, and the pre-registration was right

Applying §5.30's 1.235x correction moves DRAM bandwidth from 1.7-4.1% to 2.1-5.1%. No
conclusion turns on that. §5.27 pre-registered "the honest prediction is that **DRAM
bandwidth is not the limit**... about 2%", and the measurement lands there. That
prediction is confirmed rather than rescued by the correction, the stronger of the two
outcomes. So the re-run is worth doing for the denominator, but not urgently, and nothing
published needs withdrawing.

#### What the figure cannot explain, and it is the interesting part

At q=23 the core is **58.4% memory-bound** while every instrument that measures a memory
*resource* reads between 1% and 5%: DRAM bandwidth 4.1%, L3-miss stalls 1.2%, fill buffers
1.0%, page walker 1.0%. Those cannot simultaneously describe a bandwidth or a capacity
limit. The core is waiting on memory and nothing on §5.27's list accounts for it.

This is also the first measurement bearing on the §1 collapse. Link rate climbs to 98.0%
of line rate at q=8, then falls to 81.7% and 12.8% as workers are added, while `retiring`
collapses 60.2% → 15.3% and backend-bound rises 18.6% → 73.1%. Throughput is lost to the
core going backend-bound, not to any shared resource reaching its ceiling.

#### Two resources from the original brief are absent from the enumeration

§5.27's table lists nine resources. The brief it was written against also named **DRAM
latency**, as a thing distinct from DRAM bandwidth, and **coherence traffic between
cores**. Neither appears in the table, and neither is instrumented: `counter_groups.sh`
and `analyse_saturation.py` contain no snoop, no HITM, and no latency event of any kind.

They are precisely the two candidates the q=23 signature points at. High memory-bound with
low bandwidth is what latency that is not being hidden looks like; and a per-core cost that
worsens as workers are added, with no shared ceiling reached, is what inter-core
interference looks like. §5.27's own opening argument applies to itself here: *a bottleneck
that was never on the list cannot be found by refining the measurement of one that was.*

#### One instrument exists and is not on the figure

MLP is computed — `analyse_saturation.py:248-250` divides `l1d_pend_miss_pending` by
`l1d_pend_miss_pending_cycles` — but is reported as a count rather than a utilisation,
because it has no fixed ceiling, and so never reached the plot. It is the single
measurement that discriminates latency-bound from bandwidth-bound: an MLP pinned at a hard
ceiling while DRAM bandwidth sits at 4% is a latency answer. Recovering it requires the
same re-run.

#### What the next run should carry

1. Add DRAM-latency and coherence encodings to `counter_groups.sh`, and validate them
   against `bench_membw` first. Per §5.23, a raw encoding on this part is untrusted until
   checked against a workload with a known answer.
2. Re-run `saturation.sh`. This picks up `DRAM_ACHIEVED_GBS=184.2` automatically, recovers
   MLP, and closes the `.split(",")[0]` label defect of §5.30 without a code change.
3. Make the find-mask fix an arm of the same sweep. §5.27 pre-registered that the shipped
   and fixed builds should differ in DRAM traffic, and that comparison has never been run.

Steps 2 and 3 need the box and a working generator; node1's has been unusable since ~21:45
on 2026-09-16. Step 1 needs neither.

---

## Appendix A. Pre-registration: a per-fill ramp model for the prefetch pipeline

Written 2026-09-14 09:16 UTC, while the `d32` arm was running. Referenced by §5.9 and
§5.14. Was `docs/depth_prediction.md`; folded in here 2026-09-17.

**Disclosure about what had been seen.** Derived from the `d8` and `d16` arms, which were
complete. `d32` was in flight; its `q=1` point (101 cycles/packet at burst 64) was on
screen when the arithmetic was done, so that single point is *not* out of sample. Its
`q=2..5` points, the replicates that actually resolve the prediction, had not been taken.

### The model

`cycles/packet = P + C/B` was derived on the shipped build, prefetch queue 64 deep, RX
burst at most 64. There one burst is exactly one fill and drain, so "per burst" and "per
pipeline fill" name the same event. Shortening the queue separates them: with depth `Q`
and burst `B` the pipeline fills and drains `ceil(B/Q)` times per burst, so

    cycles/packet  =  W  +  ramp * ceil(B/Q) / B  +  alloc / B

`W` steady-state per-packet work, `ramp` the cost of one fill/drain, `alloc` the 447-cycle
`aligned_alloc`/`free` pair, once per burst at every depth because the buffer is sized by
burst length not queue depth (`l2fwd/libsashstore/dramblast.c:243`).

This explains why the fit reports `P` rising and `C` falling as the queue shortens, which
looked like the two parameters trading places. They are not: at `Q = 64` the ramp is paid
once per burst and lands in `C`; at `Q = 8` it is paid eight times per 64-packet burst and
lands in `P`. It also explains why `d8`'s fitted `C` (375 +/- 49) sits below the 447-cycle
allocator floor without anything being wrong: at depth 8 the ramp has almost entirely left
`C`, and the fit's remaining freedom is absorbed by the correlated `P`.

### Calibration

At burst 64, averaging the five queue counts that stayed there:

| depth | cycles/packet | excess over depth 64 | 1/Q - 1/64 |
|---|---|---|---|
| 8  | 119.0 | 18.6 | 0.109375 |
| 16 | 106.8 |  6.4 | 0.046875 |
| 64 | 100.4 |  —   | 0 |

A line through the origin gives **ramp = 165 cycles per pipeline fill**. Two points, one
parameter — a calibration, not yet a test.

### The prediction

At burst 64, depth 32 should cost

    100.4 + 165 * (1/32 - 1/64)  =  103.0 cycles/packet

against 100.4 if depth does not matter at all at this burst. Per-point scatter within an
arm is 1-2 cycles, so the mean of the five burst-64 points has a standard error near 0.5
and the two hypotheses are about five sigma apart.

**Falsified if** the depth-32 mean at burst 64 comes out at or below 101.4, i.e.
indistinguishable from no effect, or above 104.6.

### Why this is not the whole story

The ramp explains the *cycles*, not the instruction counts, which at burst 64 are 397.9
per packet at depth 64, 411.8 at depth 16 and 413.9 at depth 8. A per-fill instruction
overhead would make depth 8's excess twice depth 16's; instead the two are nearly equal
(+16.0 and +13.9). That step is not explained here.

The cycles-versus-instructions split is the load-bearing part of the depth result and does
not depend on the model at all: depth 64 → 8 at matched burst 64, instructions per packet
rise **4.0%** while cycles per packet rise **18.5%**, IPC falls 3.96 → 3.48.

---

### Outcome 1 (2026-09-14, after the arm completed) — SUPERSEDED

    predicted   103.0        null (no effect)   100.4
    measured    102.2 +/- 0.58  (mean of five burst-64 runs)

1.3 sigma from the prediction, 3.1 sigma from the null. Not falsified. The ramp is
confirmed by a second route: fitting `W + ramp*ceil(B/Q)/B + K/B` to all forty points from
the four depth arms at once gives `ramp = 166 +/- 25`, against the 165 calibrated here.
The two routes share no algebra.

The instruction-count anomaly stands unexplained. Depth 32 gives 405.0 instructions per
packet, between depth 64's 397.8 and depth 16's 411.9, so the step-at-first-shortening
reading is wrong too — the count rises smoothly with the number of fills while the *excess*
is not proportional to it.

### Outcome 2 (same day, after the repeat arm) — SUPERSEDED

The "1.3 sigma / 3.1 sigma" verdict used only within-sweep scatter, which cannot see
anything drifting between sweeps hours apart. The repeat arm measured that drift at 0.45
cycles/packet rms at burst 64 for dramblast. With that term included, stated as an excess
over the depth-64 arm so the baseline's own error is not dropped:

    predicted by the ramp   2.58 +/- 0.14
    measured                1.80 +/- 1.14
    null (no effect)        0.00

0.7 sigma from the prediction, 1.6 sigma from the null. **Inconclusive, leaning toward the
model** — not the confirmation claimed above. The one-model fit (ramp = 166 +/- 25 from all
forty points) is unaffected, because it pools arms rather than differencing two.

### Outcome 3 (2026-09-14, three interleaved repeats) — SUPERSEDED

    repeat 1   +2.20
    repeat 2   +2.50
    repeat 3   +2.00

    paired mean  +2.23   sd 0.25   se 0.15
    95% interval (t, 2 dof)  [+1.61, +2.86]

    ramp model predicts +2.58      null predicts 0.00

Interval contains the prediction and excludes the null. **Confirmed at depth 32.**

### Outcome 4 (same day, after an independent re-derivation) — STANDS

The paired means above compared each arm's *mean over the queue counts it happened to hold
at burst 64*, and those sets differ: repeat 2's depth-32 sweep held burst 64 at five queue
counts while its depth-64 partner held it at four. Since cost varies slightly with queue
count, that compares different queue sets. Paired queue count by queue count instead:

    repeat 1   q=1..5   differences 3, 2, 1, 1, 4   mean 2.200
    repeat 2   q=1..4   differences 3, 2, 2, 2      mean 2.250
    repeat 3   q=1..4   differences 3, 1, 2, 2      mean 2.000

    excess +2.14 cycles/packet
    between-repeat (3 means, 2 dof)          95% [+1.82, +2.47]
    every matched-q difference (13, 12 dof)  95% [+1.61, +2.69]

    ramp model predicts +2.58      null predicts 0.00

**The null is excluded by both intervals**, so the effect is real. The model's point
prediction sits inside the conservative interval and just outside the tighter one, and the
measurement is 17% below it. The ramp model has the right sign and roughly the right size;
"confirmed" was too strong.

That is the fourth verdict on the same comparison. The sequence — over-confident, then
unable to say anything, then confident again, then this — is not a story about the ramp
model. It is a story about error bars: each revision changed only which sources of
variation the interval was allowed to see.

### 5.32 The answer: latency, not bandwidth (2026-09-17)

§5.31 left the question open — core 58.4% memory-bound at q=23 while every
memory *resource* read 1-5%. Not a bandwidth or capacity limit, and none of
§5.27's nine resources could explain it. The two candidates named there were
DRAM latency as distinct from bandwidth, and inter-core coherence. Neither was
instrumented.

Latency is now instrumented. It is the answer.

#### Result

Generator recovered; sweep re-run 2026-09-17 15:53:32Z, 35/35 runs, every group
100% enabled, no multiplexing, raw `.perf` captured in `l2fwd/saturation_out/`.

| | q=1 | q=4 | q=8 | q=16 | q=23 |
|---|---|---|---|---|---|
| **mean data-read latency (cyc)** | **75.0** | **92.3** | **181.3** | **364.3** | **700.4** |
| DRAM bandwidth (of measured 184.2) | 1.2% | 3.6% | 5.3% | 7.4% | 4.5% |
| misses in flight (MLP) | 2.27 | 2.22 | 2.13 | 1.99 | 1.33 |
| core: memory-bound | 5.1% | 12.9% | 9.8% | 37.5% | 57.1% |
| link rate | 16.5% | 58.9% | 97.0% | 97.0% | 11.5% |
| L1D fill buffers | 4.9% | 3.3% | 3.0% | 0.5% | 0.1% |
| page walker | 0.0% | 0.0% | 0.0% | 0.1% | 0.0% |
| stalled on L3 miss | 0.6% | 0.7% | 0.4% | 0.7% | 0.3% |

**Latency rises 9.3x. Bandwidth never exceeds 7.4%.** The memory system is
nowhere near its throughput ceiling and every access costs nine times more.

Compounding: MLP *falls* 2.27 → 1.33 over the same range. Per-core memory
throughput is MLP/latency — 0.0303 misses/cycle at q=1 against 0.0019 at q=23, a
**16x** drop. Fewer requests in flight, each taking far longer.

That is the 57.1% memory-bound, and why no resource instrument saw it: none of
them measures time per access. Utilisation answers "how close to the ceiling";
latency answers "what does one access cost". On this workload they disagree
completely, which is the whole finding. Either curve alone is unremarkable.

`docs/latency_vs_bandwidth.svg` draws them together.

#### The denominator correction was immaterial, as predicted

DRAM now divides by the measured 184.2 ("measured mixed ceiling", preference 1),
and `CEIL_SRC` carries no comma, so §5.30's `.split(",")[0]` label defect
self-closed without a code change. Utilisation reads 1.2-7.4% against 1.7-4.1%
before. §5.27's pre-registered "DRAM is not the limit, about 2%" stands.

#### The latency validation FAILED as pre-registered. Reported, not patched.

Pre-registered in `counter_groups.sh`: 100-800 cycles on a 4 GiB dependent
chase. Measured **1203.4**. That is a fail as written.

It is not either failure mode the check was built for. §5.23's modes are "reads
0" (encoding unresolved) and "reads single digits" (resolved to the wrong
event). This read high, counted in full, and returned a physically meaningful
quantity. So the working set was swept:

| chase buffer | latency |
|---|---|
| 64 MiB | 381.9 cyc |
| 512 MiB | 323.9 cyc |
| 4096 MiB | 1136.8 cyc |

TLB-resident working sets land in the plausible range. The 4 GiB case is
inflated ~3.5x because a random chase through 4 GiB misses the TLB on nearly
every access and pays a page walk that itself misses. The counter tracks the
right quantity and responds to working-set size in the right direction.

**The threshold was misspecified, not the counter.** Stated plainly because the
distinction matters: the encoding is validated by a test written AFTER seeing
the data, which is weaker evidence than the pre-registration it replaces. The
pre-registered check did not pass. Anyone re-deriving this should re-specify the
threshold against a TLB-resident workload and re-run, rather than treat the
post-hoc discriminator as equivalent.

Coherence remains uninstrumented and unvalidatable — all three `bench_membw`
workloads are single-core, so they can only show the XSNP counters are not stuck
high, never that they are not stuck low. The patch stays in
`l2fwd/opt/counter-groups-latency-coherence.patch`, unapplied. Prerequisite is a
two-thread shared-line ping-pong arm where XSNP_FWD must approach the access
count. Latency alone explains the signature, so coherence is no longer needed to
close the question — only to decide whether it is a contributing cause.

#### Three defects found in prep, two of which would have produced plausible wrong numbers

1. **`validate_counters.sh` destroys `.dram_ceiling`.** It ends a block with
   `} > "$HERE/.dram_ceiling"`. Running it blanked `DRAM_ACHIEVED_GBS` back to
   empty, discarding the measured 184.2. Backed up beforehand and restored; also
   in git at `94742ea`. Had it not been, the sweep would have fallen through to
   `PEER_READ_24=227.53` and reproduced §5.30's exact defect, silently, in the
   session that fixed it. **Back up `.dram_ceiling` before running
   `validate_counters.sh`.**
2. **`saturation.sh` had no check on the launch.** `exit 111` was set on a failed
   cgroup join but `wait $RUN_PID` discarded the status. A denied join means
   l2fwd never runs, the log is empty, every counter reads an idle core, and 35
   runs complete "successfully" with very low, very plausible utilisations. Now
   captured and aborts. §5.23 and §5.26 again: an instrument must be able to say
   it did not measure.
3. **`validate_counters.sh` could never have run.** `BM` defaulted to
   `$HERE/../bench_membw` = `NetBlast/bench_membw`; the binary is at
   `NetBlast/l2fwd/bench_membw`. Every default invocation died on its own FATAL
   before reading a counter. Fixed.

`saturation.sh` also no longer uses `sudo systemd-run --scope` (§5.28's denied
call). It joins `bench.slice` by writing its own pid to `cgroup.procs` then
`exec`s; `taskset` inside the slice is equivalent to `-p AllowedCPUs=`. Not
routed through `benchctl run`, whose ~6 s preflight x 35 invocations would cost
3.5 minutes to re-check something meaningful once.

#### What is now open

The forwarder at high core counts is latency-bound, not bandwidth-bound. Open:
what *drives* latency from 75 to 700 cycles while bandwidth stays under 8%.
Queueing at the memory controller from 23 concurrent streams is the obvious
candidate; inter-core coherence is the other, and is still uninstrumented.
Distinguishing them needs the coherence arm above.

#### Repository additions

- `l2fwd/plot_latency_saturation.py` — `docs/latency_vs_bandwidth.svg`. Its own
  figure rather than a series on `saturation.svg`, because that plot's axis is
  "percentage of a known ceiling" and latency has no ceiling; forcing it there
  would mean inventing a denominator, which is §5.30's failure exactly.
  Refuses to plot at all if `.dram_ceiling` carries no measured value.
- `l2fwd/saturation_out/` — 35 runs, raw `.perf` per run, committed so this
  sweep can be certified directly rather than by timestamp inference.
