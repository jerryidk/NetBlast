# Investigation: performance collapse at specific queue-pair counts

Running log of the diagnosis and of every modification made. Newest sections are
appended; nothing is rewritten after the fact.

---

## 1. What the data actually shows

`docs/results.json`, in Mpps (unit confirmed at `l2fwd/main.c:207`,
`printf("\n%.2f Mpps", mpps)`):

| queues (`-q`) | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| maglev avg | 13.2 | 27.7 | 44.5 | 57.0 | 72.1 | 85.1 | 81.3 | **1.7** | 69.5 | **2.1** |
| dramblast avg | 19.9 | 39.2 | 60.0 | 77.1 | 74.2 | **1.0** | **1.0** | **1.1** | **1.0** | **1.0** |

This is not a dip. Both modes scale near-linearly, then fall ~70x to a floor.
dramblast never recovers after 6; maglev's `9` entry is
`min=1.0, max=115.0, avg=69.5`, a single run oscillating between the floor and
full speed — the signature of an intermittent failure, not a scaling limit.

The floor value is quantized: `samples` is `uint32_t *` (`l2fwd/main.c:166`) but
`mpps` is a `double` (`l2fwd/main.c:208`), so each sample truncates to a whole
Mpps. `min=1.0` means "in [1,2) Mpps"; anything below 1 Mpps reads as 0.

---

## 2. Diagnosis

### Hypothesis 1: NUMA or hyperthread boundary crossed at the cliff — KILLED

`run.sh:22-24` selects even-numbered CPUs (`0,2,4,...`), which would be a classic
way to stumble across a socket boundary partway through a sweep.

Killed by the topology. `lscpu -p=CPU,CORE,SOCKET,NODE` shows a **single socket,
28 physical cores, 1 NUMA node**, with CPU *k* and *k+28* as hyperthread
siblings. CPUs 0-27 are therefore all distinct physical cores, and `-q 10` only
reaches CPU 18. The sweep never crosses a NUMA node and never doubles up on a
hyperthread. All four E810 ports report `numa_node=0`.

### Hypothesis 2: the queue-to-lcore mapping is short by one — CONFIRMED

`run.sh:22-24` builds `-l` as `seq -s, 0 2 $(( (N-1)*2 ))` — exactly **N**
lcores — and passes `-q N`. But `main.c:513-538` assigns one lcore per queue
while skipping the main lcore (`main.c:523`), leaving **N-1** workers for N
queues.

Measured on `node0`:

```
$ ./run.sh 4 dramblast
Lcore 2 assigned to Port 0 Queue 0
Lcore 4 assigned to Port 0 Queue 1
Lcore 6 assigned to Port 0 Queue 2
EAL: Error - exiting with code: 1
  Cause: Not enough cores
```

Reproduced at N=1, N=2 and N=6 — always exactly N-1 queues assigned, then
`rte_exit`. **The committed `run.sh` cannot run at any core count.** Every
version of `run.sh` in git history has the same `-l`/`-q` pairing, so this is not
a recent regression of the script.

Control: with `-l 0,2,4,6,8` (N+1 lcores) and `-q 4` it starts cleanly, assigns
all four queues, and reports `Port 0 Link UP - 100000 Mbps`.

### Consequence: `results.json` was produced by different code than HEAD — CONFIRMED

If the committed code always dies, it cannot have produced a results file with
ten populated rows. Tracing when the skip was introduced:

```
$ git log -S "SKIP_MAIN" -- l2fwd/main.c
91d2c14
$ git log -S "rte_get_main_lcore()" -- l2fwd/main.c
91d2c14 ...
```

`91d2c14` is the *same commit* that added `docs/results.json` and
`docs/performance_plot.png`.

In its parent `7e11fc8` the assignment loop has **no** main-lcore skip:

```c
while (rte_lcore_is_enabled(rx_lcore_id) == 0 ||
       lcore_queue_conf[rx_lcore_id].n_rx_port == MAX_RX_QUEUE_PER_LCORE) {
```

and the launch was `rte_eal_mp_remote_launch(l2fwd_launch_one_lcore, NULL, CALL_MAIN)`
(`7e11fc8:l2fwd/main.c:684`), with the stats timer *inside* the worker loop
guarded by `if (lcore_id == rte_get_main_lcore())`.

So the data came from a working configuration where **N lcores served N queues**,
lcore 0 both forwarding queue 0 and printing stats once per second. Commit
`91d2c14` changed the threading model and committed results from the old model in
the same commit. The plot's x-axis is meaningful for the data it was made from
(N cores = N queues = N polling lcores), but HEAD cannot reproduce it.

### Control: is the floor a crashed run? — NO

With the link up and no generator running, a full 32-second run reports
`Minimum: 0 / Maximum: 0 / Average: 0.00`. A dead run reports zero, so the `1.0`
rows in `results.json` are genuine forwarding at ~1-2 Mpps, not a crash or an
idle process. Whatever causes the collapse leaves the forwarding path alive but
~70x slower.

### Reproduction with the corrected invocation: the collapse does NOT reproduce

Because the committed `run.sh` cannot run, the sweep was re-run with the minimal
correction implied by the code: **N+1 lcores for N queues**
(`-l $(seq -s, 0 2 $((q*2)))`, `-q $q`), leaving `main.c` untouched.

Generator on `node1`, matching the configuration `docs/data.txt` describes:
`-l 0-16` (8 TX + 8 RX workers), `-r 2097152`. It reports `Max Flows: 16777216`
= `(1<<20)*16` and offers **93.28 Mpps / 100.00 Gbps** — full line rate for these
110-byte frames, and above the 85 Mpps peak of the committed data.

32-second runs, Mpps:

| queues | dramblast avg | committed | maglev avg | committed |
|---|---|---|---|---|
| 1 | 27.84 | 19.9 | 16.81 | 13.2 |
| 2 | 54.74 | 39.2 | 34.23 | 27.7 |
| 3 | 82.48 | 60.0 | 51.71 | 44.5 |
| 4 | 92.68 | 77.1 | 68.68 | 57.0 |
| 5 | 92.77 | 74.2 | 85.68 | 72.1 |
| 6 | 92.84 | **1.03** | 92.71 | 85.1 |
| 7 | 92.87 | **1.03** | 92.77 | 81.3 |
| 8 | 92.90 | **1.06** | 92.84 | **1.68** |
| 9 | 92.94 | **1.03** | 92.87 | 69.5 |
| 10 | 92.94 | **1.03** | 92.97 | **2.06** |

No collapse at any queue count, in either mode. Both reach line rate and stay
there. maglev reaches 85.68 Mpps at q=5 and 92.71 at q=6, so the committed data's
85.06 peak is comfortably reachable — the offered load is no longer the limiting
factor, and the collapse still does not appear.

An earlier sweep at a lower offered load (72 Mpps, the committed single-TX-core
generator) gave the same answer, and is retained in
`docs/results_reproduced.json` under `capped_72mpps`. That run saturated from q=3
onward, so it could not settle the question on its own; the line-rate run can.

### Control: is any of this a generator artifact? — NO

The line-rate sweep was repeated with the generator cut from 8 TX cores to
**2 TX cores** (`-l 0-4`), holding both offered load and flow count constant.
Two TX cores already reach 93.28 Mpps / 100.00 Gbps, so the extra six were never
needed for load.

Holding flows constant required compensating `-r`, because it is per TX core:
`-r 8388608` x 2 TX = 16,777,216, the same `Max Flows` the 8-TX-core run printed.
Without that compensation the 2-core run would have had 4M flows instead of 16M
and would not have been comparable.

Forwarded Mpps, 8 TX vs 2 TX generator:

| queues | dramblast 8TX | dramblast 2TX | maglev 8TX | maglev 2TX |
|---|---|---|---|---|
| 1 | 27.84 | 27.84 | 16.81 | 16.81 |
| 3 | 82.48 | 82.61 | 51.71 | 51.71 |
| 5 | 92.77 | 92.77 | 85.68 | 86.87 |
| 7 | 92.87 | 92.87 | 92.77 | 92.77 |
| 10 | 92.94 | 92.97 | 92.97 | 92.90 |

dramblast agrees to within 0.15% at every point; maglev's worst disagreement is
1.4% at q=5, its steepest unsaturated point. Per-packet cost agrees closely in
the clean region (dramblast 54-56 vs 55-56 cycles at q=1-3, maglev 102-103 vs 103)
and is noisier in the saturated region where bursts are 1-7 packets — e.g.
dramblast q=8 reads 117 vs 144 cycles. That noise is expected: with a burst of
one packet, a single poll's timing lands entirely on one packet.

So the results are a property of the forwarder, not of how the load was
generated. This is also the third independent confirmation that no collapse
occurs — at 72 Mpps, at line rate with 8 TX cores, and at line rate with 2 TX
cores.

Practical consequence: **use `-l 0-4` for future runs.** It is indistinguishable
in results, and the 16-core generator loads the generator host far more heavily
(load average ~14 versus ~2.6) for no measurement benefit.

### Per-queue capacity, now measurable

Below saturation each added queue pair contributes a near-constant amount, so the
unsaturated slope gives the per-core capacity the committed plot was trying to
show:

* **dramblast** ~27.8 Mpps per queue pair, saturating the link at q=4
* **maglev** ~17.1 Mpps per queue pair, saturating at q=6

Consistent with the per-packet cost in the unsaturated region, where burst size
is pinned at 64 and the measurement is clean: dramblast 55-56 cycles/packet
against maglev 103, a 1.85x ratio.

### A real queue-count-dependent cost, isolated

Although the collapse does not reproduce, the diagnostics show a genuine effect
that does get worse with queue count. Comparing per-packet cost against the
average RX burst size, at line rate:

| queues | dramblast cyc/pkt | dramblast batch | maglev cyc/pkt | maglev batch |
|---|---|---|---|---|
| 1 | 55 | 64 | 103 | 64 |
| 3 | 56 | 64 | 103 | 64 |
| 5 | 82 | 15 | 103 | 64 |
| 6 | 98 | 8 | 111 | 23 |
| 8 | 117 | 1 | 122 | 7 |
| 10 | 178 | 3 | 131 | 4 |

Spreading a fixed offered load over more queues shrinks each queue's burst size,
because each core polls more often and finds fewer packets. Once that happens,
dramblast's per-packet cost rises **3.2x** (55 -> 178) while maglev's rises only
**1.3x** (103 -> 131).

Two claims here, at different confidence levels — worth keeping separate.

**Established by measurement:** the cost is something dramblast does **once per
burst** rather than once per packet. Supporting evidence is that dramblast's cost
stays flat at 55-56 ticks while its burst size stays at 64 (q=1-3) and only
climbs once the burst collapses, i.e. the cost tracks burst size rather than
queue count directly; and that maglev, with no per-burst work of this kind, stays
nearly flat under an identical burst collapse.

**Hypothesised, NOT yet tested:** that the per-burst work in question is the
`aligned_alloc(64, ...)` + `free()` in `dramblast_process_frames`
(`dramblast.c:202-203`, `:231`). It is the most conspicuous per-burst cost in
that function, but "most conspicuous" is not evidence, and nothing measured so
far distinguishes it from any other per-burst work in the same path.

**The experiment that would settle it** (suggested by a peer session and adopted
here): hoist the results buffer out of the per-burst path — it is bounded by
`MAX_PKT_BURST`, so it can be a per-lcore scratch buffer allocated once at init
instead of per call. If the allocator is the cause, dramblast's 3.2x should
collapse toward maglev's 1.3x. If it does not move, the attribution is wrong and
the cost is elsewhere in the per-burst path. This is a single run that either
confirms or kills the hypothesis, and it is worth more than any further
measurement of the symptom. Not done — it is a source change awaiting approval.

Until that runs, the allocator should be described as a candidate, not a cause.

#### Attempting to size the per-burst cost — the model does not hold up

Before running the experiment, the existing data was fitted to the obvious model
`ticks_per_pkt = W + C/batch`, where `C` is the per-burst cost. Two problems
surfaced, and both argue the current data cannot settle the attribution:

* **The fit is unstable.** Least squares over all ten dramblast points gives
  `C = 66` ticks (31 ns per burst) — strikingly consistent with a glibc
  `aligned_alloc`+`free` round trip on a hot tcache path (~20-40 ns). But fitting
  only the two extremes (q=1 batch 64, q=10 batch 3) gives `C = 387` ticks
  (184 ns), nearly 6x larger. A model that swings 6x depending on which points
  are used is not measuring a physical constant.
* **maglev fits a *larger* per-burst cost than dramblast** — 116 ticks vs 66 —
  despite having no per-burst allocation at all. If the fitted `C` were capturing
  the allocator, this could not happen. It is more likely absorbing frequency
  drift (which rises with q for both modes) plus noise.

A contributing cause is that `Average rx batch sz` is a poor regressor: it is
computed from a single one-second delta with integer division (`main.c:196-197`),
so it is a crude snapshot, not a mean over the run. The reported series is
visibly noisy — dramblast reads batch 1 at q=8 but 6 at q=9.

**What survives:** the *observed* asymmetry — dramblast's cost rising 3.2x while
maglev's rises 1.3x under a comparable burst collapse — is a measurement, not a
model, and it stands. What does not survive is any quantitative claim about the
size of a per-burst cost derived from these fits. The direct experiment is
therefore the only way forward.

**A prediction recorded in advance.** If the fitted `C` is largely absorbing
frequency drift, then once per-q frequency samples exist, refitting on
frequency-corrected ticks should shrink `C` in **both** modes, and shrink it more
in **maglev** (which has no per-burst allocation to leave behind). If a refit
does not behave that way, this explanation is also wrong and should be discarded
rather than patched. Writing the prediction down before the data exists is the
point — the earlier `C = 66` result shows how readily a fit lands on whatever
mechanism is already suspected.

#### Controls the experiment must carry

A negative result ("buffer hoisted, 3.2x unmoved") is only meaningful if the
experiment could have detected the effect had it been present. Three controls,
decided in advance:

1. **Structural verification.** `objdump -d` the rebuilt binary and confirm no
   `call` to `aligned_alloc`/`malloc`/`free` remains inside
   `dramblast_process_frames`. Source-level reasoning is not sufficient evidence
   about generated code — a peer session on this machine found GCC had hoisted
   loads out of a loop, silently invalidating a source comment that asserted a
   concurrency property. The same class of gap produced the "cycles per packet"
   mislabel here: the name described intent, the artifact did something else.
2. **Signature check.** Removing a per-burst cost must reduce the measurement
   *more at small batch than at large*. Predicted: negligible change at q=1
   (batch 64), a clear drop at q=9-10 (batch 3-6). A uniform drop across all q
   would indicate something other than a per-burst effect changed.
3. **Amplification as a positive control.** Build a third variant with a second,
   redundant `aligned_alloc`/`free` per burst. If adding one allocation produces
   no measurable rise, the measurement lacks the sensitivity to detect removing
   one, and a null result is uninformative rather than evidence of absence.

Control 3 is the load-bearing one: it converts "we saw no effect" into "we saw no
effect and we know we could have."

The two curves cross at q=7: maglev is ~1.9x more expensive per packet at low
queue counts, dramblast ~1.35x more expensive by q=10. A constant offset would
not do that.

For scale, `docs/data.txt`'s original 56-core run recorded **4978** cyc/pkt for
dramblast against **491** for maglev — the same asymmetry, an order of magnitude
further along, consistent with 56 queues each seeing near-empty polls.
### Open leads, not yet tested

Ranked by how well each matches a *cliff* rather than a gradual slope.

1. **maglev's probe-chain divergence.** `maglev_hashmap_insert`
   (`hashmap.c:27-53`) probes linearly up to `CAPACITY` = 512M slots, while
   `maglev_hashmap_get` (`hashmap.c:55-71`) stops at the first zero key. Under
   concurrent CAS inserts these disagree: a lookup can miss a key that exists,
   re-insert it, and lengthen the chain. A per-packet cost that grows without
   bound is the right shape for a cliff, and it worsens with core count.
2. **Per-burst heap traffic in dramblast.** `dramblast_process_frames` does
   `aligned_alloc(64, ...)` + `free()` on **every RX burst**
   (`dramblast.c:202-203`, `:231`) — ~1.2M allocator round-trips/s/core at
   77 Mpps, through the shared glibc heap.
3. **The two modes allocate their 8 GiB table completely differently.** Both run
   with `-c 536870912` (`run.sh:38`) = 2^29 x 16 B = 8 GiB. dramblast uses
   `mmap(..., MAP_HUGETLB | MAP_HUGE_1GB)` (`dramblast.c:253`); maglev uses plain
   `aligned_alloc(4096, ...)` (`maglev.c:43-44`), so 4 KiB pages unless THP is
   `always`. This is a large uncontrolled difference between the two curves.
4. **False sharing in dramblast's per-lcore queues.** `dramblast_queue_t`
   (`dramblast.h:35-40`) is 24 bytes, unpadded, in a flat array indexed by
   **`lcore_id`** rather than a dense 0..N-1 index (`main.c:333`).
   `find_queue_head`/`tail` are written on every push and pop. At a 24-byte
   stride, lcores 2/4, 8/10, 10/12 and 16/18 share 64-byte lines; the (8,10)
   collision first appears at N=6, where dramblast falls off. Suggestive, but
   false sharing alone cannot explain 74 -> 1 Mpps.

### Measurement-harness defects — quantified, and worked around

Three defects in l2fwd's own reporting were found by reading, then measured.
They corrupt the numbers regardless of the underlying cause. Prompted by a
question from a peer session running an unrelated benchmark on the same silicon,
the per-second sample series in the run logs was checked directly, which
confirmed all three and gave an exact model.

**1. Truncation.** `samples` is `uint32_t *` (`main.c:166`) but a `double` Mpps
value is stored into it (`main.c:208`), so every sample is `floor()`ed. This is a
fixed ~0.5 Mpps downward bias, which is only -0.3% at 93 Mpps but **-4.6% at
17 Mpps** — worst precisely where the unsaturated per-queue slope is measured.

**2. Cold first sample.** Sample 0 is always 12-19% below steady state and
therefore always sets the reported `Minimum`. The published min/max range is a
startup artifact, not run-to-run spread.

The two together give an exact model of the reported average. For dramblast
q=10 the samples are `91.97` then `93.28` x30; predicted mean of the floor()ed
values is `(30*93 + 91)/31 = 92.94`, and the harness reports **92.94**.

| run | sample 0 | steady (median of warm) | reported avg | error |
|---|---|---|---|---|
| dramblast q=1 | 23.30 | 28.62 | 27.84 | -2.71% |
| dramblast q=10 | 91.97 | 93.28 | 92.94 | -0.36% |
| maglev q=1 | 12.89 | 17.62 | 16.81 | **-4.60%** |
| maglev q=10 | 90.34 | 93.28 | 92.97 | -0.33% |

**3. Interval assumed exactly 1 s.** `double t_s = timer_period / rte_get_timer_hz()`
(`main.c:205`) is integer division, so the denominator is exactly 1.0 even when
the real interval drifts longer. A slightly long interval therefore inflates the
rate — which is how maglev q=10 reports a `Maximum` of 95 Mpps on a link whose
physical ceiling is 93.28.

**4. Batch size is integer-divided — a quantisation floor exactly where it hurts.**
`main.c:196` computes `(agg.rx - prev_agg.rx) / (agg.rx_cnt - prev_agg.rx_cnt)`
with both operands `uint64_t`, so the result truncates. At the large batches seen
at low q this is harmless (a true 64.9 reports 64, ~1.4% error), but at the small
batches seen at high q it destroys most of the signal — a true 1.9 reports 1, a
47% error. It is also a single one-second delta rather than a mean over the run,
which is why the series reads batch 1 at q=8 and batch 6 at q=9.

This matters more than a noisy regressor normally would: the per-burst term
`C/batch` has almost all of its leverage at small batch, so the regressor is
least trustworthy exactly where the model most depends on it. Any future fit
should report batch as a `double` and accumulate it over the whole run.

**The pattern.** Four reporting defects, three of which are the same mistake:
integer truncation applied to a quantity that is not an integer — `samples`
(`main.c:166`/`:208`, `double` into `uint32_t`), `t_s` (`main.c:205`,
`uint64_t/uint64_t`), and batch size (`main.c:196`, `uint64_t/uint64_t`). Each
degrades gracefully and stays invisible at large magnitudes, which is why all
three survived. Worth assuming any other derived statistic in this harness has
the same shape until checked.

Also: no bounds check on `SAMPLE_SIZE` against `TOTAL_SAMPLES` (`main.c:208`).

**Worked around without re-running.** The per-second `"%.2f Mpps"` lines
(`main.c:207`) are printed *before* truncation, so full precision is recoverable
from the existing logs. `extract_results.py` now records `steady_mpps` — the
median of the warm samples, excluding sample 0 — which is immune to all three
defects. All headline figures use it. Corrected per-queue capacity:

* **dramblast** ~27.8 Mpps per queue pair (was measured as 27.8 off biased data;
  the q=1 point rises from 27.84 to 28.62)
* **maglev** ~17.4 Mpps per queue pair (q=1 rises from 16.81 to 17.62)

maglev's corrected series is near-perfectly linear — 17.62, 34.99, 52.34, 69.59,
86.94, i.e. increments of 17.37, 17.35, 17.25, 17.35 — which the biased data
obscured.

### "Cycles per forwarded packet" is TSC ticks, not core cycles

`main.c:305`/`:355` measure with `rte_rdtsc()`, and this CPU reports
`constant_tsc` / `nonstop_tsc` with the TSC pinned at its 2.1 GHz nominal
(`rte_get_tsc_hz: 2100000000`) while cores boost to 3.7 GHz under
`powersave` with `no_turbo=0`. So the metric l2fwd prints as "Cycle per fwd
packet" is really **TSC ticks, i.e. elapsed time**, and understates true core
cycles by the boost ratio.

Consequences:

* Comparisons **between modes at the same queue count** stay valid — both run
  under identical frequency conditions. The dramblast-vs-maglev differential
  (3.2x vs 1.3x) is therefore unaffected.
* Comparisons **across queue counts** are weaker than they look: more active
  cores means less turbo headroom, so part of the rise from q=1 to q=10 may be
  frequency drop rather than per-packet work. Evidence that this is not
  dominant: dramblast's cost is flat at 55-56 ticks across q=1-3 while its burst
  size stays at 64, where a frequency effect would already have shown up.
* To settle it, re-run with frequency pinned (`scripts/constant_freq.sh`, which
  writes every CPU's `scaling_max/min_freq` and disables turbo) or derive cycles
  from `APERF`/`MPERF`.

**Size of the correction.** A peer session benchmarking on the same SKU sampled
`scaling_cur_freq` on its pinned core during a run and found it held at
3,700,000 kHz — pegged at max turbo — for the whole measurement. At that
frequency the tick-to-cycle factor is the full **1.762x** (3.7 / 2.1), so a
printed cost of 55 ticks is really ~97 core cycles.

That measurement was **single-core**, though, and does not transfer directly to
this sweep. All-core turbo is lower than single-core turbo, so as q rises from 1
to 10 the actual frequency falls and the correction factor shrinks with it. This
is exactly the mechanism that makes the across-queue-count comparison suspect:
part of the measured rise in ticks-per-packet is the clock slowing down, not more
work being done. Quantifying it requires sampling `scaling_cur_freq` per queue
count during a sweep, which has not been done.

When that sampling is added, read **every active core and take the median**, not
one core. Under an all-core load the turbo bins can differ between cores, so
dividing an aggregate packet count by a single core's clock reintroduces a
smaller version of the same error. `scaling_cur_freq` is a sysfs read needing no
MSR access, which matters here because the `msr` module is not loaded.

**Which comparisons this defect actually bites** is decided by the axis compared
along, since a ratio is only safe when conditions — frequency included — are
identical at both ends:

| comparison | frequency held? | verdict |
|---|---|---|
| dramblast vs maglev at fixed q | yes, same core count | clean |
| dramblast q=1 vs q=10 | no, 1 vs 10 busy cores | inherits drift; 3.2x is an upper bound |
| throughput in Mpps, any axis | n/a — packets over wall-clock | unaffected |

Figures now label this axis "TSC ticks per forwarded packet (2.1 GHz)" rather
than "cycles".
* The generator does not match its own documentation. `docs/data.txt` (deleted in
  `db23607`, recoverable via `git show f56827b:docs/data.txt`) describes an
  8-core generator; `pktgen/run.sh` uses `-l 0-2`, yielding
  `rte_lcore_count()-1` = 2 workers split 1 TX / 1 RX (`pktgen.c:308`).

---

## 3. Machine modifications

Both nodes received identical treatment. Source changes live on branch
`fix/sweep-invocation-and-measurement`: `d68a884` (run.sh core list), `7ebf038`
(Mpps samples as double), `455eede` (pinned 2-queue-pair generator).

### 3.1 Enable the IOMMU (persistent, required a reboot)

`vfio-pci` needs an IOMMU and `/sys/kernel/iommu_groups/` was empty, despite the
ACPI DMAR tables being present.

`/etc/default/grub`, **line 11 only**:

```diff
- GRUB_CMDLINE_LINUX_DEFAULT=""
+ GRUB_CMDLINE_LINUX_DEFAULT="intel_iommu=on iommu=pt"
```

Placed in `GRUB_CMDLINE_LINUX_DEFAULT` near the top of the file rather than
appended at the end, because the file carries an Emulab warning that a trailing
slicefix block re-assigns `GRUB_CMDLINE_LINUX` and strips anything added after
it. GRUB concatenates both variables, so this survives slicefix and leaves the
serial-console and `emulabcnet` settings CloudLab needs intact.

Verified post-reboot on both nodes: 133 IOMMU groups, `DMAR: IOMMU enabled`.

Backup: `/etc/default/grub.bak-netblast`.
Revert: `sudo cp /etc/default/grub.bak-netblast /etc/default/grub && sudo update-grub`, reboot.

### 3.2 Hugepages (runtime, resets on reboot)

```
sudo ./scripts/reserve_hugepages.sh 16 4096     # 16 x 1 GiB + 4096 x 2 MiB = 24 GiB
```

Explicit arguments rather than the script's defaults (`32` 1 GiB + `40000` 2 MiB
= ~112 GB of 125 GB), which would starve maglev of the 8 GiB of *ordinary* heap
it needs. 24 GiB covers dramblast's 8 GiB table plus DPDK's `-m 2000`.

**Side effect that is a measurement confound:** the script also flips

```
/sys/kernel/mm/transparent_hugepage/enabled : madvise -> always
/sys/kernel/mm/transparent_hugepage/defrag  : madvise -> always
```

This decides whether maglev's `aligned_alloc` table lands on 4 KiB or 2 MiB
pages, and `defrag=always` makes THP allocation synchronously compact memory,
which can stall a forwarding core. Left at the script's values so reproduction
matches the original runs, but it must be controlled before trusting any
maglev-vs-dramblast comparison.

Revert: reboot.

### 3.3 NIC binding (runtime, resets on reboot)

```
nix develop . -c bash scripts/bind-dpdk-devices.sh vfio-pci enp23s0f0
```

Binds **only** `0000:17:00.0` (the 100 GbE experiment link) to `vfio-pci`, on
both nodes. This drops `10.10.1.1`/`10.10.1.2`, so `ssh node1` stops working —
use the control path `node1.no-link-e810.gpu-coherence-pg0.utah.cloudlab.us`.

Revert: `sudo dpdk-devbind.py --bind=ice 0000:17:00.0 && sudo ip link set enp23s0f0 up`.

### 3.4 `/etc/modules-load.d/vfio-pci.conf`

Created, then found to be unnecessary — `vfio_pci` is built into this kernel.
Harmless no-op, recorded so it is not mistaken for load-bearing config.
Revert: `sudo rm /etc/modules-load.d/vfio-pci.conf`.

### 3.5 Repository additions

New files only; nothing existing was edited.

| File | Purpose |
|---|---|
| `docs/INVESTIGATION.md` | this log |
| `l2fwd/sweep.sh` | **the corrected invocation** — sweep driver, N+1 lcores per N queues |
| `l2fwd/extract_results.py` | parses sweep logs into `results_reproduced.json` |
| `l2fwd/plot_sweep.py` | regenerates the two figures from the two JSON files |
| `docs/results_reproduced.json` | measured sweep data, both load conditions |
| `docs/queue_sweep_reproduction.png` | committed vs measured, per mode |
| `docs/per_packet_cost.png` | cycles/packet and RX burst size vs queue count |

`l2fwd/sweep.sh` is the durable record of how every measurement in this document
was produced. It is `run.sh` generalised over queue count with exactly one
logic change, and it carries the reasoning in its header.

### 3.6 Fix to `run.sh` — APPLIED as commit `d68a884`

The correction used throughout this investigation is a one-line change:

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

Effect: `N=4` goes from `0,2,4,6` (4 lcores, 3 workers, dies) to `0,2,4,6,8`
(5 lcores, 4 workers, runs). Verified against live line-rate traffic before
committing: `./run.sh 4 dramblast` now assigns all four queues and reports
92.68 Mpps. Note this changes what `run.sh`'s argument *means* —
it currently reads as "number of cores" but has always been passed straight to
`-q` as the queue count, and the two differ by the main lcore. Renaming the
variable to `NUM_QUEUES` would make that honest, but is a larger edit.

`docs/results_reproduced.json` carries `min`/`max`/`avg` Mpps plus
`cycles_per_pkt`, `rx_batch`, `rx_missed` and `fwded` for every queue count, so
the diagnostics are reproducible without the raw logs.

Regenerate with `nix develop .. -c python3 plot_sweep.py` from `l2fwd/`.

The original `docs/performance_plot.png` and `docs/results.json` were left
untouched, since they are the evidence under investigation.

---

## 4. Open questions

1. ~~Generator: run as committed or scaled to 8 cores?~~ **RESOLVED** — scaled to
   `-l 0-16` (8 TX + 8 RX), which reproduces `docs/data.txt`'s 16M flows exactly
   and offers 100 GbE line rate. `pktgen/run.sh` still carries the 2-worker
   `-l 0-2`; updating it would make the committed generator match its own
   documentation, but that is a source change and has not been made.
2. **Highest value remaining: test the allocator attribution.** Hoist the
   `aligned_alloc`/`free` out of `dramblast_process_frames` into a per-lcore
   scratch buffer allocated once at init (it is bounded by `MAX_PKT_BURST`), then
   re-run the sweep. If dramblast's 3.2x collapses toward maglev's 1.3x the
   attribution is confirmed; if it does not move, the cause is elsewhere in the
   per-burst path and the write-up must be corrected. One run, decisive either
   way. Source change — needs approval.
3. May `main.c` be modified for the measurement fixes (`samples` as `double`,
   bound `SAMPLE_SIZE`)? Currently worked around in post-processing, which is
   sufficient but leaves the harness itself still producing biased numbers for
   anyone who reads its output directly.
4. Add per-queue-count frequency sampling (median over active cores) to separate
   real per-packet work from turbo drift across the sweep. Cheap, no source
   change to `l2fwd` — the sweep driver can sample sysfs alongside each run.
5. Optionally pin frequency (`scripts/constant_freq.sh`) for a clean absolute
   cycle count. Machine-wide change; coordinate if the box is shared.
6. Reproduce against `7e11fc8` (faithful to the data) or against a corrected HEAD
   invocation with N+1 lcores (faithful to current code)? These measure different
   threading models.
7. Apply the one-line `run.sh` fix in §3.6.
