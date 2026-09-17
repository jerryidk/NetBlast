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

#### RESULT: the model does hold up, once the clock is fixed

The section above concluded that `ticks_per_pkt = W + C/batch` could not be
fitted stably and that "the current data cannot settle the attribution". That
conclusion was correct **about the turbo data** and wrong as a general claim. Re
fitted on the pinned arm, where TSC ticks *are* core cycles and there is no
frequency drift across queue counts to absorb, the model is well behaved:

| estimator | dramblast `C` | maglev `C` |
|---|---|---|
| least squares, all 10 points | **771** cycles/burst | 71 cycles/burst |
| least squares, only the 4 post-knee points | 739 | — |
| two-point, the extremes | 794 | 98 |
| `R^2` (all points) | **0.9946** | 0.68 |

Three estimators within **7%** for dramblast, against a **5.9x** swing on the
same model fitted to turbo data. The instability was the frequency drift, not the
model. maglev's low `R^2` is not a failure: its cost is flat, so there is almost
no variance for the model to explain -- which is itself the asymmetry, stated
numerically. **dramblast's per-burst cost is 11x maglev's.**

**A pre-registered prediction that half failed -- reported, not patched.** The
prediction recorded above was that refitting on frequency-corrected data should
shrink `C` in **both** modes and shrink it **more in maglev**, and that "if a
refit does not behave that way, this explanation is also wrong and should be
discarded rather than patched." Outcome:

* maglev: `C` fell 116 -> 71. As predicted.
* dramblast: `C` rose 66 -> 771. **Not as predicted, by an order of magnitude.**

So the "fitted `C` was mostly absorbing frequency drift" explanation is
discarded. The better account is that the turbo fit was not one biased estimate
but an unstable one, and its least-squares value of 66 happened to land on the
low end -- which is precisely why it looked so convincing against a hot-tcache
allocator round trip. In fairness to the prediction, it was also badly posed: the
pinned arm is not the turbo data with a correction applied, it is a different
dataset with different burst sizes, so "refit the same points" was never
something this experiment could do.

#### This substantially downgrades the allocator hypothesis

With `C` now measured rather than fitted unstably, it can be checked against the
mechanism it was attributed to:

    C = 771 cycles/burst at 2.100 GHz = 367 ns per burst
    glibc aligned_alloc + free, hot tcache path = 20-40 ns = 42-84 cycles

**The allocator can account for at most ~11% of the per-burst cost.** The
`aligned_alloc`/`free` pair in `dramblast_process_frames` (`dramblast.c:202-203`,
`:231`) therefore cannot be the explanation, whatever else it is. The 3.2x rise
is real, the per-burst character of it is now firmly established by a clean
linear fit, but the attribution to the allocator is the weakest link and the
number says so.

Note what killed it: not an argument, and not the hoist experiment, but putting
the machine in a state where the existing measurement became interpretable. The
earlier `C = 66` matching an allocator round trip "to the nanosecond" was the
plausibility trap this document has now recorded three times.

> **REVERSED by §5.13, and the reversal is the more useful finding.** The
> 20-40 ns reference is the wrong reference: this call asks for 64-byte
> alignment, which in glibc 2.33 never reaches the tcache fast path and goes
> through `_int_memalign` under the arena lock instead. Measured directly rather
> than argued from a published constant, one pair costs **462-527 cycles —
> 64-73% of the per-burst cost, not 11%.** Everything in this subsection is left standing
> because how it was wrong matters: the measurement was correct and was compared
> against the wrong reference, which no amount of measurement hygiene catches.

**Consequence for the experiment design.** A plain hoist is now a *weak* test: if
the allocator is ~11% of `C`, removing it should move `C` by ~80 cycles, against
a 7% estimator spread of ~50. That is marginal. The **amplification arm** becomes
the primary measurement instead -- inject `N` extra `aligned_alloc`/`free` pairs
per burst and check that `C` rises by `N x ~60` cycles. At `N = 8` that is ~480
cycles, far above the noise floor, and it calibrates the allocator's true
per-burst contribution on this machine rather than relying on a literature value
for the tcache path. The hoist then becomes the confirmation, with the
amplification arm supplying the expected effect size.

> **Also wrong, per §5.13.** The hoist was not weak — it resolved the allocator
> immediately, at 8.25 ticks per packet at burst 64, which is 527 ± 48 cycles
> per burst and many times the run-to-run floor §5.15 later measured. The
> `~60 cycles per pair` expectation understated the truth by a factor of eight
> (an incremental pair costs about 509). The amplification arm earned its place
> as a calibration, not because the hoist lacked power.

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

#### Pre-registered: what would downgrade the hoist from proof to corroboration

The removal arm is only clean if the hoist removes the *mechanism* and not
merely *some code*. A peer session's prefetch-removal control was airtight
because its instruction count was unchanged across the manipulation
(9,915,528,371 vs 9,915,527,336); mine cannot be that clean, because removing an
`aligned_alloc`/`free` pair necessarily removes instructions and may let GCC
reorder around the now loop-invariant pointer.

So the threshold is fixed **here, before the experiment runs**, since choosing it
afterwards would mean choosing it in full knowledge of which answer it licenses:

* **Primary criterion — the delta must have the right shape, not merely the right
  size.** A genuine per-burst removal must reduce instructions per packet
  *proportionally to 1/batch*: near-invisible at q=1 (batch 64, where the cost is
  amortised 64-fold) and large at q=9-10 (batch 3-7). If instructions per packet
  instead fall roughly uniformly across all q, the change is code generation, not
  mechanism removal, regardless of how the timing moved.
* **Secondary criterion — a fixed bound at the amortised end.** If instructions
  per packet at **q=1** change by more than **2%**, treat the hoist as having
  altered the per-packet code path and **downgrade it from proof to
  corroboration**, leaving the amplification arm to carry the result.

Both arms report instruction counts, and both criteria are evaluated before the
timing result is interpreted.

This guard exists because of a repeated pattern in this investigation: a wrong
method that yields a right-looking number is the hardest error to catch, since
plausibility is what stops the checking. Three instances so far — the `C = 66`
ticks per burst that matched a hot-tcache `aligned_alloc`+`free` to the
nanosecond while the same model fitted a *larger* per-burst cost to the mode with
no allocation; the peer session's stale output file reporting a plausible 0.3%
change for a run that never happened; and its tick-to-cycle factor of 1.762
derived from `scaling_cur_freq`, which landed within 1.3% of the truth by an
invalid method.

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
   `aligned_alloc(4096, ...)` (`maglev.c:43-44`).

**Both backings are now measured, and the gap is smaller than first written.**
dramblast takes nine 1 GiB pages (`free_hugepages` 16 -> 7; maglev takes one,
16 -> 15, isolating DPDK's own). And maglev's table is **not** on 4 KiB pages as
originally assumed: `AnonHugePages` reads **8,515,584 kB during a maglev run
against a 36,864 kB baseline**, so the 8 GiB table is fully THP-backed at 2 MiB.

So the real asymmetry is **1 GiB vs 2 MiB** -- 8 TLB entries against 4096, a 512x
difference -- not the 1 GiB vs 4 KiB (262144x) that earlier notes implied. Still
a genuine uncontrolled variable between the two curves, but a materially smaller
one, and it depends on THP being `always`, which this repo's
`reserve_hugepages.sh` sets as a side effect (§3.2).
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
work being done. Quantifying it requires per-queue-count frequency sampling
during a sweep.

**Retraction: `scaling_cur_freq` cannot be used for this, on this box.** The
obvious plan was to sample it on every active core and take the median. It does
not work. Sampled while a sweep was busy-polling CPUs 0-14, an *idle* core read
**3.63-3.70 GHz** -- indistinguishable from the busy cores -- while `perf` counted
**506,165 cycles/s** on that same core, i.e. it was halted essentially the whole
interval. Under `intel_pstate` in active mode, `scaling_cur_freq` reports the
P-state *request*, not per-core delivery, and `cpuinfo_cur_freq` does not exist
in that mode, so there is no sysfs fallback. A median over cores would not have
rescued it: every core was reading the same wrong number, so the median is wrong
too, and it would have looked stable and plausible.

The working method, now in `sweep.sh:111`, counts cycles directly:

    sudo perf stat -e cycles -C <worker cpus> -x, -- sleep 8

Cycles / wall-time / core *is* the delivered frequency here with no task-clock
correction, because DPDK busy-polls: the workers sit at 100% with no populate
phase or idle time to contaminate the average. Worker cores only -- lcore 0 runs
the stats loop, not forwarding.

**Resolved by configuration, not by correction.** Section 3.4b pins every core to
2,100,000 kHz, which is simultaneously this SKU's `base_frequency` and exactly
the invariant TSC rate. At that setting **TSC ticks are core cycles**, the
1.762x factor is 1.000, and the entire "across queue counts is weaker than it
looks" caveat above disappears rather than being estimated away. Verified by
perf, not sysfs: 33,482,579,855 cycles over 8 s on 2 cores = **2.0927 GHz**
delivered; an independent peer session on the same box measured 2.0843 GHz and
the sweep instrumentation reports 2095 MHz. The cost is representativeness --
see the pre-registration below.

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

### Pre-registered: what the pinned re-baseline must show

Written **before** running the 2.1 GHz sweep, against the turbo-era
`linerate_2tx_instr` numbers, so the prediction cannot be fitted after the fact.

**First, a units correction that inverts the naive expectation.** A peer session
pinning the same box predicted that per-packet cost would *fall* at 2.1 GHz,
because memory latency is fixed in nanoseconds and therefore spans fewer cycles
at a lower clock. That is right in *core cycles*, which is what that session
measures. It is backwards in the units this investigation reports. `rte_rdtsc()`
counts the invariant TSC, which runs at 2.1 GHz in **both** conditions, so a tick
is a fixed quantity of *time* either way. Lowering the core clock cannot make any
work finish sooner in wall-clock terms. So here, ticks per packet must **rise or
stay flat, never fall.** If any point falls, the rig is wrong, not the chip.

That gives a free, sharp test of the central hypothesis. Let `r = 3.7/2.1 =
1.762` be the clock ratio (an upper bound; all-core turbo at high q is below
single-core turbo, so the true per-q ratio is somewhere in `[1.0, 1.762]`).

| the cost is... | scales by | because |
|---|---|---|
| CPU-bound work (instructions retired) | up to `r` | fewer instructions per unit time at a lower clock |
| memory / DMA latency | `1.0` | fixed in nanoseconds, unaffected by core clock |

The hypothesis under test (section 2, "A real queue-count-dependent cost") is
that dramblast's rise from 58 to 185 ticks is a **fixed per-burst cost amortised
over shrinking bursts**, and the candidate mechanism is the `aligned_alloc`/`free`
pair in `dramblast_process_frames` (`dramblast.c:202-203`, `:231`) -- which is
CPU work, not a memory stall. maglev's near-flat ~100 ticks, by contrast, is
presumed dominated by table-lookup memory latency.

So, concretely:

1. **Every point rises or holds.** No point falls below its turbo value. This is
   a rig check; failure invalidates everything downstream.
2. **dramblast's excess scales like CPU work.** Its q=10 *excess over its own
   q=1 plateau* is `185 - 58 = 127` ticks. If that excess is the allocator, it
   should scale by close to `r`, landing the q=10 excess near `127 x 1.7 ~ 216`
   ticks. If it is memory stalls in disguise, it stays near 127.
3. **maglev's plateau scales less than dramblast's excess.** maglev's flat
   ~100 ticks at q=1-5 should grow by a *smaller* factor than dramblast's excess
   does, since it is the more latency-exposed of the two.

**What would falsify the per-burst-CPU-cost story:** dramblast's excess growing
by less than maglev's plateau does. That would mean the queue-count-dependent
cost is latency-bound, the allocator is the wrong candidate, and the hoist
experiment should not be run as specified.

**AMENDMENT, before the turbo arm was run.** The criteria above use a single
`r = 1.762`. A peer session pointed out that this is wrong in a way that biases
toward *rejecting* the hypothesis, and it is right:

* Delivered single-core turbo on this box measures **3.65-3.68 GHz**, not the
  3.70 nominal, so `r <= 1.74` even at q=1.
* All-core turbo is below single-core turbo, and this sweep goes from 1 to 10
  busy cores. A delivered **3553 MHz** was observed here at high core counts,
  giving `r ~ 1.69` there, and the true figure at q=10 is lower still.

So `r` is a *function of q*, falling as q rises. Testing "does dramblast's excess
scale near `r`" against a fixed 1.762 would make genuinely CPU-bound work look
sub-`r` at exactly the high queue counts where the per-burst cost lives, and
would kill a correct hypothesis. **Criterion 2 is therefore evaluated against
`r(q)` computed per queue count from the turbo arm's own recorded `freq_mhz`, not
against a constant.** The absolute target "~216 ticks" is withdrawn; the test is
`excess_pinned / excess_turbo` compared to that queue count's measured `r(q)`.

**Second amendment: the comparator changes.** Comparing the pinned arm against
the *older* `linerate_2tx_instr` dataset is not a single-variable contrast. Four
other things changed on this host in between -- `irqbalance` stopped, C-states
disabled, THP `defrag` moved to `madvise`, and the cpuset partition introduced
(section 3.4b/3.4c). All of those should make results faster or steadier rather
than slower, so none of them explains a slowdown, but none of them is *measured*
either. Early evidence that this matters: pinned q=2 dramblast reads 105 ticks
against the old turbo arm's 58, a ratio of **1.81 -- above the `r <= 1.762`
ceiling**, which under a clean single-variable contrast should be impossible.

The turbo arm resolves this. It is run with the identical binary, cpuset,
`irqbalance`, C-state and THP configuration as the pinned arm, differing *only*
in turbo and the frequency governor. **The pinned-vs-turbo comparison is
therefore made against the new turbo arm, and `linerate_2tx_instr` is retained
only as the historical record.**

**Third amendment: q-for-q is the wrong axis to compare the arms on.** Found
while the pinned arm was running, from its own early points. At 2.1 GHz the
forwarder is ~1.74x slower, so it stays oversubscribed to a higher queue count:
pinned dramblast is still at a full `batch=64` and a flat ~103 ticks at q=4,
where the turbo arm had already begun keeping up and its bursts had started to
shrink (66 ticks, rising). The knee simply moves right.

That breaks the obvious comparison. The per-burst-cost model says ticks/packet
depends on **burst size**, not on queue count -- queue count only matters because
it sets the burst size. Comparing the two arms at equal `q` therefore compares
them at *different burst sizes*, which is the one variable the hypothesis is
about.

The fix, which is also a sharper test than the one pre-registered above: plot
**core cycles per packet against 1/burst-size**, for both arms and both modes,
converting each turbo point by its own measured `r(q)` (the pinned arm needs no
conversion, `r = 1.000`). Under the model `cycles/packet = P + C/B`, this is a
straight line with intercept `P` (per-packet work) and slope `C` (per-burst
work).

The test is then a *collapse*: if all the work is CPU-bound, both arms fall on
the **same** line, because core cycles are clock-invariant for CPU work. Any
vertical separation between the arms is precisely the memory-latency fraction,
which does not scale with the clock. This needs no assumption about `r` being
near any particular value, and it uses every point in both sweeps rather than
just the endpoints -- addressing the failure recorded above under "Attempting to
size the per-burst cost -- the model does not hold up", where a two-point fit and
a least-squares fit disagreed by 6x.

**Fourth amendment, and this one is a post-hoc correction -- flagged as such.**
Criterion 1 above ("every point rises or holds") is **mis-specified**, and it was
a violation in the data that made me notice, not foresight. At q=9 the pinned arm
reads 149 ticks against the older turbo arm's 164 -- a fall, which criterion 1
declares impossible.

The criterion is unsound as written. "A lower clock cannot make anything finish
sooner" is true only *at equal work*. Here the work per packet is itself a
function of the clock, through exactly the mechanism the third amendment
identified: at q=9 the pinned arm is running `batch=14` while the turbo arm ran
`batch=6`, and a larger burst amortises the per-burst cost over more packets. The
pinned arm is doing **less work per packet**, so fewer ticks per packet is not
only possible, it is what the per-burst-cost model predicts.

So criterion 1 is valid only where both arms sit at the same burst size -- in
practice the low-q cells where both are pinned at a full `batch=64`. It is
reported below in both forms: as originally written (which it fails), and
restricted to equal-burst cells (its sound form). The unrestricted form is
retained in the output rather than deleted, because a pre-registration that is
quietly narrowed after it fails is worth nothing.

The honest summary is that the third amendment already implied this and I did not
propagate it into criterion 1. That the two arms cannot be compared at equal `q`
turns out to matter more than it first appeared: it invalidates not just the
headline comparison but the rig check built on top of it.

**Control the collapse test requires: are the two arms instruction-matched?**
The collapse test reads any vertical gap between the arms as the
memory-latency fraction. That reading is only valid if both arms execute the
*same work*. A peer session checked this assumption on its own two arms -- having
just called the equivalent result its strongest -- and found a real, deterministic
asymmetry of **0.19 instructions per key** between machine states, reproducible
to four decimal places across four trials and surviving the differencing that
removes setup. Origin unestablished. At its IPC that is ~0.073 cycles/op against
a gap precision of ~0.08 cycles/op: the *same order as the effect being
measured*, which turned "twelve points on zero" into "zero to within the
precision at which the arms are matched".

The same hazard applies here and has not been checked. **Before any gap in the
collapse figure is interpreted as latency, `perf stat -e instructions,cycles`
must be run on a matched configuration in each arm.** If the arms differ in
retired instructions, part of the gap is work asymmetry, not latency, and the
figure's caption would be claiming more than the data supports. This is the same
failure mode already recorded twice in this document -- a plausible number
produced by an invalid method -- and it is cheap to rule out.

**What this costs, and why it is still worth doing.** Pinning makes the rig more
reproducible and *less representative at the same time*. Production runs with
turbo, so absolute numbers at 2.1 GHz describe a machine nobody deploys on --
throughput drops from ~93 to ~56 Mpps. The peer session found its own tuning
parameter looked 4x less valuable pinned than at turbo, and warned that anyone
tuning on a pinned rig would pick a value too shallow for production. That
applies here too. Mitigation: run **both arms instrumented** -- pinned for the
mechanism, turbo for the representative number -- and report the turbo arm as the
headline. The turbo arm also supplies the per-q delivered frequency that
`linerate_2tx_instr` never recorded, which is what converts the whole existing
tick dataset into core cycles.

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

### 3.4b Measurement-stability configuration (runtime, resets on reboot)

Applied after a contention artifact was traced to the scheduler co-locating
other processes onto the DPDK polling cores (§2, maglev q=7). Coordinated with
the other benchmarking session on this host before applying, since all six are
machine-wide.

| # | Change | Reason | Revert |
|---|---|---|---|
| 1 | `no_turbo=1` | removes the 800 MHz - 3.7 GHz clock swing | `echo 0 > .../intel_pstate/no_turbo` |
| 2 | `scaling_min=max=2100000` on all CPUs | pins at base frequency | restore `800000`/`3700000` |
| 3 | C-states disabled | removes wake-latency variance | `echo 0 > .../cpuidle/state*/disable` |
| 4 | `irqbalance` stopped | it migrates IRQs onto busy cores mid-run | `systemctl start irqbalance` |
| 5 | `nmi_watchdog=0` | removes a periodic per-core interrupt | `echo 1 > /proc/sys/kernel/nmi_watchdog` |
| 6 | THP `defrag` `always` -> `madvise` | stops synchronous compaction stalls | restore `always` |

1-3 via this repo's `scripts/constant_freq.sh 2.1GHz`.

**Why 2.1 GHz specifically.** It is this SKU's `base_frequency` *and* exactly the
invariant TSC rate. Pinning there makes TSC ticks equal core cycles, so l2fwd's
"Cycle per fwd packet" becomes literally true and the tick-to-cycle correction
(§2) disappears rather than having to be measured.

**Verified by measurement, not sysfs.** perf counted 33,482,579,855 cycles over
8 s on two busy cores = **2.0927 GHz delivered**, within 0.3% of nominal. THP was
re-checked after change 6 and still applies: `AnonHugePages` rises from 122,880
kB to 8,509,440 kB during a maglev run, so maglev's table keeps its 2 MiB
backing and the crossover comparison is unaffected.

**What this configuration costs.** Every absolute number measured under it
describes a machine nobody deploys on -- production runs with turbo. Ratios
survive; "cycles per packet at 2.1 GHz" is a measurement of a configuration
chosen for measurability. Absolute throughput drops ~1.67x.

**A caveat on comparing across the change.** Cycles per operation is
frequency-invariant only for compute-bound code. For memory-bound code
`cycles_per_op = compute_cycles + memory_stall_ns x frequency`, so the stall
component shrinks *in cycles* at a lower clock. Any comparison of cycle counts
taken before and after this change must account for that rather than assuming
cycles are a frequency-independent unit.

### 3.4c CPU isolation via cpuset (runtime, resets on reboot)

Frequency pinning removes clock variance, but it does nothing about the
scheduler co-locating other work on the DPDK polling cores -- which is what
actually produced the false maglev q=7 result (§2). Fixed with cgroup v2
cpusets rather than `isolcpus`, so no reboot is needed.

    bench.slice  (TOP-LEVEL, partition root)  ->  0-23
    system.slice / user.slice / init.scope    ->  24-27,52-55

Housekeeping is physical cores 24-27 *and* their hyperthread siblings 52-55, so
nothing in housekeeping shares a physical core with a benchmark core. The
siblings of the benchmark cores (28-51) are left unused by both sets.

Applied with `systemctl set-property --runtime`, so it does not survive a reboot.
Revert by setting `AllowedCPUs=` (empty) on the three slices.

**Two things that are easy to get wrong here.**

*`bench.slice` must be top-level, not under `system.slice`.* cgroup v2 cpusets
are hierarchical: a scope beneath a restricted `system.slice` can never exceed
its parent's effective cpuset, whatever `AllowedCPUs` is passed to it. It is
silently confined to the housekeeping set instead of failing.

*The slice restrictions alone are not sufficient.* They confine userspace, but
kernel threads live in the root cgroup and are not bound by them. Setting
`cpuset.cpus.partition = root` on `bench.slice` makes those CPUs exclusive and
removes them from the root cgroup's effective set:

    /sys/fs/cgroup/cpuset.cpus.effective:   0-55  ->  24-55

(The stronger `isolated` partition type, which also disables load balancing
inside the set, postdates this 5.15 kernel; `root` is the strongest available
here.)

**Consequence for how benchmarks are launched.** A plain `sudo ./build/l2fwd`
inherits the shell's cpuset and is confined to housekeeping -- it still runs,
silently, on the wrong cores. `sweep.sh` therefore launches into the partition:

    sudo systemd-run --scope --slice=bench.slice -p AllowedCPUs=0-23 -- ./build/l2fwd ...

Verified end to end: a sweep run through this path reports `freq=2095MHz` from
the perf-based instrumentation, `hp1g=16->7`, and leaves the root cgroup's
effective set at `24-55`.

**Diagnostic trap worth recording.** Checking isolation with
`pgrep -f 'l2fwd.*dramblast'` matches the **`sudo`/`systemd-run` wrapper**, whose
command line contains the same strings and which legitimately lives in the
shell's cgroup. That made a correctly-isolated process look unisolated and cost
several minutes of chasing a non-existent bug. Use `pgrep -x l2fwd`.

### 3.5 Repository additions

Originally new files only; §5's experiments also required three
run-time knobs in the l2fwd source, described at the end of this section.

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

Source changes were made to `main.c`, `dramblast.c`/`.h` and `maglev.c` to add
three run-time knobs — `-B` (page backing), `-A` (allocator pairs per burst) and
`-Q` (prefetch pipeline depth). All three default to as-shipped behaviour, so an
invocation without them is the unmodified experiment; each exists because a
hypothesis about the per-burst cost was otherwise going to be settled by argument
rather than by measurement. They are passed in **argv, never the environment**:
these runs launch through `sudo systemd-run`, which strips the environment, so an
env-var knob would silently fall back to its default and report a plausible wrong
number — a failure mode a peer session lost a dataset to on the same night.

`l2fwd/set_clock.sh` deliberately touches *only* `scaling_min/max_freq` and
`no_turbo`. C-states, `irqbalance`, `nmi_watchdog` and THP `defrag` stay as
section 3.4b left them in **both** arms, which is what makes pinned-vs-turbo a
single-variable contrast rather than a five-variable one. It writes max before
min when pinning and min before max when releasing, because the kernel rejects a
`scaling_min_freq` above the current `scaling_max_freq`: doing it in the wrong
order fails silently on the affected cores and leaves the machine half
configured, which reads exactly like success. It verifies by reading back every
core rather than `cpu0` for the same reason.

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

Updated 2026-09-14. Items resolved during the night's work are struck through
with what resolved them, rather than deleted, so the record shows which
questions turned out to matter.

1. ~~Generator: run as committed or scaled to 8 cores?~~ **RESOLVED** — scaled to
   `-l 0-16` (8 TX + 8 RX), which reproduces `docs/data.txt`'s 16M flows exactly
   and offers 100 GbE line rate. `pktgen/run.sh` still carries the 2-worker
   `-l 0-2`; updating it would make the committed generator match its own
   documentation, but that is a source change and has not been made.
2. ~~Highest value remaining: test the allocator attribution by hoisting the
   `aligned_alloc`/`free` out of `dramblast_process_frames`.~~ **SUPERSEDED, and
   the reasoning behind it was wrong.** The estimate that made the allocator
   look like the whole per-burst cost came from turbo-era data whose delivered
   clock was never recorded; refitted on the pinned arm the per-burst cost is
   ~645 cycles, against 20-40 ns for a hot-tcache round trip, so a plain hoist
   would move ~10% of it and could not distinguish "small" from "zero".
   **(That rationale was wrong twice over, per §5.13: the per-burst cost is ~718
   on the binary every later arm used, and the tcache reference does not apply
   to a 64-byte-aligned request at all. The hoist was run, and it worked.)** §5.9
   replaces it with an amplification sweep (`-A N`), which measures what a pair
   costs on this machine instead of assuming a literature value, paired with a
   pipeline-depth sweep (`-Q`) that the allocator cannot mimic.
3. May `main.c` be modified for the measurement fixes (`samples` as `double`,
   bound `SAMPLE_SIZE`)? Still worked around in post-processing. The harness
   itself still produces biased numbers for anyone reading its output directly,
   which is a trap for the next person rather than for this investigation.
4. ~~Add per-queue-count frequency sampling.~~ **RESOLVED** — `sweep.sh` measures
   the delivered clock per run with `perf`, never from sysfs, which under
   `intel_pstate` reports the P-state request rather than delivery (§5.3). The
   turbo arm's clock turned out to be a machine constant here — 2993 MHz across
   a tenfold change in busy cores — because idle states are disabled, so all 56
   cores always count as active and the ceiling is permanently the six-plus-core
   turbo entry.
5. ~~Optionally pin frequency for a clean absolute cycle count.~~ **RESOLVED** —
   done in §3.4b, and it is what made the per-burst model fit at all. The
   earlier conclusion that the model "does not hold up" was a statement about
   uncontrolled frequency, not about the model.
6. Reproduce against `7e11fc8` or against a corrected HEAD invocation? Still
   open, and still a question about which threading model is being measured
   rather than one this data can settle.
7. ~~Apply the one-line `run.sh` fix.~~ **RESOLVED** as commit `d68a884`.

### Still genuinely open

- **What the smallest distinguishable step of this rig is, everywhere else.**
  §5.13 records a result that stood for six hours, survived peer review, and was
  entirely an artefact of `Cycle per fwd packet` being printed as an integer —
  64 cycles per burst at a 64-packet burst. The same counter underlies every
  matched-burst number in this document. The ones that matter are all many ticks
  and are unaffected, but no systematic sweep of "which claims here are within a
  tick or two" has been done.
- ~~**What the ~645 cycles of per-burst work actually are.**~~ **RESOLVED by
  §5.13 and §5.14.** Both knobs were run. Of the 718 cycles the refactored
  binary shows, roughly **500** (462-527, §5.13) is the `aligned_alloc`/`free`
  pair and **~165** is the prefetch pipeline's per-fill ramp, which together
  account for most of it; what is left for the RX and TX burst calls themselves
  is small and not separately resolved. What is still open is narrower: why a
  once-per-burst allocation moves the *per-packet* coefficient at all (§5.13).
  Cache and TLB pollution from the allocator's own chunk walking is the obvious
  candidate and has not been measured.
- **Why the refactored binary is 6% faster per packet** (§5.10). Behaviour is
  identical and the difference has the same sign at every queue count, so it is
  codegen; which change, exactly, has not been chased. It matters only as a
  reminder that a binary is a variable.
- ~~**Whether the cross-core page-table contention seen on 4 KiB pages**~~
  **ANSWERED by §5.18, from counters already in the logs.** Walks per packet are
  flat at 0.99 across every queue count and instructions per packet are flat
  too; what rises is walk *duration*, 19-28% from one core to six or ten. The
  same binary on 1 GiB pages takes no walks and shows no core-count effect at
  all. What remains unmeasured is which shared structure is contended:
  last-level-cache pressure from the page table's own working set fits
  directly.
- **Whether any of this transfers off this machine.** Every number here is from
  one Xeon Gold 5512U with idle states disabled and the uncore locked at
  2.5 GHz. The *shape* of the result — a fixed per-burst cost against a fixed
  per-packet one, crossing at a particular burst size — should transfer; the
  crossing point is a property of this silicon.

---

## 5. The per-burst cost, measured rather than argued (2026-09-14)

Everything below was taken with the machine frequency-pinned and cpuset-isolated
per §3.4b/§3.4c, against the node1 generator that had been holding 93.28 Mpps
continuously since 2026-09-12.

### 5.1 The rig reproduces across days

The first measurement taken was a cold re-run of the pinned `dramblast` q=1
point, two days after the original. It returned **15.62 Mpps and 101 TSC ticks
per packet** against 15.63 and 101 before. Machine state was re-verified rather
than assumed: `no_turbo=1` and `scaling_min = scaling_max = 2100000` on all 56
cores, `bench.slice` still holding CPUs 0-23 as a `root` partition.

That matters because everything in this section is a comparison between
conditions measured hours apart. A rig that reproduces to one tick after two days
idle can carry that weight; one that does not cannot.

### 5.2 A confound found before it was measured: the two modes do not use the same page size

The two modes allocate the same 8 GiB of table on **different page sizes**, which
had gone unnoticed:

| mode | allocation | pages | TLB entries for 8 GiB |
|---|---|---|---|
| dramblast | `mmap(MAP_HUGETLB \| MAP_HUGE_1GB)` (`dramblast.c`) | 1 GiB | 8 |
| maglev | `aligned_alloc(4096, ...)` (`maglev.c:43`) | 2 MiB via THP | 4096 |

This is measured, not inferred. With a maglev run live, `/proc/meminfo` reported
`AnonHugePages: 8513536 kB` and the process's own `smaps_rollup` reported
`AnonHugePages: 8331264 kB` alongside `Private_Hugetlb: 2048000 kB` — the latter
being DPDK's own `-m 2000`, not the table. THP is `always` on this host, so
maglev's plain `aligned_alloc` is silently promoted to 2 MiB pages in full.

8 GiB on 2 MiB pages is 4096 distinct pages against a ~2048-entry L2 STLB, so
uniformly random lookups miss the STLB most of the time and take a page walk;
8 GiB on 1 GiB pages is 8 entries and never misses. **Any per-packet cost
difference between the two modes was therefore a difference in algorithm and in
address translation at once.** Section 5.6 reports what that is worth.

### 5.3 The two clock arms, and what the disabled C-states did to them

Both arms use the identical binary, cpuset, irqbalance, THP and C-state
configuration, and differ only in `no_turbo` and the governor limits.

The turbo arm's delivered clock was measured per run rather than assumed, and it
is a machine constant here:

    q=1..10, dramblast and maglev:  2992-2994 MHz, every run

Ten runs spanning one to ten busy worker cores, one MHz of spread. And a core
doing nothing at all measured 2.993 GHz too. The cause is that **idle states are
disabled machine-wide** (`POLL`, `C1`, `C1E`, `C6` all `disable=1` on all 56
cores), so every core spins unhalted and the package never goes quiet. The
all-core turbo ceiling is therefore pinned near 2.99 GHz regardless of load —
nothing like the 3.7 GHz nominal.

So the clock ratio is

    r = 2993 / 2094 = 1.429

not the 1.762 the original pre-registration assumed and not the 1.756 a 3.66 GHz
turbo would give. This is recorded as a **condition of the experiment**, not
fixed, because fixing it would invalidate the pinned arm as well. It is also the
reason AMENDMENT 1 was right to replace a constant `r` with a per-run measured
one.

### 5.4 The model, and the instrument

The claim under test is

    cycles per forwarded packet  =  P  +  C / B

with `P` an irreducible per-packet cost and `C` a fixed cost paid once per RX
burst and amortised over the `B` packets that burst returned. The queue-pair
sweep is an unusually clean instrument for it: offered load is held at line rate
while the queue count rises, so the same packet stream is divided over more
queues and `B` falls with nothing else about the workload changing. That sweeps
`1/B` over a sixteenfold range using only a command-line argument.

`B` is l2fwd's own `Average rx batch sz`, which is `rx / rx_cnt` with `rx_cnt`
incremented on every poll *including empty ones* (`main.c:301`, before the
`nb_rx > 0` test), so it is packets per poll attempt — the right denominator for
a per-poll cost.

`l2fwd/fit_burst_model.py` fits this by least squares and cross-checks the slope
against a two-point estimator using only the extreme bursts. The two share no
algebra, so agreement is evidence about the model rather than about convergence.

### 5.5 Result: dramblast has a large per-burst cost; maglev has none

| arm | mode | P (cycles/packet) | C (cycles/burst) | R² | two-point C |
|---|---|---|---|---|---|
| pinned 2.094 GHz | dramblast | 95.2 | **644.5 ± 41** | 0.968 | 638.5 (0.9% away) |
| pinned 2.094 GHz | maglev | 163.4 | −44.4 ± 35 — **not resolved** | 0.166 | — |
| turbo 2.993 GHz | dramblast | 105.0 | **684.4 ± 55** | 0.951 | 638.9 (6.7% away) |
| turbo 2.993 GHz | maglev | 200.5 | −26.9 ± 26 — **not resolved** | 0.117 | — |

dramblast's slope is resolved at about sixteen standard errors and two independent
estimators agree on it to 0.9% in the pinned arm. maglev's is indistinguishable
from zero in both arms, which is not a weak result but a strong one: its cost per
packet barely moves while its burst collapses from 64 packets to 10
(168 → 159 ticks, a **fall** of 5%), whereas dramblast's rises 101 → 171 over the
same range.

The crossover follows directly: dramblast is cheaper than maglev while
`644.5 / B < 163.4 − 95.2`, i.e. while **B > 9.4 packets** (9.5 on the refactored
coefficients §5.10 says to use). Above that burst size
dramblast wins by up to 40%; below it, it loses. That is the whole shape of the
queue-count dependence in one inequality.

### 5.6 What the two arms decompose it into — the headline

A cost measured in core cycles at two different clocks separates CPU work from
memory stall, because instructions retire in a fixed number of *cycles* while a
DRAM access takes a fixed number of *nanoseconds*. Writing `X(f) = W + T·f`:

| | CPU work | exposed stall | memory-bound share |
|---|---|---|---|
| dramblast, per packet | 72.4 cycles | 10.9 ns | **24%** |
| maglev, per packet | 77.0 cycles | 41.3 ns | **53%** |
| dramblast, per burst | 551.5 cycles | 44.4 ns | **14%** |

**The two modes do essentially the same CPU work per packet — 72.4 against 77.0
cycles, within 6%. The entire per-packet performance difference is exposed memory
latency.** maglev eats 41.3 ns per packet; dramblast's software prefetch pipeline
hides all but 10.9 ns of the same access, removing about three quarters of the
stall.

And the cost of that hiding is **not** waiting. `C` decomposes as 551 cycles of
executed work against 44 ns of stall — 86% CPU work. This kills the hypothesis
that the per-burst cost is *predominantly* unhidden DRAM latency at the start of
a short burst.

> **Read with §5.13 and §5.14, which settle what those 551 cycles are.** About
> 500 of them are `_int_memalign` and `free` — executed work, entirely
> consistent with the 86% here. But the pipeline ramp is **not** dead: §5.14
> measures it at ~165 cycles per fill and shows it is latency rather than work,
> since shortening the pipeline eightfold costs 4.1% more instructions against
> 18.5% more cycles. An aggregate "86% executed work" conceals a smaller
> latency-bound component; it does not exclude one.

This decomposition is done on the **fitted coefficients**, not point by point,
and that is deliberate. The two arms never sit at the same burst size where an
excess exists — a faster forwarder drains its queues sooner, so at q=5 the turbo
arm is at burst 30 while the pinned arm is still at 64. Differencing at fixed `q`
mixes the clock change with a burst-size change. `P` and `C` are free of burst
size by construction, which is what makes them comparable.

### 5.7 Two errors found in this session's own analysis code

Recorded because the same class of error was being hunted in a peer's work at the
same time, and it would be dishonest to report theirs and not these.

**(a) `check_arms.py` computed the CPU-bound fraction with the wrong formula.**
It used `((t_pinned/t_turbo) - 1) / (r - 1)` — a linear interpolation of the tick
ratio between 1 (all memory) and `r` (all CPU). Both endpoints are correct, but
the ratio is a ratio of two linear functions of the work fraction, not a linear
one, so every intermediate value was wrong. It read maglev's plateau as 35.2% CPU
work where the correct figure is 43.6%. The correct form, now used, is

    cpu fraction = (1 - t_turbo/t_pinned) / (1 - f_pinned/f_turbo)

The bug was found because `fit_burst_model.py`, working in core cycles, was
already correct and the two disagreed. Two independent routes to one number is
what caught it; a single route would have shipped it.

**(b) `extract_results.py` destroyed four historical conditions.** It rebuilt
`results_reproduced.json` from scratch on every invocation, so running it against
a directory containing only the new arm's logs silently deleted every condition
whose log directory no longer existed. They were recoverable from git; an
uncommitted arm would not have been, and one — the 2026-09-12 pinned arm — was
lost and is superseded rather than recovered. The extractor now merges, and only
rewrites a condition when logs for it are actually found.

### 5.8 Instructions per packet, and a scope caveat that must travel with them

| mode | burst 64 | burst 8-10 | implied per-burst |
|---|---|---|---|
| dramblast | 400.8 | 496.0 | ~870 instructions/burst |
| maglev | 285.1 | 315.3 | ~358 instructions/burst |

**These are not commensurable with the tick counts above and must not be put in
the same table without saying so.** `perf` counts the whole worker core,
including the DPDK RX/TX driver path; `Cycle per fwd packet` brackets only the
hash region (`main.c:305` to `main.c:355`, with `rte_eth_rx_burst` and
`rte_eth_tx_burst` outside it). Instructions per packet therefore rise at high
queue counts partly because polls per packet rise, which is driver work.

Differencing the two modes removes it — same DPDK, same queues, same driver — and
leaves roughly **512 extra instructions per burst** attributable to dramblast's
batched path. Against 551 cycles of per-burst CPU work that implies an IPC near
**1.0 for the per-burst path specifically** — not for the forwarder, which
§5.14 measures at 3.96 overall. The two are different quantities, and an IPC of
1.0 for several hundred instructions of chunk-splitting under an arena lock is
exactly what §5.13 later found that work to be.

**The equal-work control, stated with its limits.** The decomposition in §5.6
assumes both arms execute the same work, so that is checked at matched burst
size rather than assumed:

| mode | burst | pinned insn/pkt | turbo insn/pkt | difference |
|---|---|---|---|---|
| dramblast | 64 | 400.9 | 400.9 | **+0.00%** |
| maglev | 64 | 285.6 | 285.8 | +0.08% |
| dramblast | 30 | 416.6 | 419.0 | +0.57% |
| dramblast | 8 | 496.0 | 530.4 | **+6.95%** |

At burst 64 and 30 the arms are doing the same work to well under a percent, and
`P` is determined mostly by those cells, so the per-packet decomposition rests on
solid ground. **At burst 8 they do not agree**, and that cell matters for `C`
because it is the longest lever on the slope. The likely cause is that the two
arms reach burst 8 at different queue counts — q=10 pinned against q=8 turbo — so
they differ in worker-core count and in empty-poll rate, and the burst-size
*distribution* behind an equal mean need not match. The honest reading is that
`C` carries an additional systematic uncertainty of several percent beyond the
±41 cycles of fit error, on top of which the turbo arm's burst-4 point is
non-monotone (535.6 instructions per packet against 552.1 at burst 7). `C` is
resolved well enough to distinguish "large" from "zero", which is what §5.5
claims; it is not resolved well enough to support a precise value, and no
conclusion here depends on one.

(This control exists because a peer session found a real, reproducible 0.19
instructions/key asymmetry between its own two machine states. That number is
specific to that workload and is **not** imported here; what transferred was the
check, not the constant.)

### 5.9 What is now queued, and what each run can refute

The per-burst cost is 86% executed work, so the two surviving candidates are the
allocator and the batching machinery itself. `l2fwd/run_matrix.sh` runs both, and
they cannot mimic each other:

- **`-A n`** sets the number of `aligned_alloc`/`free` pairs per burst: `-1`
  hoists the buffer to a per-lcore allocation made once at init, `0` is as
  shipped, `n > 0` adds `n` extra pairs. `C` must be linear in `n` if the
  allocator is the cost, and the slope is what a pair costs on this machine —
  replacing the 20-40 ns literature figure the earlier write-up leaned on.
- **`-Q depth`** sets the prefetch pipeline depth (64 as shipped). A burst of `B`
  packets can only fill `min(B, depth)` slots, so if `C` is a pipeline ramp it
  must fall as depth falls while `P` rises. If `C` is the allocator, depth cannot
  touch it.
- **`-B backing`** puts each mode on the other's page size, plus 4 KiB which
  neither ships with. Under the TLB reading of §5.2 this should move `P` and
  leave `C` alone; anything else refutes it.

### 5.10 The control that had to be run, and the review that had to happen

Three knobs were added to the source to settle §5.9. That makes the binary a
variable, so before any of those knobs is used the flag-free build has to
reproduce the build it replaced. It does not, quite, and the way it fails is
worth recording.

| | P (cycles/packet) | C (cycles/burst) | at burst 64 | at burst 8 |
|---|---|---|---|---|
| dramblast, pre-refactor | 95.2 | 644.5 ± 41 | 105.3 | 175.8 |
| dramblast, refactored | 87.7 | 717.6 ± 42 | **98.9 (−6.1% fitted, −3.3% measured)** | 177.4 (+0.9%) |
| maglev, pre-refactor | 163.4 | not resolved | 162.7 | 157.8 |
| maglev, refactored | 163.0 | not resolved | 162.4 (−0.2%) | 158.5 (+0.4%) |

**maglev is unchanged**, which matters more than it looks: maglev's allocation
mechanism genuinely changed (`aligned_alloc(4096, 8 GiB)` became an explicit
`mmap` plus `MADV_HUGEPAGE`), and on this host `defrag=madvise`, so the new path
takes synchronous compaction where the old one did not. Measured, the new path
reaches 99.98% THP coverage against the old path's 99.3%. That is a real
difference in what the kernel did, and it is worth 0.2% of run time — so it can
be set aside, having been measured rather than argued away.

**dramblast is cheaper at a full burst and unchanged at a short one**, and the
size depends on how you ask. Evaluating the two *fits* at burst 64 gives
105.3 → 98.9, a 6.1% drop — but that differences two extrapolations. Measured
directly at a matched burst of 64 and a matched queue count, the five paired
differences are −3, −4, −4, −3, −3 ticks: **−3.4 ticks, −3.3%** (§5.19 audits
every claim this way). Half the size, same sign, same systematic character, and
the direct measurement is the one to believe. The per-burst coefficient moved by
1.2σ of the combined fit error — that σ is within-sweep, and §5.15 puts the
run-to-run drift on this coefficient at ~29 cycles, so "not measurably" is the
most that can be claimed and "not at all" would be too strong. Behaviour is identical — the
push-loop bound is 63 either way — so this is codegen, most plausibly register
allocation around a bound that changed from a compile-time constant to a loaded
field. It is small, but it is systematic and it has the same sign at every
queue count, so **every later condition is read against the refactored control
and never against the pre-refactor arm.** Without this run the crossover would
have been compared to the wrong baseline and a 6% instrument artefact would have
been reported as a page-size effect.

#### What an adversarial review of the new code found

The changes were reviewed specifically for the failure mode that matters here —
not crashes, which are visible, but changes that would silently produce a wrong
number. Two findings were serious enough to change what was done.

**The harness could not verify the thing the experiment is about.** `sweep.sh`
samples `hugepages-1048576kB/free_hugepages` per run, which distinguishes 1 GiB
from not-1 GiB and nothing else. It cannot tell a 2 MiB arm from a 4 KiB one.
And both of those are *advisory*: `MADV_HUGEPAGE` may be declined under
fragmentation and `MADV_NOHUGEPAGE` can fail, in either case leaving a complete,
plausible, wrongly-labelled dataset and no error anywhere. The crossover block
could have run entirely on 4 KiB pages while reporting itself as 2 MiB.

Rather than edit the harness mid-experiment, the page backing every run actually
got is now sampled from outside, from the process's own `smaps_rollup`
(`AnonHugePages` and `Private_Hugetlb`), so the label is checked against the
kernel for every run rather than trusted. The 99.3%/99.98% figures above came
from that sampler on its first run, which is the sort of thing only visible once
the real quantity is being measured.

> **Not true as written — see §5.16.** The sampler filtered on `Rss > 1 GiB`, and
> hugetlb pages never appear in `Rss`. At the time this was written every
> 1 GiB-backed run was therefore skipped: the as-shipped dramblast control, the
> whole allocator block and the whole depth block. The THP arms, which is what
> this paragraph was written about, were covered throughout.

**A guard against the compiler had itself been compiled away.** The
amplification arm adds N `aligned_alloc`/`free` pairs per burst; a store into
the scratch buffer was supposed to stop the compiler discarding them. GCC 11
deletes that store — it is a dead store to an object about to be freed — and
disassembly of the shipped binary shows it gone while the pairs survive only
because the compiler happened not to apply `-fallocation-dce`. The arm as built
is valid, and was measured with that binary. But the failure mode if a later
toolchain does apply it is not a crash or noise: it is the allocator round trip
reporting **zero cycles**, which is a clean and publishable number and is exactly
the answer the experiment exists to rule out.

So `l2fwd/check_codegen.sh` now asserts, after every build and before any
measurement, that the alloc/free calls are still reachable and that software
prefetch instructions are still present at all. The second is the more important
assertion: the whole dramblast-versus-maglev result is a claim about
prefetching, and a build with the prefetches optimised out would measure a
different algorithm and still produce a well-behaved dataset.

Smaller findings, fixed after the measurements so that the data continues to
correspond to a single committed tree: `-Q 1` passed validation and then read an
uninitialised queue slot and used the unmasked result as a table index; `-A` with
any negative value silently selected the hoisted arm while printing the value
back; the hoisted buffer's size was a bare literal decoupled from
`MAX_PKT_BURST`, so raising that constant would have overflowed a heap buffer in
that one arm and read as a hoisting result.


### 5.11 An independent check on the memory share, and what its disagreement means

The decomposition in §5.6 infers the memory-bound share from how a cost responds
to the core clock. That inference can be checked directly, because the PMU
counts the cycles in which the core is stalled with an L3 miss outstanding
(`CYCLE_ACTIVITY.STALLS_L3_MISS`) and the cycles in which a page-table walk is
in flight (`DTLB_LOAD_MISSES.WALK_ACTIVE`). Both are counted per run alongside
cycles and instructions.

At a full 64-packet burst:

| mode | ticks/pkt | IPC | memory share, two clock arms | memory share, L3-stall counter | page-walk cycles/pkt | LLC load misses/pkt |
|---|---|---|---|---|---|---|
| dramblast (1 GiB pages) | 101 | 3.03 | 17.5% | **0.9%** | 0.0 | 0.007 |
| maglev (2 MiB THP) | 168 | 1.45 | 56.4% | **51.9%** | 25.6 | 0.879 |

**For maglev the two methods agree** — 56.4% against 51.9%, from a frequency
sweep and a hardware counter that share no assumptions. That is the strongest
corroboration in this document: the clock-arm method's whole premise is that a
cost which does not scale with core frequency is memory, and here an independent
counter says the same thing to within five points.

**For dramblast they disagree by a factor of twenty, and the disagreement is the
result.** `STALLS_L3_MISS` counts cycles in which *no* micro-operation executes
while an L3 miss is outstanding. dramblast is essentially never in that state —
0.9 cycles per packet out of 101, at an IPC of 3.03. The core always has other
work, because the software prefetch pipeline has run ahead and queued it. Yet
the clock-arm method still finds 17.5% of the cost failing to scale with core
frequency.

Both numbers are right, and they measure different things. The stall counter
measures *exposed latency*: cycles thrown away waiting. The clock-arm method
measures everything whose duration is fixed in nanoseconds rather than cycles,
which includes exposed latency **and** any throughput limit in the memory
hierarchy — a mesh running at its own fixed 2.5 GHz, fill-buffer occupancy,
DRAM bandwidth. dramblast has almost no exposed latency and a real throughput
cost; maglev has overwhelmingly exposed latency.

That is the mechanism stated precisely: **the prefetch pipeline does not remove
dramblast's memory traffic, it converts that traffic from latency into
throughput.** The two measurements agreeing for maglev and diverging for
dramblast is not a discrepancy to be reconciled — it is how the two regimes are
told apart.

One counter needs a caveat before anyone reads it as a miss rate. dramblast
records 0.007 LLC load misses per packet while reading a random 64-byte line out
of an 8 GiB table on every packet, which is impossible taken at face value.
`LLC-load-misses` counts *demand* loads; dramblast's table lines are brought in
by `_mm_prefetch`, so by the time the demand load issues the line is already
resident and no miss is recorded. The DRAM traffic is real and this counter
cannot see it. maglev, which has no prefetching, shows 0.879 per packet — close
to one line per lookup, which is what the algorithm says it should be.


### 5.12 The crossover: how much of the gap was ever about page size?

The confound in §5.2 is now measured rather than reasoned about. Each engine was
run on 1 GiB pages, on 2 MiB transparent hugepages, and on 4 KiB pages — the
last a configuration neither ships with, included so that a two-point swap
becomes a three-point trend.

Every arm's backing was checked against the kernel rather than trusted from the
flag, because `MADV_HUGEPAGE` and `MADV_NOHUGEPAGE` are both advisory and a
declined one leaves no error: the 4 KiB arms recorded 0.00 GiB of
`AnonHugePages` and the 2 MiB arms 8.00 GiB, in both modes, sampled from each
process's own `smaps_rollup` (`l2fwd/verify_backing.py`). The 1 GiB arms are
covered by the hugepage pool count instead, which drops 16 → 7 free pages for
the 8 GiB table.

> **One exception, found later (§5.16):** maglev on 1 GiB — the arm this
> comparison rests on — was not sampled by the `smaps_rollup` watcher during its
> measurement runs, because that watcher's filter excluded every hugetlb-backed
> process. It was re-verified afterwards by re-launching the same binary with
> the same flag (9.95 GiB `Private_Hugetlb`, zero THP), which is strong evidence
> that the flag does what it says and is not evidence about those specific runs.
> The pool count of 16 → 7 free pages did cover them at the time.

Cost at q=1 — one worker, a full 64-packet burst — in core cycles per packet.
(Shipped dramblast appears at several values across this document — 101, 98.9,
100.4, 99.5 — and they are not inconsistent: 101 is the **pre-refactor** binary,
everything from §5.10 onward is the **refactored** one, which is 3.3% cheaper at
a full burst, and the remaining spread is the ±1-tick print resolution plus the
small reproducible variation with queue count. Any comparison in this document
is made within one binary.)

| | 1 GiB | 2 MiB THP | 4 KiB |
|---|---|---|---|
| dramblast | **97.8** (shipped) | 101.8 (+4.0) | 116.7 (+19.0) |
| maglev | 153.6 (−14.0) | **167.6** (shipped) | 209.5 (+41.9) |
| gap | 55.9 | 65.8 | 92.8 |

q=1 is used rather than the fitted intercept for a reason given below.

**The page-size confound is real and it is a fifth of the story.** The
as-shipped gap between the two engines is 69.8 cycles per packet. Put both on
1 GiB pages and 55.9 cycles remain — **80% of the difference survives**.
Address translation was a genuine confound, it was worth finding, and every
mode-versus-mode comparison earlier in this document carried it. It is not the
explanation.

**The more interesting number is how differently the two engines respond.**
Dropping from 1 GiB to 4 KiB costs dramblast 19.0 cycles and maglev 55.9 — very
nearly three times as much. So the software prefetch pipeline is not only hiding
data latency, it is hiding *page-walk* latency: the walk is triggered by the
prefetch, early, and completes underneath subsequent work. That is why the gap
between the engines widens as pages shrink, from 55.9 cycles at 1 GiB to 92.8 at
4 KiB. A configuration change that hurts both engines hurts the unprefetched one
three times harder.

**Walk cycles are not walk cost.** The counters make the overlap explicit.
maglev on 2 MiB pages spends 25.6 cycles per packet with a page walk in flight,
yet removing the walks entirely by moving it to 1 GiB pages saves only 14.0 — so
even the engine with no software prefetching at all overlaps about 45% of its
page-walk time under other work. dramblast on 2 MiB pages spends **27% of all
its core cycles** with a walk outstanding and pays 4.0 cycles per packet for it.
`DTLB_LOAD_MISSES.WALK_ACTIVE` is an occupancy measure, not a cost, and quoting
it as one would have overstated the page-size effect by a factor of six.

#### Where the per-burst model stops applying

On 4 KiB pages the per-packet cost rises with **queue count** at a constant
64-packet burst: 117, 118, 118, 120, 126, 129 ticks across q=1..6, with the
burst pinned at 64 throughout. No per-burst term can express that — the model
says cost depends on burst size, and burst size is not changing.

The cause has to be a shared resource, and the obvious candidate is the page
table itself. Ten cores each walking a two-million-entry table put the page
table's own working set into contention for the last-level cache, and page
walks are memory accesses like any other. The effect tracks page size exactly as
that story predicts: +12 ticks over six cores on 4 KiB, +4 on 1 GiB, +2 on
2 MiB.

So the two-parameter model quietly stops applying on that arm. Its R² falls
0.974 → 0.933 → 0.867 across 1 GiB, 2 MiB and 4 KiB, and the fitted intercept
stops meaning "per-packet cost" because a third, core-count-dependent term is
being absorbed into two parameters that cannot represent it. **The fit still
returns confident-looking values.** That is why the comparison above is made at
q=1, which carries neither the per-burst term nor the contention term, rather
than at the intercept.

One more arm is worth reporting for what it cannot show: maglev on 4 KiB pages
never reaches line rate at any queue count (84.3 Mpps at q=10), so it stays
oversubscribed throughout and its RX burst never leaves 64. Every point sits at
the same burst size, so no slope is measurable at all. That is a result about
the configuration, not missing data, and the analysis now says so instead of
printing an unfittable line.


### 5.13 What the per-burst cost is made of — and a reversal

Two candidates survived §5.6: the `aligned_alloc`/`free` round trip
`dramblast_process_frames` performs once per burst, and the batching machinery
itself. They are separated by two knobs that cannot produce each other's
signature — `-A N` multiplies the allocator round trips, `-Q depth` changes the
prefetch pipeline's depth — and both predictions were registered in
`l2fwd/analyse_matrix.py`'s docstring and committed before the runs.

#### The reference was wrong, not the measurement

Before the numbers, the correction they force. §5.6's earlier conclusion
downgraded the allocator by comparing the measured per-burst cost against a
published figure of 20-40 ns for a hot allocator round trip. That figure
describes `malloc`'s tcache fast path. **This code never reaches it.**

Read from the linked library rather than from memory: the binary links glibc
2.33 out of the nix store, where `aligned_alloc` resolves to `__libc_memalign`,
which is nine bytes — a bare `jmp` into `_mid_memalign`. And `_mid_memalign`
relays to plain `malloc` only when the requested alignment is at most
`MALLOC_ALIGNMENT`, which is 16 on x86-64. This call asks for **64**. So it goes
to `_int_memalign` instead: 453 bytes of code that allocates oversized, computes
the aligned address, splits the chunk, and frees the leader — with the arena
lock held throughout.

So the earlier reasoning was not a bad measurement. It was a **correct
measurement compared against the wrong reference**, which no amount of
measurement hygiene catches, because nothing in the pipeline is wrong. The check
that catches it is to read the implementation actually being called.

Worse, the evidence was already pointing the right way. §5.6 established that
the per-burst cost is 86% executed instructions rather than waiting. Several
hundred instructions of chunk-splitting under an arena lock is exactly what that
looks like. It was read as evidence *against* the allocator because a constant
taken from a paper had quietly replaced a measurement, and had been sitting in
the reasoning for several days.

The 64-byte alignment also appears to be unnecessary: `dramblast_result_t` is two
`uint64_t`s, and the array is written sequentially, so cache-line alignment buys
nothing that 16-byte alignment does not.


#### What the allocator actually costs here

Five arms, each a full ten-run queue sweep, differing only in how many
`aligned_alloc`/`free` round trips the burst path performs.

The comparison is made at a **matched burst of 64 and a matched queue count**.
Both halves matter. Adding allocator pairs makes the forwarder slower, so it
stays oversubscribed further up the sweep and reaches different burst sizes —
fitting each arm over its own burst range would compare conditions differing in
two things at once. And at a matched burst the cost still varies slightly with
queue count, reproducibly, so the difference is taken at each queue count and
then averaged rather than read off a single one.

| pairs per burst | matched q | tick differences vs hoisted | cycles per burst |
|---|---|---|---|
| 1 (as shipped) | 1, 2, 3, 4 | 7, 9, 7, 10 | **527 ± 48** |
| 3 | 1, 2, 3, 4 | 24, 25, 24, 27 | 1596 ± 45 |
| 5 | 1, 2, 3, 4 | 40, 40, 40, 42 | 2586 ± 32 |
| 9 | 1, 2, 3, 4 | 72, 73, 71, 75 | 4645 ± 55 |

**The one round trip the shipped code performs costs about 500 cycles.** Two
estimators that share no algebra: 527 ± 48 at matched burst and queue count, and
462 ± 44 by differencing the two fitted per-burst coefficients. They agree to
1.0σ. Quote it as **462-527 cycles, roughly 240 ns** — three significant figures
are not available from this instrument, for a reason that turns out to matter a
great deal (below).

So the allocator is **64-73% of the per-burst cost**, against the "at most ~11%"
this document previously claimed. The remainder — the batching machinery itself
— is measured directly by the hoisted arm at **256 ± 13 cycles per burst**,
rather than extrapolated from a fitted intercept.

The amplification arms give an incremental pair at **509 cycles**, with
consecutive slopes of 495 (3→5) and 515 (5→9). Extrapolating that line down to
one pair predicts 562 against the 527 measured — 0.7σ apart, **not resolved**.
Within this experiment the shipped pair and an incremental pair cost the same.

#### The version of this section that stood for six hours, and why it was wrong

The three paragraphs above replace a considerably more confident set of claims,
and the way those failed is the most transferable thing in this document.

The earlier reading took the comparison at **q = 1 alone**. It reported the
shipped pair at **447** cycles, an incremental pair at **510.78**, the two
consecutive slopes agreeing to **0.00 cycles**, and the line's intercept at
**0.0** — which was written up as a *structural* check (*k* pairs cost exactly
*k* times one pair) and then, when the shipped pair came out 64 cycles below the
line, as an allocator **load effect**: a lone pair being cheaper than one of
several in flight. A reviewer on the peer session pushed hard on the framing and
improved it, and the improved version was still built on sand.

None of it survives. `l2fwd` prints "Cycle per fwd packet" as an **integer**, so
at a 64-packet burst **one printed tick is 64 cycles per burst**. Two
consequences:

- The tick differences at q=1 are 24, 40 and 72. `40 − 24 = 16` and
  `72 − 40 = 32`, which is exactly twice it, and the pair counts are 3, 5, 9 —
  gaps of 2 and 4. So after *any* common scaling the two "independent"
  consecutive slopes are identically equal and the intercept is identically
  zero. **The agreement was arithmetic, not measurement.** Using each run's own
  measured frequency instead of a single rounded one already splits them to
  511.18 and 510.92.
- 447 against 511 is **one tick**. And 7 is the smallest of the four matched
  tick differences (7, 9, 7, 10); q=1 is the queue count where the gap happens
  to be least. The "load effect" was a single integer, chosen — unknowingly —
  from the low end.

The error class is new to this document and worth naming: **precision claimed
beyond the instrument's resolution, where the excess precision then generated a
mechanism.** Everything downstream was internally consistent, the peer review
made it more rigorous rather than less, and it was all describing rounding. The
defence is not more careful reasoning about the numbers; it is asking what the
smallest distinguishable step of the instrument is *before* interpreting a
difference, and this instrument's step is 64 cycles per burst.

It was caught by asking a second agent to re-derive the headline numbers from
the raw logs without using any of the analysis code, which reproduced 447 and
510.78 exactly, and then said that it could only reproduce them from a single
run each.

#### Three things that had to be got right, and one that was got wrong

**The line is tested, not asserted.** The earlier draft quoted R² = 0.988 for the
regression of per-burst cost against pair count. With three points, two
parameters and an x-range doing all the work, R² is near-uninformative — it is
close to 1 whatever happens. The regression is now weighted by each arm's own
standard error and tested with χ² against those same errors, and R² is
deliberately not printed for it.

**One arm is excluded, and the exclusion is stated.** The 9-pair arm is slow
enough that only two of its ten runs left a 64-packet burst, so it has no
measurable slope of its own (R² 0.62, standard error a quarter of the value). It
is dropped from the line with the reason given. This is the third condition in
this investigation where a fit returned confident parameters in a regime where
the model had stopped applying — the others being the 4 KiB crossover arm
(§5.12) and maglev's per-burst slope, which is indistinguishable from zero.

**The two estimators disagree and the disagreement is reported.** An incremental
pair costs about 509 cycles at matched burst and 365 from the weighted fit. The
reason
is visible in `P`, which is not constant across the arms (89.3, 87.7, 96.9,
99.5): the matched-burst route multiplies the whole per-packet difference by 64
and so charges that drift to the per-burst term, while the fit separates them
but pays for it with a model. An incremental pair costs **between 365 and 511
cycles**; that is as far as this data goes. Why a once-per-burst allocation
moves the per-packet cost at all is open — cache and TLB pollution from the
allocator's chunk walking is the obvious candidate and has not been measured.

Note that `P` *is* flat across the two arms that carry the headline — 89.3
hoisted against 87.7 as shipped — so the structural check holds exactly where
the claim lives and weakens only where the calibration lives.

#### The three explanations that were offered for a rounding error

Kept in full, because the sequence is the lesson. Extrapolating the multi-pair
line down to one pair predicted 510.8 against a measured 446.9, a gap of 63.8
cycles, and three separate accounts of that gap were written:

1. *The shipped pair must cost more,* because its `free` sits a whole batch away
   from its `alloc` while the amplification pairs run back to back. This had the
   sign backwards and was retracted the same day.
2. *The first pair is intrinsically cheap and later ones cost 511.* Ruled out by
   the data: if pair #1 were 447 and every later pair 511, the model would miss
   the 3-, 5- and 9-pair arms by +64 each, a constant, so the discount does not
   persist into them.
3. *Pair cost depends on how many are in flight, not on which one it is* — an
   allocator load effect, more live chunks and a larger free-list working set.
   This was the version that survived peer review and stood for six hours.

All three were explaining **one printed tick**. The gap of 63.8 cycles is 64
cycles, which is exactly one integer step of the cycle counter at a 64-packet
burst, and it came from the single queue count where the difference is smallest.
Note that account 3 *correctly refuted* account 2 using the constant +64 miss —
and that constant was the tick itself, visible in plain sight as the same number
three times, read as a physical constant rather than as the quantisation it was.

Measured at every matched queue count instead of one, the shipped pair is
527 ± 48 and the extrapolated incremental pair 562: **0.7σ apart, not
resolved.** There is no first-pair effect, no load effect, and nothing to
explain.

### 5.14 The prefetch pipeline depth: the model was the wrong shape, and saying so fixes two things

The per-burst cost was decomposed into an allocator part (462-527 cycles, §5.13)
and a remainder of about 200. The remainder was supposed to be the prefetch
pipeline's fill-and-drain ramp. The test was to shorten the pipeline, which
`-Q` now does: `dramblast_queue_depth` sets `find_queue_size`, the push loop
bounds against it (`dramblast.c:142`), and the four arms run at depth 8, 16, 32
and the shipped 64.

#### The result that needs no model

Five queue counts in every arm stayed at a 64-packet burst, so the arms can be
compared at a matched burst with nothing fitted:

| queue depth | cycles/packet | instructions/packet | IPC |
|---|---|---|---|
| 8 | 119.0 ± 0.32 | 414.0 | 3.48 |
| 16 | 106.8 ± 0.37 | 411.9 | 3.86 |
| 32 | 102.2 ± 0.58 | 405.0 | 3.96 |
| 64 (as shipped) | 100.4 ± 0.75 | 397.8 | 3.96 |

From depth 64 to depth 8, **instructions per packet rise 4.1% while cycles per
packet rise 18.5%**, and IPC falls from 3.96 to 3.48. A shallower prefetch
pipeline does not make the forwarder do meaningfully more work; it makes it
wait. That is the load-bearing depth result and it uses no fit at all. The
burst-cost model cannot produce it, because a model in cycles alone cannot tell
work from waiting — only a second counter can, which is why the instruction
counter has been read alongside the cycle counter since §5.8.

It also puts a number on the design decision. Going from the shipped depth 64 to
depth 32 costs 1.8 cycles per packet; going to depth 8 costs 18.6. The returns
are nearly exhausted by 32 and the last doubling to 64 buys 1.8 cycles.

#### Why the fitted C did something impossible

Fitting `P + C/B` to each arm separately gives:

| depth | P | C | R² |
|---|---|---|---|
| 8 | 115.8 | 375.4 ± 49 | 0.882 |
| 16 | 97.5 | 646.5 ± 18 | 0.994 |
| 32 | 91.2 | 733.7 ± 18 | 0.995 |
| 64 | 87.7 | 717.6 ± 42 | 0.974 |

Depth 8's `C` is **375, below the ~500-cycle allocator pair** — and that pair runs
once per burst at every depth, because the buffer is sized by the burst length
and not by the queue depth (`dramblast.c:243`). A per-burst term cannot be
smaller than a cost the burst pays unconditionally. At 1.5σ this is not a
falsification, but it is the model announcing that something is wrong with it.

What is wrong is the shape. The pipeline fills and drains `ceil(B/Q)` times per
burst. At the shipped depth `Q = 64` and the burst never exceeds 64, so there is
always exactly one fill and *per burst* and *per fill* name the same event — the
model cannot tell them apart, and quietly puts the ramp in `C`. Shorten the
queue and the two come apart: at depth 8 a 64-packet burst fills eight times, so
the ramp is paid per eight packets rather than per burst and moves into `P`.
Nothing trades places; the same cycles change which event they belong to.
`ceil(B/Q)` is a step function, so a straight line in `1/B` is mis-specified
wherever `B > Q` — which is most of the depth-8 arm and none of the shipped one.

This also retires a worry from §5.13. The concern there was that `P` moved
across the allocator arms for no stated reason. Some of that is the same effect:
`P` is not a clean per-packet quantity once anything per-fill is in play.

#### Calibrating the ramp, and a prediction that could have failed

Before the depth-32 arm finished, the per-fill ramp was calibrated on depths 8
and 16 at the matched burst of 64 — two points, one parameter — giving **165
cycles per pipeline fill**, and the prediction for depth 32 was written to
`docs/depth_prediction.md` with its falsification bounds. (That file also records
that one of depth-32's five burst-64 points was already on screen when the
arithmetic was done; the other four were not.)

The result, stated as an excess over the depth-64 arm, which is what the ramp
actually predicts and what the errors have to be propagated against:

    predicted by the ramp   2.58 ± 0.14
    measured                1.80 ± 1.14
    null (no effect)        0.00

0.7σ from the prediction, 1.6σ from the null: consistent with the model and not
excluding the null. **Inconclusive.**

The first version of this paragraph said 3.1σ from the null and declared the
model confirmed. That used only the within-sweep scatter — the spread of ten
samples inside one twelve-second window — which cannot see anything that drifts
between sweeps taken hours apart. The repeat arm (§5.15) measured that drift
afterwards at 0.45 cycles/packet for dramblast at burst 64, and it is the larger
term in this comparison. Including it doubled the error bar and removed the
verdict.

The shallower arms were never close to the floor: depth 8's excess is 18.6 ± 1.0
(18σ) and depth 16's is 6.4 ± 1.1 (6σ). Only the depth-32 point, which is the
one that tests the model, sat near the noise — so the remedy was repeats, not a
better fit.

#### Deciding it: three interleaved repeats

Both arms were re-run three times, alternating rather than in two blocks so that
any drift over the half hour would land on both equally instead of entirely on
the second; `q=1..5` only, since the comparison is made at a 64-packet burst and
those are the counts that stay there. The statistic is the **paired** difference
within each repeat, so drift common to a pair cancels, and the spread of the
three pairs is a measured error bar rather than an assumed one.

The pairing is **queue count by queue count**, not arm mean against arm mean.
The repeats do not all reach burst 64 at the same queue counts — one depth-64
sweep left it at `q=5` while its depth-32 partner did not — so comparing arm
means would silently compare different queue sets, and the queue count does move
the cost a little.

| repeat | matched q | tick differences | mean |
|---|---|---|---|
| 1 | 1, 2, 3, 4, 5 | 3, 2, 1, 1, 4 | 2.200 |
| 2 | 1, 2, 3, 4 | 3, 2, 2, 2 | 2.250 |
| 3 | 1, 2, 3, 4 | 3, 1, 2, 2 | 2.000 |

The excess is **+2.14 cycles per packet**, and it gets two error bars because
they answer different questions and here they disagree about one of the two
hypotheses:

    between-repeat (3 repeat means, 2 dof)      95% [+1.82, +2.47]
    every matched-q difference (13, 12 dof)     95% [+1.61, +2.69]

    ramp model predicts  +2.58        null predicts  0.00

**The null is excluded by both.** Depth 32 really does cost more than depth 64
at a matched burst; that part is settled, and it is the part the pipeline story
needs.

The model's *point prediction* is inside the conservative interval and just
outside the tighter one — 2.58 against an upper bound of 2.47 — and the measured
excess is 17% below it. So the ramp model has the right sign and roughly the
right size, and calling this a clean confirmation would be overreading it. The
first version of this paragraph did exactly that, on arm means that compared
five queue counts against four.

The verdicts were written into `analyse_matrix.py` before the data existed,
including the ones that would have gone against the model, and including the
instruction — for the case where the intervals excluded both hypotheses — not to
pick the nearer one.

#### One model across all four arms, and where it stops being a measurement

Fitting `W + ramp·ceil(B/Q)/B + K/B` to all forty points at once:

    W     =  91.4 ± 2.0    steady per-packet work
    ramp  =   166 ± 25     one pipeline fill
    K     =   477 ± 30     per burst
    residual rms 6.25 cycles over a 98-172 range

`K` is the striking one. Nothing in this fit knows about the allocator — this
experiment varied the queue depth and never varied the number of `alloc`/`free`
pairs — and yet `K` lands within 1σ of the ~500 cycles the amplification sweep
measured for that pair directly.

**That agreement does not survive a sensitivity check, and it is reported
anyway.** Refitting without the depth-8 arm, which is the arm the step model
describes worst (residual rms 9.1 against 3.3-5.0 for the others), moves `K` to
589 ± 30 — a 2.6σ shift, and well away from the allocator's ~500. A parameter that
moves that far when the noisiest arm is dropped is not a measurement. So the
convergence is suggestive and no more; what this fit actually determines is the
ramp, which barely moves (166 ± 25 with all four arms, 142 ± 31 without depth 8)
and which agrees with the 165 obtained by the completely separate matched-burst
route.

#### What the depth sweep settles

- The prefetch pipeline's benefit is **latency hiding, not work reduction**:
  4.1% more instructions, 18.5% more cycles when it is shortened eightfold.
- The ramp costs about **165 cycles per fill**, by two independent routes.
- The remaining ~271 cycles of the shipped per-burst cost is **not** all ramp,
  because at the shipped depth the ramp is paid exactly once per burst and is
  therefore already inside that number — the ramp and the allocator together
  account for roughly 500 + 165 = 665 of the 718 measured, leaving of order 50
  cycles of genuinely per-burst work (the RX and TX burst calls themselves)
  unattributed — which is inside this instrument's resolution and should not be
  treated as a measured quantity.
- The linear burst model is valid only while `B ≤ Q`. For the shipped build that
  is every burst, so §5.5 through §5.13 are unaffected. For any future arm that
  shortens the queue, it is not, and the step model must be used instead.

### 5.15 The repeat arm: the error bar everything else should have been measured against

The shipped condition was re-run at the end of the matrix — same binary, same
cpuset, same invocation, hours later with every other arm in between. Until this
ran, nothing in this investigation had a measured run-to-run spread. Every error
bar came from *within* one sweep: the scatter of ten one-second samples inside a
twelve-second window, which by construction cannot see anything that changes
between sweeps.

| | matched-burst points | mean | rms | worst |
|---|---|---|---|---|
| dramblast | 7 | −1.71 | 2.98 | −6 |
| maglev | 6 | +0.00 | 1.53 | −3 |

Only queue counts that landed on the *same* burst in both runs are compared;
cycles per packet depend on the burst size, so two runs at different bursts
differ for a reason that has nothing to do with repeatability.

**The floor is not one number, and it matters which one is used.** Split by
burst:

    burst 64          11 points   rms 1.17   (dramblast alone: 0.45)
    smaller bursts     2 points   rms 5.52

At burst 64 the forwarder is oversubscribed and the operating point is pinned by
the offered load, so the run reproduces almost exactly. At the small bursts a
saturated link produces, the burst size is an *outcome* rather than a setting,
and it wanders between runs; the largest single discrepancy is 6 cycles/packet
at a burst of 9. Every matched-burst claim in this investigation is made at
burst 64, so 1.17 — or 0.45 for dramblast specifically — is the floor they have
to clear. Quoting the pooled 2.42 would be conservative in the wrong place: it
would inflate the error on claims made exactly where the rig is most stable
while hiding that small-burst comparisons are twice as noisy as the pooled
figure suggests.

Against the burst-64 floor:

| claim | size at burst 64 | multiple of the floor |
|---|---|---|
| the `aligned_alloc`/`free` pair | 7.0 cycles/packet | 6× |
| depth 64 → 8 | 18.6 | 16× |
| depth 64 → 32 | 1.8 (2.14 from §5.14's repeats) | 2× (5× on the repeats) |

The first two are results. The third is why **one sweep per depth could not
decide the ramp model** — at twice the floor, that is all the resolution a single
sweep buys. §5.14 settled it a different way, with three interleaved paired
repeats: +2.14 cycles/packet, with the null excluded by both of the error bars
§5.14 quotes and the model's point prediction at the edge of the tighter one.
The repeats also put the effect at 2.14 rather than the single sweep's 1.8.

The fitted per-burst coefficients also reproduce: dramblast 718 then 689 (0.6σ
of the two fits' own errors), maglev −36 then −26 (0.3σ). maglev's per-burst
cost coming back negative and consistent with zero a second time is a useful
independent confirmation of §5.5 — that engine genuinely has no per-burst term,
and the negative sign is noise around zero rather than a fit going wrong once.

**What this changes retrospectively.** Every sigma quoted before this arm ran
used the within-sweep scatter, and was therefore optimistic by however large the
between-sweep drift is. That number did not exist until now. The correction has
been applied where it changes a conclusion — §5.14's single-sweep depth-32 test
moved from "confirmed at 3.1σ" to inconclusive, and had to be settled by repeats
instead — and the claims that stand at six times the floor or better are
unaffected. The general lesson is that an error
bar taken from inside a single sweep is the wrong error bar for a comparison
between sweeps, and the repeat arm is the only thing that can say by how much.

### 5.16 The backing verifier was verifying the wrong half of the matrix

`page_watch.sh` samples each live `l2fwd`'s `smaps_rollup` from outside and
`verify_backing.py` reads its log, so that every `-B` arm can be checked against
the backing it *actually got* rather than the one it asked for — `MADV_HUGEPAGE`
is advisory and `MADV_NOHUGEPAGE` can fail. Both were written for §5.2's
confound and both ran throughout.

The watcher's filter was `Rss > 1 GiB`, to skip the moment between launch and
table allocation. **Hugetlb pages are not counted in `Rss`.** They appear only in
`Private_Hugetlb`. So every run whose table is on 1 GiB pages was skipped
entirely: dramblast as shipped, which is the control, the whole allocator block
and the whole depth block. Sampling a live run shows the shape plainly —

    rss=24364  thp=0  hugetlb=10436608      (dramblast, as shipped)
    rss=8412576  thp=8388608  hugetlb=2048000   (maglev, as shipped)

— 24 MB of RSS against 10.4 GB of hugetlb. The log never looked broken, because
the maglev and 2 MiB/4 KiB arms, whose pages *do* land in `Rss`, kept writing
lines the whole time.

`verify_backing.py` then turned a gap into a pass. It judges the rows it finds,
so an arm with no samples produced no complaint, and the script printed *every
arm got the backing it claims* having never looked at the arm that carries the
result. **Absence of evidence was being reported as evidence.** It now carries an
explicit list of the arms that must be present, fails on a missing one, and
fails on one sampled too thinly to judge. Run that way it immediately named a
second arm nobody had checked — maglev on 1 GiB, which is the arm the crossover
conclusion rests on.

This is the same failure as the `--` splitting bug in §5.10, one level up: there
the verifier mislabelled what it had checked, here it stayed silent about what it
had not. Both produce the same output — a clean bill of health — and both are
worse than having no verifier, because the check is then cited as if it had
happened.

With the filter corrected, all six arms verify:

| `-B` | mode | samples | THP GiB | Hugetlb GiB | wanted |
|---|---|---|---|---|---|
| 1g | maglev | 9 | 0.00 | 9.95 | 1 GiB hugetlb |
| 4k | dramblast | 131 | 0.00 | 1.95 | 4 KiB |
| 4k | maglev | 134 | 0.00 | 1.95 | 4 KiB |
| as-shipped | dramblast | 144 | 0.00 | 9.95 | 1 GiB hugetlb |
| as-shipped | maglev | 234 | 8.00 | 1.95 | 2 MiB THP |
| thp2m | dramblast | 127 | 8.00 | 1.95 | 2 MiB THP |

**What that table can and cannot claim.** The 1.95 GiB of hugetlb in the 4 KiB
and 2 MiB rows is DPDK's own reservation, not the table, which is the right
control: if the `-B` flag had silently fallen back, the table's 8 GiB would show
up there too. But the rows differ in *when* they were taken. The 4 KiB and 2 MiB
rows were sampled during the runs that produced the measurements. The
as-shipped dramblast row was sampled during the depth-repeat block, which is
itself measurement data. The maglev-on-1-GiB row was taken by re-launching that
arm afterwards for twenty seconds, with the same binary, flag and machine state
— which is a re-verification, not a contemporaneous one. It is strong evidence
that the flag does what it says, and it is not evidence about those specific
runs. The distinction is recorded rather than smoothed over, because the point
of this section is that a verifier which overstates its coverage is the problem.

### 5.17 The fixes, and a self-inflicted detour worth recording

Four defects found by the adversarial review of this session's own code (§5.10)
were held back until every arm had been measured, so that the whole dataset
would correspond to one committed tree. It does: commit `96e6eea`, binary md5
`ecd94f3ecf801b4daaef7f3d3b188788`, archived at `~/sweeps/l2fwd.measured`.
Nothing in the matrix ran the fixed code.

The one that matters is **F4**. The amplification arm allocates and frees a
scratch buffer N times per burst, and the obvious way to stop the compiler
deleting a pair it can prove is dead — a store into the buffer before freeing it
— *does not work*. GCC 11 removes a store to an object that is about to be
freed. Disassembling the shipped binary showed exactly that: the store was gone
and the `aligned_alloc`/`free` pair survived only because the compiler happened
to be conservative about the call. `-fallocation-dce` is on by default at `-O2`
and one toolchain bump from eliding the pair outright, at which point the arm
would report that an allocator round trip costs nothing — the precise wrong
answer it exists to rule out, with no symptom anywhere. The buffer's pointer now
escapes into an empty `asm` with no memory clobber; a clobber would force spills
around the loop and change the cost being measured. The other three are
validation: `-Q 1` passed, left the queue empty at the first pop and fed an
unmasked index into a 512-bit load; `-A` accepted any negative value and all of
them silently selected the hoisted arm; and the hoisted buffer's size was a bare
`64` repeated from `main.c`'s `MAX_PKT_BURST`, now tied to it by a
`_Static_assert`.

All four verify: the tree builds clean under `-Werror`, `check_codegen.sh` still
finds both `aligned_alloc` calls, the `free` and all four software prefetches,
and `-Q 1`, `-Q 2`, `-A 99` and `-A -5` are each rejected with the intended
message.

**The detour.** Building them nearly did not happen, because `meson` was "not
found", and earlier in the session `matplotlib` and `numpy` had been "not
installed" — which was written up as another session having removed them from a
shared machine, a dependency-free SVG plotter was written to work around it, and
a commit message recorded the claim. All of that was wrong. This project's
toolchain lives in a nix dev shell: `nix develop` provides meson 0.60.3,
ninja 1.10.2, matplotlib 3.5.1 and gcc 10.3.0, and `l2fwd/README.md` says so on
line 8. Running from a plain shell makes every one of them look uninstalled, and
worse, makes `/usr/bin/gcc` (11.x, system glibc) look like the project compiler
— which is what it linked against before failing on `GLIBC_PRIVATE` symbols and
revealing the mistake.

Two things follow, and only the second is interesting. The figure work was not
wasted: the SVG plotter is kept, because the report draws every other chart the
same way and a figure that needs no environment is one fewer thing to be wrong
about. The real lesson is that *"the tool is missing" is a claim about the
environment, and it was made without checking the environment* — the same shape
as every measurement error in this log, where a number was read correctly and
compared against the wrong reference. Here a `command not found` was read
correctly and attributed to the wrong cause, and unlike a measurement error it
came with a confident story about a third party. The correction is recorded in
the commit that made it and here, rather than quietly rewritten.

### 5.18 The 4 KiB core-count term, answered without running anything

§5.12 left an open question it could not close. On 4 KiB pages the per-packet
cost rises with the **number of queues** while the burst stays pinned at 64 —
117 to 129 ticks over `q = 1..6` for dramblast — and the burst-cost model has no
term for how many cores are running. Cross-core contention for the page table
was the hypothesis, with nothing measuring it.

Nothing needed to be re-run. The page-walk counters were recorded alongside every
run from §5.9 onward, and they answer the sharp form of the question: is the
extra cost **more walks, or slower walks**?

Every queue count below is at a matched 64-packet burst.

| | q=1 | | q=6 or 10 | | |
|---|---|---|---|---|---|
| | cyc | walk cyc | cyc | walk cyc | walks/pkt |
| dramblast, 4 KiB (q=1→6) | 117 | 107.3 | 129 | 127.6 | 0.99 → 0.99 |
| maglev, 4 KiB (q=1→10) | 210 | 101.7 | 214 | 129.8 | 0.99 → 0.97 |
| dramblast, 1 GiB (control) | 98 | 0.00 | 102 | 0.01 | 0.00 |

**Walks per packet are flat** — 0.99 everywhere, one walk per lookup — and so are
instructions per packet (398.7 → 397.1 for dramblast; 285.8 → 286.0 for maglev).
What rises is **walk occupancy**: 19% over six cores for dramblast, 28% over ten
for maglev. The same binary on 1 GiB pages takes no walks at all and shows no
core-count effect whatsoever, which is the control the whole argument needs.

So the term that breaks the model is **contention for shared page-table
structures, measured in the duration of a walk rather than in how many happen.**
Which structure is contended — last-level-cache pressure from the page table's
own working set, or the shared page-walk resources — is not settled here.

**How much of it reaches the cost, at a matched queue count.** The two arms
cover different ranges (dramblast leaves burst 64 at `q=7`, maglev never does),
so the percentages above are not comparable with each other; at `q = 1 → 6` they
are:

    dramblast   walk cycles +20.3   cost +12   59% reaches the cost
    maglev      walk cycles +11.8   cost   0    0% reaches the cost

That is the **opposite direction** from the prefetch result in §5.6, and for a
reason consistent with it. A pipeline hides latency it *issued early*; it cannot
hide latency added underneath it. An engine already spending half its cycles
waiting has slack to absorb more waiting, while one retiring at nearly four
instructions per cycle has none. dramblast wins on the memory access it can
prefetch and loses on the contention it cannot.

The methodological point is smaller but worth keeping: this question stood open
for a day and was answered in ten minutes from counters already on disk. The
`.perf` sidecar next to every run exists precisely because which counter matters
changes as an investigation moves, and a question that can be answered from data
already taken should be, before any machine time is asked for.

### 5.19 Every claim, counted in ticks

§5.13 records a result that stood for six hours, survived peer review, and was
one printed tick. `l2fwd` reports "Cycle per fwd packet" as an **integer**, so at
a 64-packet burst the smallest distinguishable step is one tick — 64 cycles per
burst. That instrument underlies every matched-burst number in this document,
so the obvious question is which *other* claims here are a tick or two wide.

Each row is recomputed identically — matched burst of 64, matched queue count,
paired then averaged — so the table is comparable across rows and independent of
how each result happens to be quoted elsewhere. It regenerates with the rest of
the analysis.

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

**Every headline survives.** The engine gap, the page-size effects, the allocator
round trip and the eightfold depth change are all many ticks wide, and the
allocator result — the one that was wrong — is 8.25 ticks when measured properly
rather than the 7 it showed at the single queue count it was read from.

**Three rows needed the text changed.**

- **The refactor was quoted at 6.1%.** That figure differences two *fits* at
  burst 64. Measured directly it is 3.4 ticks, **3.3%** — half the size. The
  conclusion it supports is untouched (every later condition is still read
  against the refactored control, and 3.4 ticks with a 0.24 standard error is
  systematic, not noise), but the number was inflated by the model.
- **dramblast on 2 MiB versus 1 GiB is 3 ticks.** Real, and not a quantity to
  quote to two significant figures.
- **depth 64 → 32 is 1.8 ticks**, which is why §5.14 needed six repeats to say
  anything about it and why the verdict there is hedged.

**The repeat arm coming out below one tick is the intended result** — the same
condition re-run should not differ — and it sets the scale for reading the rest
of the table: a difference of that size anywhere else is indistinguishable from
simply running the identical experiment again.

One caution about the standard errors in that table. They describe the scatter
of numbers that were all rounded the same way, so a tight `sem` on a one-tick
difference is not evidence of anything; the tick count is the check and the
`sem` is secondary. This is the same mistake in a new costume, and it is why the
status column is computed from the tick count alone.

**What would remove the limit.** `main.c:217` divides a cumulative cycle total by
a cumulative packet count and prints the integer quotient. Both operands are
already `uint64_t` and are printed elsewhere; emitting the ratio as a float, or
simply emitting the two totals, would give roughly four more significant digits
for a one-line change. That has deliberately not been done, because it would
change the binary the whole dataset was taken with. It is the first thing to do
before the next campaign.

### 5.20 The floor arm: what the forwarder costs with no lookup at all

Every per-packet number in this document is read out of one timed region in
`main.c` — an `rte_rdtsc()` before the per-packet loop and another after it,
accumulated into `hash_tsc` — and until now nothing said how much of that region
is *not* the lookup. That gap mattered in two directions. A cost attributed to
an engine could not be stated as a fraction of anything, because the denominator
was unknown; and the per-burst coefficient `C` could not be separated from
whatever fixed cost the timed region itself carries once per burst.

The control was already in the program and had simply never been run. `-m none`
(`main.c:438`) takes the forwarding loop's third branch (`main.c:351-356`),
which writes the destination MAC exactly as the two engines do and skips only
the lookup that produced the address:

```c
        } else {
          for (uint16_t j = 0; j < nb_rx; j++) {
            unsigned dst_port = l2fwd_dst_ports[portid];
            uint64_t mac = 0xff;
            l2fwd_mac_updating(pkts_burst[j], dst_port, mac);
          }
          port_statistics[portid][lcore_id].fwded += nb_rx;
```

No new measurement code: the same `sweep.sh`, the same counters, one different
argument. The `trio` block in `run_matrix.sh` sweeps all three modes under one
tag, back to back, because the comparison it exists to make is *between* the
three and should not span the hours that `pinned3` exists to bound.

**Result 1: the packet path is not what limits this system.** With no table the
forwarder reaches the offered 93.28 Mpps at **two** queue pairs and holds it to
ten. dramblast needs seven, maglev nine. A single worker carries 65.85 Mpps on
its own, against 16.02 for dramblast and 10.78 for maglev.

**Result 2: the lookup is essentially the whole per-packet cost.** At q=1, where
all three sit at a full 64-packet burst:

| arm | cycles/packet, timed region | instructions/packet |
|---|---|---|
| no hash table | 5 | 102.5 |
| dramblast | 98 | 399.2 |
| maglev | 166 | 285.6 |

So 95% of dramblast's per-packet cost and 97% of maglev's is the lookup itself.
Every engine-versus-engine number in this document is therefore a comparison of
lookups, not of harnesses — which is the licence §5.12 and §5.13 were assuming
without having checked it.

**Result 3, and the one that was not anticipated: what the timed region does not
contain.** `rte_eth_rx_burst`, the TX buffer and the driver all sit *outside* the
timestamp pair, so no "cycles per packet" anywhere in this document includes
them. At one queue the single worker busy-polls at 100%, so the cycles it has
per packet is just its delivered clock over its delivered rate, and the
difference between that and the timed region is the invisible part of the path:

| arm | MHz/Mpps = cycles/pkt total | timed | outside |
|---|---|---|---|
| no hash table | 31.8 | 5 | 26.8 |
| dramblast | 130.8 | 98 | 32.8 |
| maglev | 194.3 | 166 | 28.3 |

Three arms whose delivered rates differ sixfold agree on **29 ± 6 cycles per
packet** of RX, TX and driver. They did not have to agree: an artefact of the
subtraction would scale with the quantity being subtracted, and this does not.
It is also what reconciles the two halves of result 1 — how an arm costing five
cycles per packet inside the region still needs two cores to hold line rate.

**Result 4, a small correction rather than a finding.** Fitted the same way as
every other arm, the floor has a per-burst term of its own: `C = 32 ± 12`
cycles/burst, `P = 8.3 ± 2.6`, which is the two timestamp reads plus loop entry
and is charged to every burst in every arm. Against dramblast's 717.6 that is
4%. It is quoted as a bound rather than a measurement, because the fit is poor
(R² 0.45) for a reason that is itself instructive: this arm is fast enough to run
down to two-packet bursts, where the printed integer is 5 to 24 and the
quantisation of §5.19 is a large fraction of the value. Nothing else in this
document moves, because every other comparison is a *difference* between two
arms and the instrument cancels in a difference. Only an absolute per-burst
figure would need this subtracted.

**What this arm does not license.** It prices the region, not the machine. The
5 cycles/packet is the MAC write and loop overhead only; the driver and NIC
costs are the 29 cycles measured above, and neither number says anything about
what a different NIC or a different DPDK version would do.

### 5.21 The report was reorganised, and now quotes the tree rather than describing it

`docs/report.html` had been grown one findings card per experiment, in the order
the experiments happened, and it read as a pile of results rather than an
argument. Three specific failures, all of arrangement rather than of
measurement:

- The crossover control (§5.12) — the thing that makes any engine-versus-engine
  comparison legitimate — sat two thirds of the way down, *after* the results
  that depend on it.
- The instrument's resolution (§5.19), which is the reason three numbers are
  quoted as ranges, was in the final section, after every one of those numbers
  had already been read.
- Retractions were interleaved with live claims, so a reader could not tell
  which parts still stood.

It is now six numbered sections, each the precondition for the next: fix the
instrument, price the floor, make the engines comparable, find what depends on
queue count, take that cost apart, say what it is worth. Corrections that are
properties of the rig are collected at the end; corrections that belong to a
particular number stay with that number, and each place says which it is.

The page also quoted `C = 645` in one section and `718` in another without
saying that these are the pre-refactor and refactored builds. They agree to
1.2σ, so nothing was wrong, but a reader had no way to know that a difference
taken *across* the two would not have been legitimate. The page now says so
where the two first meet.

Separately, claims about the code now show the code. `make_report.py:snip()`
quotes real lines out of the working tree at generation time, with their real
line numbers, anchored on exact substrings; a missing or ambiguous anchor aborts
the build rather than emitting a wrong quotation. Six snippets: the lcore
arithmetic that produced the fake collapse, the integer quotient that sets the
resolution, the forwarding loop's third branch, the two table allocations that
differ in page size, the per-burst `aligned_alloc`, and the prefetch fill. The
point is not decoration — a paraphrase of what a function does cannot be checked
against the tree, and this document has already recorded one case (§5.13) where
a claim about `aligned_alloc` rested on a published constant rather than on the
call actually being made.

### 5.22 What the table is doing, and a counter that undercounts by a hundredfold

Asked plainly what role the hash table plays in all this, the answer turned out
to be worth measuring rather than asserting, and it produced a caveat about one
of the counters this investigation has been quoting.

**What the table is.** Both engines do the same job: hash the packet's flow key,
look the hash up in a table of 2^29 sixteen-byte entries (8 GiB, `sweep.sh`
`CAPACITY`, `dramblast.h:8-11`), take the value as the destination MAC, and on a
miss consult a static backend table and insert. It is a connection tracker, one
lookup per packet.

**What makes it the workload is its size, not its algorithm.** 2^29 entries
against the generator's 16.8M flows is 3% occupancy, so the live set is ~268 MB
against this part's 52.5 MiB of L3 (`lscpu`), and the index is a hash, so there
is no locality to exploit. Every packet should be one random DRAM read. At q=1
and a full burst, per packet:

| arm | LLC-load-misses | cycles stalled on an L3 miss | cycles/packet |
|---|---|---|---|
| no hash table | 0.000 | 0.0 | 5 |
| dramblast | 0.007 | 0.8 | 98 |
| maglev | 0.853 | 85.5 | 166 |

maglev is exactly the predicted thing: ~0.85 misses per packet, and 85 of its
166 cycles stalled waiting for them.

**dramblast's 0.007 is not a hit rate, and saying why matters.** The same table
and the same flows would require a 99.3% hit rate in a cache a fifth the size of
the live set, which is arithmetically impossible for hash-distributed access.
Checked as bandwidth it is worse: 0.007 x 93.26e6 x 64 B is 42 MB/s of fill
traffic for a workload randomly touching 268 MB at 93 Mpps. The counter is not
seeing dramblast's fills. The reason is attribution: `LLC-load-misses` counts
demand loads, and dramblast's lines are brought in by `_mm_prefetch`
(`dramblast.c:67`, issued in the find loop at `dramblast.c:140-150`) ahead of the
load that consumes them.

**The code says precisely how**, and it is sharper than "a prefetch is not a
load". `dramblast_prefetch` issues `PREFETCH_T1` (`dramblast.c:71`), i.e.
`prefetcht1`, which fills **L2 and not L1** — the comment directly above it
describes `PREFETCH_T0`, which is not what the line does. So the cache line
arrives in two steps and *neither* is a demand load that misses the last-level
cache: the DRAM fill is done by the prefetch, which is not a load at all, and
the L2 → L1 move is done by the `_mm512_load_si512` gather, which is a load but
hits in L2. That accounts for 0.007 exactly, with nothing left over.

This is the same fact §5.11 recorded from the other side. There the clock-arm
method put dramblast's memory share at 17.5% while `stalls_l3_miss` said 0.9%,
and the conclusion was that the prefetch pipeline converts latency into
throughput rather than removing traffic. The miss counter now says the same
thing more bluntly: **on a software-prefetched path, both `LLC-load-misses` and
`stalls_l3_miss` measure exposure, not traffic.** Neither can be read as "how
much memory this engine touches", and this document should not be read as
claiming they do anywhere.

**The consequence for the rest of the investigation** is a framing rather than a
number. The experiment is not really about hashing; it is about servicing one
random DRAM access per packet at 93 Mpps. The engine gap is two strategies for
that access — wait for it, or issue it early and find other work. §5.12 exists
because the thing being translated is 8 GiB. The per-burst cost of §5.13 and
§5.14 exists because issuing early requires batching. And the burst-size
crossover is the point where the price of batching passes the latency batching
hides. The report now opens section 2 with this, because a reader had no way to
tell from the page what the table was for.

**Figure fixes made at the same time**, recorded because they were invisible
defects rather than cosmetic preferences. Three rotated y-axis captions were
anchored at the top of their plot area; `rotate(-90)` makes text run *upward*
from its anchor, so all three ran off the top of the viewBox and were clipped —
"core cycles / packet" by 102 px of its 124. They are now centred on the plot
area and anchored in the middle. The page-backing chart's row labels and its
"(nnn in page walks)" annotations overran both margins. The three-engine chart's
series were labelled at the ends of their lines, which cannot work when all
three converge at the offered load; both that chart and the matrix panel now
carry a separate key. All of these were found by extracting every `<text>` from
the generated SVG and checking its extent against the viewBox, not by looking at
the page — which is the only method that works on a host with no rasteriser.

### 5.23 Validating a raw PMU encoding against a workload with a known answer

`perf` has no JSON event file for this part (family 6 model 207, Emerald Rapids
/ Raptor Cove), so `perf list` shows only the ~171 architectural events and
every interesting counter has to be raw-encoded. §5.9 records what goes wrong
when that is done by assumption: the generic `dTLB-load-misses` alias read zero
on a core where the raw encoding read non-zero, and a counter that silently
reads zero is indistinguishable from a real result of zero.

A peer session needed `L1D_PEND_MISS.PENDING` and `.PENDING_CYCLES` for a
fill-buffer occupancy figure, which is the same problem again. The method that
settles it cheaply is to point the candidate encoding at a workload whose answer
is known in advance rather than at the workload under study:

```
cpu/event=0x48,umask=0x01,name=l1d_pend_miss_pending/
cpu/event=0x48,umask=0x01,cmask=0x01,name=l1d_pend_miss_pending_cycles/
cpu/event=0x48,umask=0x02,name=l1d_pend_miss_fb_full/
```

The probe is a dependent pointer chase through a 128 MiB randomly-permuted
cycle. Each load's address comes from the previous load's result, so **exactly
one L1D miss can be outstanding at a time** — the memory-level parallelism is
1.00 by construction, not by measurement. Over 400M chased loads on one pinned
core:

| counter | value |
|---|---|
| cycles | 67,643,556,510 |
| instructions | 3,007,427,737 (IPC 0.04) |
| `l1d_pend_miss_pending` | 64,096,891,488 |
| `l1d_pend_miss_pending_cycles` | 63,479,999,439 |
| `l1d_pend_miss_fb_full` | 16,372,232 |

`pending / pending_cycles` = **1.0097** outstanding misses, against a ground
truth of 1.00 — so the encoding counts what it claims. Two corroborating
readings fall out of the same run: `pending_cycles / cycles` = 93.9%, i.e. the
core has a miss in flight almost always, which is what a serial chase must look
like; and `fb_full` is negligible, which it must be when one miss is outstanding.
All six events ran together without multiplexing.

The general point is worth stating separately from the encoding, because the
encoding will be obsolete on the next part and the method will not: **an
unverified raw encoding is a measurement of unknown provenance, and the cheapest
way to verify one is a workload whose answer you already know.** A chase gives
occupancy 1; a streaming read gives a known miss count per cache line; an empty
loop gives zero. Ten seconds of that is worth more than any amount of reasoning
about which umask the manual implies.

Note also the distinction the ratio hides, since it decides what a figure's axis
means: `pending / pending_cycles` is the average occupancy *while any miss is
outstanding*, while `pending / cycles` is occupancy averaged over all cycles.
On a batched, software-prefetched loop those differ substantially, and §5.22's
result — that a prefetched path shows almost no demand misses while moving the
same traffic — is precisely the regime where quoting the wrong one inverts the
conclusion.

### 5.24 The PMU counters were never multiplexed, now checked rather than assumed

`sweep.sh` carries the claim that its six events "fit the PMU without
multiplexing on this part: all six report 100.00% enabled". That was true when
it was written and was never checked again, and nothing downstream could have
noticed if it stopped being true: a multiplexed counter is scaled up to a
full-window estimate before `perf` prints it, so it is numerically
indistinguishable from a measured one. Every PMU-derived result in this
document — the page-walk decomposition of §5.12 and §5.18, the stall-counter
corroboration of §5.11, the miss counts of §5.22 — would have quietly become
extrapolation.

**Audited.** Across all 310 `.perf` sidecars, 1,860 counter readings, six events
each: **every reading is 100.00% enabled.** The dataset is clean; nothing needs
revisiting.

**Why it fits — and a retraction, because the first answer given here was
wrong.** The original version of this section reasoned that with SMT a logical
CPU gets four general-purpose counters, that `cycles` and `instructions` take
fixed-function ones, and that the four raw events therefore fill the budget
exactly, so *one more raw event would silently multiplex all of them*. That
argument was tidy, it was consistent with every reading being at 100%, a peer
session reported a measurement that appeared to confirm it — and it is not what
this machine does.

Measured directly, on CPU 26, with the shipped set plus extra raw events:

| raw events | enabled |
|---|---|
| 4 (the shipped set) | 100.00% on all six |
| 5 | 100.00% on all seven |
| 6 | 74.43 – 87.61%, and 61.95% on the last |
| 7 | 55.43 – 78.14% |

Two further checks: making the SMT sibling busy (a spinner pinned to CPU 54, the
sibling of 26) does not change it, and neither does asking perf to count on both
siblings — five raw events still read 100.00% in all three conditions, so the
static-partitioning story is not the mechanism either.

**And "the ceiling is five" is wrong too, in exactly the same way.** The table
above varies one thing — the count — and reads a rule off it, which is the
identical mistake one step over. The extra events in it happened to be two
umasks of `MEM_LOAD_RETIRED` (0xd1). Holding the count fixed and varying *which*
events instead:

| set | raw | enabled |
|---|---|---|
| shipped + `0xd1/01` + `0xd1/02` | 6 | 61.97 – 87.61% |
| shipped + `0xd1/01` + `0xc4/00` | 6 | **100.00%** |
| shipped + `0xc4/00` + `0xc5/00` | 6 | **100.00%** |
| shipped + `0xd1/01` + `0xd1/02` + `0xd1/04` | 7 | 55.61 – 77.95% |
| shipped + `0xc4/00` + `0xc5/00` + `0xd1/01` | **7** | **100.00%** |

**Seven raw events schedule at 100%; six multiplex.** The count does not predict
the outcome anywhere in the range that matters here. What predicts it is whether
two events share a restricted family: two `0xd1` umasks together collide, one
does not, and mixing families is fine at seven. This is the same shape the peer
session isolated independently on their own set — three `L1D_PEND_MISS` (0x48)
umasks together with two `OFFCORE_REQUESTS_OUTSTANDING` (0x20) umasks fail,
while four 0x48 umasks alone are fine and three 0x48 plus two generic events are
fine. Two different colliding families, found by two sessions, same mechanism:
per-event counter restrictions over-constraining each other, not a budget.

So the shipped set's headroom cannot be stated as a number at all. It has room
for some events and not others, and which is which is not predictable from
anything this document knows.

**And the premise underneath all of it was never true here.** The peer session
then tested *upward*, which neither of us had done — every table above starts
from a set that fails and works down. They measured **eight** generic raw events
at 100.00% enabled, alongside their five-event set that collides. Eight schedule
while five collide. So "with SMT a logical CPU gets four general-purpose
counters" — the sentence this entire section was built on, in its original form
and in both of its corrections — **is simply not true of this host.**

That premise was never written down as a finding by either session. It was
inherited as background knowledge, and inherited knowledge does not get phrased
as a claim, so it never entered the set of things under test. Both wrong
ceilings were downstream of it: four was the premise with nothing added, five
was the premise plus a correction. Each of us revised the *conclusion* twice
while the thing generating the conclusion sat underneath, unexamined. The
spinner-on-the-sibling check recorded above was the closest either of us came,
and it is worth being exact about how it failed: it tested whether the
partitioning was *dynamic*, which presupposes that partitioning exists. It could
not have discovered that there was none.

So the pattern recorded below extends. The dangerous shape is a true measurement
plus a generalisation that predicts it — and the generalisation is most
dangerous when it is **inherited rather than derived**, because then it is never
written as a claim, never cited, and never tested. The operational form: when a
conclusion keeps needing revision, suspect the premise that has not changed.

**What the peer's number then means.** They measured five raw events multiplexing
at 57–86% with *their* event set, and I read that as confirming a general
ceiling of four. It confirms no such thing: it is a fact about their event set,
not about the part — and, as the table above shows, not even a fact about the
count within their set. I had told them the ceiling was four and that they could
drop an event to reach it; they were right to keep the counter instead, and the
correction has been sent.

**The pattern is worth naming because it caught both sessions twice.** The
dangerous shape here is not a wrong measurement. Every measurement involved was
correct: their five raw events really did multiplex, my five raw events really
did fit, my six really did multiplex. The failure each time was a true
measurement plus a generalisation that *predicts the measurement already in
hand* — which is why it survives inspection, and why neither of us could have
caught it alone with one event set each. The thing that broke it open was
building the hardware half of the test rather than the synthetic half: a
synthetic test can only check what its author already believed, so it would have
passed forever, while the hardware disagreed with both of us.

**The transferable rule is the one that survives both results.** The number of
events that fit is not knowable by counting them, so it must be read off each
run. That is precisely what the guard does, and this correction is an argument
for it rather than against it: had this document kept relying on "four raw
events fit", the next campaign would have added a fifth, been correct by
accident, added a sixth, and had no idea.

**The guard**, now in `perf_csv.py` so that it can be imported and tested, drops
any reading below 99.99% enabled rather than storing it, and says so loudly.
`test_perf_guard.py` fires it two ways, and the second is the one that matters
(the idea is a peer session's, arrived at while closing the same gap on their
own harness):

- **Synthetic**, seven checks against hand-built perf output: a clean sidecar is
  accepted whole, a 41.63% reading is refused *and named*, the 99.99% boundary
  holds on both sides, `<not counted>` is neither stored nor read as zero, a row
  with no enabled column still parses, and one bad reading does not take the
  good ones with it. One check exists purely as a regression pin: an
  `instructions` row carrying an IPC in field 5 must not be read as an enabled
  percentage.
- **Real** (`--real`), which oversubscribes the PMU on purpose and asserts the
  guard rejects what comes back: the shipped set reads 100.00% and is accepted,
  six raw events read 62.36–87.61% and every one of the eight readings is
  refused. The multiplexing is produced by the hardware, so this exercises the
  actual failure mode rather than a reconstruction of it — and unlike a
  hand-edited percentage it cannot rot, because a part with a different counter
  budget changes the test's answer instead of letting it pass forever.

Two traps found while writing it, both of which made the test pass while
measuring nothing, and both of which are why it is worth writing the test
rather than reasoning about the guard:

- **Count per-CPU, not per-task.** `perf stat -e ... -- sleep 2` reports 100.00%
  enabled however many events are requested, because the task is off-CPU almost
  the whole time and enabled and running time are both ~zero. The first version
  of the real test passed on six oversubscribed events for exactly this reason.
  A CPU accumulates time whatever is scheduled on it, so `-C` is required — which
  is also how `sweep.sh` measures.
- **`name=r5` is a parser error, not a name.** perf reads a bare `rNNN` as its
  own raw-event syntax, so naming a probe event `r5` fails to parse. It failed
  loudly, which is the desired behaviour, but it cost a round of debugging.

**A third appearance of the field-index bug, and the first one that defeats the
guard rather than tripping it.** A raw event spec contains commas, so with
`-x,` an *unnamed* spec splits across extra columns:

```
59304,,cpu/event=0x12,umask=0x0e/,1000954706,100.00,,
```

The event column now holds `cpu/event=0x12` and the enabled column holds a run
time in nanoseconds — comfortably above any threshold — so the reading sailed
through the multiplexing check and was stored under a truncated name with the
guard never actually applied to it. Verified directly here, and the parser did
exactly that before this was fixed.

Passing `name=` in every raw spec prevents it, which `sweep.sh` does, and which
is why the 310-sidecar audit was valid. But the parser must not depend on the
producer having remembered. `parse_perf` now returns malformed rows as a third
category, separate from multiplexed ones and reported more loudly, because the
two mean different things: a multiplexed reading means the machine was busy, a
malformed one means the parser was reading the wrong columns and nothing it
produced can be trusted. Two checks catch it — a raw-spec fragment in the event
column, and an enabled value outside 0–100 — with test cases for both.

**Why the guard was blind to this, stated generally.** A check of the form
`enabled < 99.99` can only ever see a **low** number — and every way of getting
the columns wrong produces a high one, a non-number, or no column at all. The
guard was structurally blind to its own most likely failure mode. That
formulation is the peer session's, arrived at when they found the identical hole
in their parser; theirs was worse in one specific respect, which is worth
recording because it is a tempting shape: their enabled parse was wrapped in a
`try/except` that substituted **100.0** on failure, so "I cannot read this"
silently became "it is fine" inside the function whose entire job is to refuse
what it cannot vouch for.

Checked here rather than assumed, and this parser had two of the four shapes
open — a non-numeric enabled column was accepted, and so was a row with no
enabled column at all. The second was worse than an oversight: it was pinned by
a *test case asserting it*, written on the reasoning that a perf version
omitting the column would otherwise drop every counter. That reasoning was
wrong, and the test made the wrong belief look deliberate. Breaking loudly when
the producer changes its output is the correct behaviour for a guard, not a cost
to be designed around — the same lesson as the test-label problem two paragraphs
up, in a different costume. Every path out of the parser is now explicit: a
reading is stored only when the enabled column exists, parses, and lies in
range. Five new cases cover the shapes, including one asserting that a genuinely
multiplexed reading is still filed as *multiplexed* and not swept into the new
category, which is the regression the fix could easily have introduced.

The peer session hit the column shift while building the table above, and caught
it only because the mis-read column printed as `1958811208.00%`, which is absurd
on its face. Had the shift landed on a column holding a value between 0 and 100 it
would have read as a plausible percentage and been published. That is the third
time one field index has produced a wrong result in this investigation, in three
different disguises: reading the derived metric as the enabled percentage,
naming an event in a way perf parses as something else, and now a producer-side
omission that silently moves every column. The rule that covers all three:
**a CSV column index is only correct relative to a producer that has not changed
its mind, so a parser must check what it is looking at rather than count.**

**One trap in `perf`'s own output**, passed on by the peer session running the
`find_batch` campaign, who hit it and rejected two good runs before catching it.
The `-x,` fields are:

```
0 value   1 unit   2 event   3 run_time_ns   4 enabled_pct   5 metric   6 metric_unit
```

Field **5 is the derived metric, not the enabled percentage**. On the
`instructions` row it holds the IPC, so a guard reading `f[5]` reads a perfectly
healthy 1.47 IPC as "1.47% enabled" and throws away a good run. Enabled is
field 4. The extractor here had no guard at all rather than a wrong one, so it
was not affected, but the index is now written down next to the code that uses
it.

**Independent corroboration of §5.22, from a different program.** The same peer
pointed L1 fill-buffer occupancy (`L1D_PEND_MISS.PENDING` / `.PENDING_CYCLES`,
the encoding validated in §5.23) at their own prefetch-heavy loop and measured
occupancy flat at ~1.13 across a 64× range of pipeline depth, never approaching
the ~16 fill-buffer ceiling their hypothesis predicted. Their explanation is
structurally identical to §5.22's: their enqueue prefetch is `prefetcht2`, which
parks the line in L2 and allocates no L1 fill buffer, so the L1 level sees
almost nothing. Two different programs, two different counters, two different
sessions, same mechanism — **a software prefetch moves the traffic out of view
of the counters that watch demand paths.** Their loop and dramblast's differ in
one respect worth recording: theirs issues a second, `prefetcht0` stage eight
keys ahead to pull the line L2 → L1, and dramblast has no such stage. Whether
dramblast's demand gather is therefore paying an L2 hit it need not pay, and
whether `PREFETCH_T0` (which its own comment describes) would be cheaper, is an
untested one-line question and a good candidate for the next campaign.

**A datum on that question, from the peer's own loop, offered with its own
caveat.** They compared the two-stage arrangement (`prefetcht2` far plus
`prefetcht0` near) against a single `prefetcht0` at the far distance, and the
single long-range T0 **won by 8.4%** — 19.55 → 17.91 cycles per operation at
depth 63. On their workload, paying to land in L1 at long range beats landing in
L2 and topping up. They were explicit that this is not transferable: dramblast's
gather consumes the line differently and its lead distance is set by a different
mechanism, so it is a reason to run the experiment rather than a prediction of
its result.

**What running it here would take, and why it has not been run.** The project's
own pattern says the change should be a *runtime* flag, not an edited constant —
`-B`, `-A` and `-Q` all exist precisely so one binary measures both arms of a
comparison and no result rests on "we rebuilt it in between". A `-P t0|t1` in
the same style plus one sweep arm is perhaps twenty minutes of work and nine
minutes of machine time. It has deliberately not been done in this session:
every number in `docs/` was taken with the current binary, the control arm that
licenses cross-binary comparison (§ CONTROL, `pinned2` against `pinned`) would
have to be re-run to license another one, and there is a presentation pending. It
is a decision for the user rather than a gap to be quietly filled.

**One layer further out, and it is a regression the hardening itself created.**
The guard refuses a bad reading by leaving the key absent from the record. Every
consumer of those counters then did `(rec.get(key) or 0) / packets`, so a
*refused* counter came back as a confident **zero** — which is the exact failure
the guard exists to prevent, displaced one layer outward, and made more likely
by the guard rather than less. It matters most in the two places a zero is also
a real answer: the no-table arm legitimately reads 0.000 L3 misses per packet in
§5.22's table, and the 1 GiB control legitimately takes zero page walks in
§5.18, which is the control the whole core-count argument rests on. A rendered
zero was indistinguishable from a counter that was never recorded.

`per_pkt()` now returns `None` for an absent counter. The report prints an em
dash where one is missing rather than a number, and `walk_rows` drops such a run
from the section instead of letting it contribute a zero to it. Nothing on the
published page moves — the dataset is complete, and the output is byte-identical
— so the only thing that changed is what happens when it is not.

**And the fix was verified by inspection, which is what had missed the bug in
the first place.** Injecting an absent counter instead — blanking the L3-miss
reading on the trio `dramblast` q=1 run, and the page-walk counters on a 1 GiB
control run, in a scratch copy — found the repair was half done in one shot. The
table declined correctly and printed its em dash; the paragraph *underneath* it,
which argues from those three numbers, still died with `unsupported format
string passed to NoneType`. That is not a crash in the useful sense: it names a
formatting fault, not a missing measurement, and it is the same false-diagnosis
shape recorded below. The paragraph now withholds itself and names the counter
that is absent, so the page degrades into saying less rather than into saying
something untrue or into a stack trace. On the real data the output is
byte-identical, so nothing on the published page depends on any of this — only
what happens when a counter is one day missing does.

**The rule, sharpened by the peer's version of the same bug.** They found it in
their own occupancy calculation, where an unmeasurable denominator became
`0.00`, was written to a CSV, averaged into a median and plotted. Their
observation is the part worth keeping: the finding that file exists to support
is that occupancy is *low*, so a fabricated `0.00` does not contradict the
conclusion — **it strengthens it**. A spurious 16 would have had them tearing
the harness apart; a spurious 0 would have had them writing a bolder sentence.
So: a guard that refuses a value has not finished until every consumer
distinguishes "refused" from a legal reading, and **the danger is proportional
to how well the fabricated value agrees with what you expect**. Both of the rows
this section protects — 0.000 L3 misses on the no-table arm, zero page walks on
the 1 GiB control — are exactly that case: a fabricated zero indistinguishable
from a real one, in the rows a reader leans on hardest.

The peer session found the same shape from the other side, which is what
prompted looking: a short row in their parser was correctly rejected, but the
rejection read "perf did not report ['cycles']", when perf had reported it
perfectly well and the output *format* had changed underneath. Correct outcome,
false diagnosis, and safe by accident of a whitelist rather than by design. The
general form is worth stating because it is not the same as being wrong: **a
correct verdict reached for a false reason sends the next reader after the wrong
thing**, and it is invisible precisely because the verdict is right.

**A closing note on what this exchange is evidence of.** Two sessions agreed,
repeatedly, and the agreement was the weakest evidence in it — twice it was the
thing that locked an error in rather than the thing that caught one. Both
sessions had inherited the same premise, so concurrence added no independent
observation; it only added confidence. The genuinely independent things were the
ones that did not share a premise: a workload with a ground-truth answer, a
counter asked to do something its author had not predicted, and a test that
oversubscribes real hardware rather than replaying what its author believed.
Those are what moved the conclusions. Every conclusion moved by agreement
subsequently had to be retracted.

### 5.25 The literals in the report, and a checker that nearly broke one

Almost everything in `docs/report.html` is generated from
`results_reproduced.json`, so it cannot drift. But a dozen *measured* values are
written into the prose as literals, because an IPC or a walk occupancy reads
better mid-sentence than a format specifier. Those can go stale silently, and
nothing else would notice: the figure beside them regenerates, the sentence does
not, and no layer produces an error.

The prompt to check was a peer session's observation about their own report,
whose figures are generated and whose prose is hand-written. Their point was
sharper than the general hazard: **their newest material was their least
guarded** — their claim-checker covered sections 1–5 while the section written
that night, and all its figures, went in with nothing checking them. The same
was true here. Section 2 (the floor arm) and its numbers went in on the same
night, and every guard written in this investigation had been pointed at the
PMU pipeline rather than at the page.

`check_report_numbers.py` re-derives each literal and fails on drift. All ten
currently match. Two things it caught are worth recording, because neither is
the thing it was written to find.

**It nearly made a correct number wrong.** The page says dramblast on 2 MiB
pages "spends 27% of every core cycle with a walk outstanding". The first
version of the checker derived that as walk occupancy over the *timed region's*
cycles, got 35.7%, and reported drift. The literal was right and the checker was
wrong: "every core cycle" includes the RX/TX path outside the `rdtsc` pair, so
the denominator is `pmu_cycles`, and against that the page's 27% is 26.9%. Had
this been run and believed at face value, it would have sent someone to "correct"
an accurate number — which is worse than having no checker, because a checker
carries authority. Every claim in the file now names its denominator, and where
two are plausible both are printed side by side.

**Its own matcher went stale before its first run.** Each check first asserts
the literal still appears on the page, so that a check cannot pass vacuously
against prose that has been rewritten. One of those matchers failed — not
because the prose had changed, but because the generated HTML wraps at about 78
columns and the phrase straddled a line break. A stale checker reporting stale
prose. It now matches against whitespace-normalised text.

Both are the same shape as everything else in §5.24: the verification layer
failing in a way that looks like a finding. The general form worth carrying
forward is that **a checker is itself an unverified claim about what the
document says**, and it should be injected against before it is trusted — which
is how §5.24's absent-counter fix was found to be half done, one section up.

**So this one was injected, having initially been verified the wrong way.** It
was written, run against the real data, and reported ten matches — which proves
the happy path and nothing else, the exact mistake it exists to catch in the
page. `--self-test` now fires all four branches against a scratch copy:

1. an unchanged copy passes;
2. drifting the *data* under unchanged prose fires the affected claims and only
   those;
3. changing the *prose* reports the phrase absent rather than skipping the check
   silently;
4. a missing page is a hard exit, not ten failed checks.

The fourth exists because of a trap a peer session fell into and recognised:
they ran their equivalent against a scratch *data* directory while the page path
still resolved relative to the script, so the page was never opened, every
literal reported missing, and twelve failures looked exactly like a guard doing
its job. **An injection that fails for the wrong reason is indistinguishable
from one that works.** Both paths here now come from one argument, and an
absent file exits with a message naming it.

Writing the self-test then produced one more instance of the same family: `load()`
rebinds the module-level `DOCS`, so the restore step in branch 3 copied the
scratch directory onto itself. A helper quietly mutating shared state so that a
later step operates on something other than what it names — which is, in
miniature, every bug in §5.24 and §5.25.

**What the peer's tree produced from the same warning**, recorded because it is
the outcome that justifies the exercise: four literals on their *published* page
were wrong at the moment the warning was sent. They had re-taken a dataset,
updated the table, and left the prose around it untouched — including one claim
("FB_FULL never exceeds 1.5%", against a measured 1.62%) that was simply false,
sitting under a caption naming the file that contradicted it. Ten literals here
were checked and all ten held; the difference is not care, it is that this
page's numbers are mostly generated while theirs were mostly typed. The
generated architecture makes this failure *rarer*, which is worse for noticing
and is the reason the dozen exceptions needed a guard of their own.

### 5.26 dramblast's find path never finds anything (2026-09-16)

Asked to look over the dramblast implementation for optimization
opportunities, the first thing found was not an optimization. The SIMD find
path compares the search key against the wrong half of the cache line, so it
returns a miss for every key in the table — including keys inserted
microseconds earlier.

#### The defect

A bucket is 64 bytes holding four `dramblast_kv_t`, and that type is
`{uint64_t k; uint64_t v;}` (`dramblast.h:8-11`), so as eight qwords the line is
`k0 v0 k1 v1 k2 v2 k3 v3` — **keys on the even lanes, values on the odd ones**.
The find loop masks the comparison to a subset of lanes:

```c
#define DRAMBLAST_SIMD_KEY_MASK 0b10101010
...
__mmask8 key_cmp = _mm512_mask_cmpeq_epu64_mask(DRAMBLAST_SIMD_KEY_MASK,
                                                cacheline, key_vector);
if (key_cmp > 0) {
  int offset = __builtin_ctz(key_cmp);
  result->v = cacheline[(offset + 1)];
```

`0b10101010` selects lanes 1, 3, 5, 7 — the **value** lanes. The search key is
therefore compared against stored MAC addresses and never against a stored key.

The `+ 1` is itself the proof, and needs no measurement: it is only meaningful
for an even `offset`, because the value of the pair whose key matched at lane
`2i` lives at lane `2i + 1`. A mask of `0b10101010` can only ever yield an odd
`offset`, for which `offset + 1` is the *next pair's key* — and at `offset = 7`
it is `cacheline[8]`, one past the end of an eight-lane vector. The two lines
cannot both be right, and the constant is the one that is wrong.

#### Confirmed by running, because the disassembly is ambiguous

Reading was not enough here, for a reason worth recording. The peer DRAMHiT
session, asked independently, quoted the upstream `Item::find_simd`
(`kvtypes.hpp:640-670`), which uses `0b01010101` over exactly this layout and
the same `bucket[idx+1]` idiom — strong evidence, but it also warned that the
mask's *shape* in the object code is build-dependent: GCC may fold it into an
EVEX write-mask register or leave the compare unmasked and apply the constant
afterwards on a GPR. Grepping the disassembly for `0x55` would have found
nothing either way. (This build takes the first form: `mov $0xffffffaa,%eax`,
`kmovb %eax,%k1`, `vpcmpequq %zmm1,%zmm0,%k0{%k1}`.)

The general form of that warning, after the peer corrected their own framing of
it: the two encodings they meant were the two ways a *correct* mask can appear,
but this case is worse than that, because the constant is plainly visible in the
expected EVEX form and is simply the wrong value. Grepping for `0x55` finds
nothing and proves nothing. **Read the constant that is there; do not search for
the one you expect.**

It is also a port defect rather than an inherited one. Upstream DRAMHiT has
`constexpr __mmask8 KEYMSK = 0b01010101` (`kvtypes.hpp:643`), used for both the
key compare (:649) and the empty-slot compare (:663), and the peer reports its
correctness harness passing all 28 cases on this host — hits, misses, empty
keys, reprobes and a soak — which a `0xAA` mask could not survive. The bit
pattern was inverted when the code was brought into this tree.

So the question was settled by execution. `l2fwd/test_dramblast_find.c` links
the real `libsashstore`, inserts 64 keys, reads them back with a **scalar**
probe that does not use the SIMD path at all — establishing independently that
the inserts are really there — and then looks the same 64 keys up:

| arm | present (scalar readback) | find hits | hits correct |
|---|---|---|---|
| shipped, mask `0b10101010` | 64 | **0** | 0 |
| mask `0b01010101` | 64 | **64** | 64 |

One character. Nothing else differs between the two columns.

#### Why nothing caught it

Because the forwarder cannot tell the two apart. `populate_lut` fills the entire
backend table with `0xff` and returns before reaching the Maglev code
(`conshash.c:22-26`), so a hit and a miss produce the *same destination MAC*.
`dramblast_process_frames` reacts to a miss by consulting that LUT and calling
`dramblast_insert_one` (`dramblast.c:282-291`), which finds the key already
present and updates it. Every packet is forwarded, to the right place, with the
right address, and the packet counters are identical. There is no error path, no
dropped packet and no log line — the only observable is speed.

That also means **every dramblast number in this document was measured on a
100%-miss workload.** Not a workload with a poor hit rate: a workload where the
hit rate is exactly zero by construction. §5.22 is the section this most
directly revises. It reported dramblast at 0.007 `LLC-load-misses` per packet
and explained the number as prefetch attribution — the fills being done by
`_mm_prefetch` rather than by a demand load. That explanation stands, and is
still the reason the counter cannot see the traffic. But the section went on to
frame dramblast as "one random DRAM read per packet", and that is not what the
shipped code does. It does a read *and* a write: the insert that every miss
triggers stores through the same line the find just examined, turning a clean
read into a read-for-ownership and a later writeback.

#### What it costs

`l2fwd/bench_dramblast_path.c` drives the real `dramblast_process_frames` in
bursts of 64, as `main.c:337` does, over a table at the rig's 3% occupancy.
`l2fwd/check_dramblast_arms.sh` builds each arm from a copy of the real source
with exactly one edit, and interleaves the arms across repeats. Nine repeats,
TSC ticks per packet, median:

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

**Scope, stated because it limits every row.** This is a 256 MiB table on 2 MiB
THP with no NIC, not the rig's 8 GiB on 1 GiB pages behind a 100 Gbps port. It
is several times this part's 52.5 MiB L3, so the access is still a DRAM access
and the read-versus-read-modify-write comparison is still the right one, but the
absolute ticks are not comparable to the rig's ~98 and the ordering is
indicative rather than final. The rig has to confirm it.

**A second limit, and it is mine.** These were taken on the shared housekeeping
cpuset (cores 24-27,52-55), because the attempt to run them inside `bench.slice`
was refused by this session's own permission settings. An earlier pass of the
identical sweep, run while two peer sessions were compiling on that set, read
178 ticks with a standard deviation of 60 against 53 on a quiet machine — 52-55
are the hyperthread siblings of 24-27, so a compile on 52 halves core 24. The
numbers above were re-taken after the peers moved off, use a median rather than
a mean, and have standard deviations of 0.25-1.1. The ranking was identical in
both passes.

#### Three candidates the measurement killed

Recorded because they were all read straight off the disassembly and all three
looked convincing there.

- **The prefetch hint constants are inverted, and correcting them buys almost
  nothing.** `dramblast.c:60-64` defines `PREFETCH_T0 0, PREFETCH_T1 1,
  PREFETCH_T2 2, PREFETCH_NTA 3`. The ISA encoding is the exact reverse —
  `_MM_HINT_T0 = 3, _MM_HINT_T1 = 2, _MM_HINT_T2 = 1, _MM_HINT_NTA = 0`
  (`xmmintrin.h:42-45`). So the shipped `PREFETCH_T1` emits **`prefetcht2`**,
  confirmed in the object code (`prefetcht2 (%r14,%rax,1)`), and `PREFETCH_NTA`
  would emit `prefetcht0`. **§5.22 is wrong on this point**: it states the line
  is `prefetcht1`. Correcting the constant to a true `prefetcht1` changes
  nothing measurable (35.6 against 35.7), which is expected — T1 and T2 both
  land in L2 on this part. Going all the way to `prefetcht0`, which also fills
  L1, is worth about 0.9 ticks: an interleaved seven-repeat paired test gives
  **−0.95 ± 0.24**, real but small. The inverted table is worth fixing for
  honesty regardless of the ~1 tick, because the source currently says one thing
  and the silicon does another.
- **Hoisting the find loop's state into locals does nothing.** The shipped loop
  reaches the queue through three helpers that each re-derive `&ht->queues[id]`,
  and the object code reloads `ht->len` inside the push loop (`mov 0x8(%rbx),%rdx`)
  and recomputes the mask every iteration, because a store through
  `dramblast_queue_item_t *` may alias the header. `opt/dramblast-hoist.patch`
  reads all of it once. Measured: 35.3 against 35.7, inside the scatter. The
  loop is not issue-bound at this occupancy; the removed instructions were
  running in slack.
- **Removing the vector spill on the hit path makes it slightly worse.**
  `cacheline[(offset + 1)]` subscripts an `__m512i` by a variable, which GCC
  implements by storing all 64 bytes to the stack (`vmovdqa64 %zmm0,0x40(%rsp)`)
  and reloading 8 — a wide-store-narrow-load forwarding stall on every hit.
  Reading the value from the table line instead, which is in L1 by then,
  measured 36.8 against 35.7: **worse**, not better. Recorded as a refuted
  hypothesis rather than dropped.

#### The flow hash, which is not dramblast's but is on its path

Separately measured, because it sits inside every per-packet number here and had
never been separated from the lookup it precedes. `flowhash()`
(`packettool.c:110`) calls `fnv_1_multi()` three times over 8, 1 and 4 bytes;
`fnv_1_multi` (`hash.c:12`) is a byte-at-a-time loop carrying its state through
an `imul`, so thirteen bytes are thirteen 3-cycle multiplies in series. Neither
function is inlined into the caller — `libsashstore/meson.build` sets
`override_options: ['b_lto=false']` on the whole library, so `main.c` is built
with `-flto=auto` and the entire lookup library is not, and `flowhash` remains
an out-of-line call making three more.

`l2fwd/bench_flowhash.c`, TSC ticks per packet:

| | throughput | latency |
|---|---|---|
| FNV, as shipped | 34.0 | 65.4 |
| CRC32C over the same 13 bytes | 5.6 | 34.0 |

The throughput column is the forwarder's regime — `main.c:322-333` hashes every
packet of a burst with no dependency between them, so the hashes overlap. The
first version of this benchmark measured only the latency column and would have
overstated the saving by a factor of two; the number to quote is **~28 ticks per
packet**. CRC32C needs no justification as the comparison arm: dramblast already
runs `_mm_crc32_u64` on this hash's output (`dramblast.c:78`).

The caveat that has to travel with it: changing the flow hash changes which
flows collide, so it changes the measured workload and not merely its speed.

#### One more build-configuration finding

DPDK 21.11's `libdpdk-libs.pc` carries `-march=nehalem` in its `Cflags`, and
meson appends dependency flags *after* project arguments, so every translation
unit in this project is compiled as
`... -mavx512f -mavx512dq -march=native ... -march=nehalem`, and the last
`-march` wins. Verified with `gcc -Q --help=target`: the effective target is
`-march=nehalem -mtune=nehalem`, with `-mbmi2` and `-mfma` **disabled** and
`-mavx256-split-unaligned-load/store` **enabled**, against `-march=cooperlake`
for `-march=native` alone on this part. The explicit `-mavx512f`/`-mavx512dq`
survive, which is why the AVX-512 code compiles at all and why this was
invisible. One visible consequence in the find loop: `bsf` where a BMI build
would use `tzcnt`.

This has not been measured and no change is proposed for it yet. It is recorded
because it means the entire dataset was taken from a binary tuned for a 2008
microarchitecture, and because re-ordering the flags would change the binary
every measurement in this document was taken with — the same reason §5.19 gives
for not yet widening the printed cycle counter.

#### Repository additions

- `l2fwd/test_dramblast_find.c` — the insert-then-find correctness harness.
- `l2fwd/bench_dramblast_path.c` — per-packet cost of the real
  `dramblast_process_frames`.
- `l2fwd/bench_flowhash.c` — FNV against CRC32C, throughput and latency.
- `l2fwd/check_dramblast_arms.sh` — builds every arm from a copy of the real
  source with one edit each, interleaves them, writes the TSV.
- `l2fwd/opt/dramblast-hoist.patch` — the loop-hoisting change, kept although it
  measured flat, so the refutation is reproducible.
- `l2fwd/plot_dramblast_arms.py` — `docs/dramblast_arms.svg`.

Nothing under `l2fwd/libsashstore/` has been modified. The find-mask fix is a
one-character change and is not applied.

### 5.27 Every resource that could be the limit, and how each one is measured

Everything in this document so far is a **cost**: cycles or ticks per packet,
attributed to a piece of code. A cost says how expensive something is. It does
not say whether the machine has any headroom left in the resource that cost is
drawn from, and therefore cannot answer the question that decides whether an
optimization is worth making — if a shared resource is already at its ceiling,
removing work elsewhere buys nothing.

So this section enumerates every resource that could plausibly be the limit,
gives each one a ceiling, and names the instrument that measures its
utilisation. The enumeration comes first deliberately: a bottleneck that was
never on the list cannot be found by refining the measurement of one that was.

#### The resources, their ceilings, and where the ceiling comes from

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

Two of these have a measured ceiling as well as a nominal one, and the measured
one is what the utilisation should be divided by: DRAM's achievable streaming
bandwidth is well below 307.2 GB/s, and `validate_counters.sh` measures it.

#### The instruments, and why they are split into groups

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

Top-down is the load-bearing addition. It partitions **every** issue slot into
one of four fates, so unlike any single counter it cannot miss a bottleneck by
not having been asked about it — whatever the core is waiting on shows up
somewhere in the four. Level 2 then splits the backend into memory and core,
which is precisely the distinction §5.11 and §5.22 could only approach
indirectly, from clock-arm ratios and a miss counter that turned out to measure
exposure rather than traffic.

The events are split across six groups, each collected in its **own** `l2fwd`
run, because a group large enough to hold them all would multiplex and silently
scale every value (§5.24). That is expensive — one run per group per queue count
— and it is why the default queue list is short. `saturation.sh` re-checks the
enabled percentage on every run and marks the summary line `MULTIPLEXED` rather
than leaving it to the analysis to notice.

`L1D_PEND_MISS.PENDING`/`PENDING_CYCLES` and `FB_FULL` are deliberately in
*different* groups. All three are event 0x48, and the peer DRAMHiT session found
that three simultaneous 0x48-family events collide in the event scheduler — a
general scheduling property rather than a quirk of particular codes. Splitting
them costs an extra run and removes the collision.

#### The axis is core count, and why that is the discriminating one

A per-core resource — issue slots, fill buffers, the page walker — holds its
utilisation roughly flat as cores are added, because each core brings its own.
A shared resource — DRAM bandwidth, the memory controller, the NIC, the LLC —
climbs towards its ceiling and then flattens the throughput curve. Plotting
utilisation against worker count therefore separates the two classes without
needing to model either, and the resource whose curve reaches the ceiling first
is the answer.

This also gives the §1 collapse a second look it has never had. That collapse
was diagnosed as an invocation defect (§2) and did not reproduce once the
invocation was corrected, but no measurement has ever shown what the forwarder
runs *out of* at high queue counts — only that it stops scaling.

#### Pre-registered: what each instrument can refute

Written before the runs, as §5.9 and `depth_prediction.md` were.

- **If DRAM bandwidth is the limit**, its utilisation rises with core count and
  flattens where Mpps flattens. At 93 Mpps with one 64 B line touched per packet
  this is only ~6 GB/s of compulsory read traffic against a 307 GB/s nominal
  ceiling — about 2% — so the honest prediction is that **DRAM bandwidth is not
  the limit**, and a measurement showing otherwise would mean the access pattern
  is moving far more than one line per packet. The find-mask defect (§5.26) is
  exactly such a mechanism, since every packet also writes, so the shipped build
  and the fixed build should differ here.
- **If fill buffers are the limit**, `FB_FULL/cycles` is high and the measured
  MLP sits at a hard ceiling regardless of queue depth — which would mean the
  depth-64 prefetch pipeline of §5.14 cannot actually keep 64 lines in flight,
  and would explain why its returns are exhausted by depth 32.
- **If the core is the limit**, top-down shows `retiring` high and `mem-bound`
  low. §5.14 measured 398 instructions/packet at IPC 3.96, which is close enough
  to the 6-wide allocation limit that this is a live possibility and would make
  instruction count — the flow hash of §5.26, at ~34 of ~98 ticks — the thing
  worth attacking.
- **If nothing is saturated**, the forwarder is offered-load-bound and the
  per-packet costs measured in this document are latency that is not being
  hidden rather than capacity that has run out. `RX-Missed` decides this one
  directly and needs no PMU at all.

These are not mutually exclusive and the interesting outcome is a crossover:
one resource limiting at low core counts and another taking over.

#### Repository additions

- `l2fwd/counter_groups.sh` — the event groups and the ceilings, sourced by both
  scripts below so that what is validated and what is measured cannot drift.
- `l2fwd/validate_counters.sh` — points every counter at a workload with a known
  answer before it is pointed at the forwarder.
- `l2fwd/bench_membw.c` — those workloads: a sequential stream (known bytes), a
  dependent pointer chase (MLP must be ~1), and a register-only chain (memory
  counters must read ~0).
- `l2fwd/saturation.sh` — one `l2fwd` run per event group per queue count.
- `l2fwd/analyse_saturation.py` — utilisations and `docs/saturation.svg`.

### 5.28 The saturation denominator: a runner written, and the one thing blocking it (2026-09-17)

`.dram_ceiling` carries `DRAM_ACHIEVED_GBS=` empty, and §5.27's whole table of
utilisations divides by it. Until it holds a measured number, every DRAM
utilisation in `docs/saturation.svg` is divided by a named lower bound, which
**overstates** saturation. This section records the attempt to fill it, which
did not succeed, and what stands in the way.

#### Why the existing figures could not be extended

`validate_counters.sh` measures the ceiling at four cores and says so
(`validate_counters.sh:164-168`): the agent shell is confined to cpus
24-27,52-55 by `user.slice`, and `taskset` cannot escape a cpuset. Four cores
was a limit, not a choice. The withdrawn `DRAM_ACHIEVED_GBS=360.0` was that
four-core mixed/read ratio applied to a peer session's 24-thread *read* ceiling,
resting on an untested assumption — that the ratio is independent of thread
count — and on a peer figure that had not plateaued (+18.3% from 16 threads).

The repair is not a better extrapolation but a measurement at the thread count
`l2fwd` actually runs at. Cores 0-23 are 24 *distinct physical* cores, since the
sibling of core `c` is `c+28`, so `bench.slice` can supply exactly that.

#### `l2fwd/dram_ceiling.sh`, and the two guards built into it

The method is inherited unchanged from `validate_counters.sh` — mixed rather
than read-only, differenced over 3 against 9 passes so the buffer memset and
first touch cancel, both arms from one binary on the same cores. Two things are
new, and both exist because of defects this document has already paid for:

- **It refuses to run on the wrong cores.** A probe confined to the
  housekeeping cpuset does not fail; it returns a plausible wrong number, and
  would report the same four-core figure at every thread count. The script reads
  its own `Cpus_allowed_list` and exits unless it is on bench cores.
- **It sweeps thread count rather than measuring only at the top, and marks its
  own result a lower bound.** If the mixed arm grows more than 5% over the last
  step of the sweep, the value written to `.dram_ceiling` is annotated
  `LOWER BOUND, still climbing`. That is precisely the defect the withdrawn
  360.0 had, and nothing on the page showed it. The provenance block it appends
  names the exact cpu list, and states that the new figure is **not** comparable
  to `READ_4`/`MIXED_4`, which came from the contended 24-27 set — the same set
  that produced the 178-tick reading of §5.26.

The constant is therefore written by the thing that derives it, which is the
failure `.dram_ceiling`'s own header is about.

#### The run is blocked on a session permission, and a committed script does not help

Attempted, and recorded because a negative result here saves the next attempt.
`bench_membw` was built, the bench lease taken (journal `2026-09-17T00:28:45`,
released `00:29:50` rather than held while blocked), and the run launched as:

```
benchctl run --cpus 0-23 --purpose "DRAM ceiling denominator (thread sweep)" \
  -- l2fwd/dram_ceiling.sh <outdir>
```

Denied by this session's harness permissions. The denial is on the
`sudo systemd-run --scope --slice=bench.slice` that **`benchctl` itself**
performs, not on the script being launched — so shipping the probe as a
committed script, which was the suggested alternative, does not route around
it. Since that launch is the only path onto cores 0-23, no committed script ever
can. It is a per-session harness permission and not a sudoers restriction, so
the fix is a decision rather than a systems change. The work is otherwise ready.

#### Machine-wide hazards in the *other* tree, and a question answered without touching the machine

Flagged by the scheduler session and verified here, recorded because the blast
radius reaches this project's runs even though the code does not belong to it.

The scripts concerned — `run_tiny.sh`, `setup.sh`, `setup_hbm.sh`,
`run_sweep_test.py`, `toggle_hyperthreading.sh` — are in
`/users/sohamb/DRAMHiT-migrate/DRAMHiT/scripts/`. **None of them exist in
NetBlast**, whose `scripts/` holds only `bind-dpdk-devices.sh`,
`constant_freq.sh`, `get-dpdk-ice.sh`, `prefetch_control.sh` and
`reserve_hugepages.sh`. The distinction is worth stating because a bare
`scripts/...` path in a two-project document sends a later reader to the wrong
repository, where finding nothing discredits the surrounding entries.

Two mechanisms mattered. `run_tiny.sh` offlines cpus 28-55 mid-run by writing
`/sys/devices/system/cpu/cpuN/online`, which deletes every SMT sibling of the
bench cores and changes the machine's cpu count under any affinity mask already
set, while other sessions are live. That alone justifies not running it. The
*restore* path was initially reported as broken on the grounds that a bare
invocation toggles rather than sets; reading it shows the toggle does restore in
the nominal sequence, because the state is 0 when it is reached. The remaining
doubt was the loop bound, `NPROC` from `lscpu`'s `CPU(s):` — if that counts
online rather than present cpus, the restore pass would iterate 0..27 and leave
28-55 offline permanently.

That question was settled by the peer session without offlining a real cpu, by
running `lscpu --sysroot` against a synthetic `/sys` tree first validated to
reproduce this host, then altered to the post-offline state: `CPU(s):` stayed at
56 with the offline set reported on its own line. `CPU(s):` is the **present**
count, so the restore pass does cover 28-55. Strong evidence rather than a live
test, and the method is worth reusing — a sysfs-reading script's behaviour under
a machine state you must not create can be tested against a synthetic sysroot.

#### Repository additions

- `l2fwd/dram_ceiling.sh` — the thread-count sweep, with the cpuset guard and
  the lower-bound self-marking described above.
- `l2fwd/plot_dram_ceiling.py` — achieved bandwidth against thread count, with
  the nominal 307.2 GB/s peak drawn for reference and the plateaued/still-
  climbing verdict rendered on the figure rather than left in the CSV. Carries
  the separate legend and the `<text>`-extent check against the viewBox that
  §5.22 made standard, both exercised end to end on synthetic input.

### 5.29 Verifying the 5 cycles per packet, by rebuilding the instrument (2026-09-17)

The floor arm of §5.20 reports **5 cycles per packet** for `-m none` at a
64-packet burst, and five cycles to write a destination MAC is low enough to be
worth checking rather than believing. This section is that check. It splits into
two questions that are answered differently: whether the *arithmetic* that
produces the number is right, which is settled by reading the code and the logs,
and whether the *instrument* that feeds it can resolve five cycles at all, which
needed a separate measurement.

The conclusion first: the arithmetic is correct, and 5 is if anything half a
tick to one and a half ticks **low**. Nothing on the page needs retracting.

#### The arithmetic: three things checked, three clean

`main.c:217-218` prints `total_hash_duration / total_packets_fwded`. Four ways
that quotient could be wrong, and what each one turned out to be:

- **Denominator deflation.** The numerator accumulates over every packet in the
  timed region, but the denominator is `fwded`, and the dramblast and maglev
  branches only increment `fwded` on a hit (`main.c:344`, `main.c:319`), while
  the `none` branch increments it for the whole burst (`main.c:357`). A dropping
  engine would therefore charge its drops to its hits and read high, and the
  95%/97% lookup shares of §5.20 would be inflated. **It does not happen here:**
  `Packets dropped` is exactly `0` in all thirty trio logs, and `Packets
  forwarded` tracks `Packets received` to within one burst. Checked directly in
  `/users/sohamb/sweeps/trio/*.log`, not assumed.
- **Ticks versus cycles.** Still sound: `set_clock.sh show` reports the machine
  in the pinned arm, `no_turbo=1` and every core at 2 100 000 kHz, and the trio
  logs record a delivered 2094-2095 MHz. §5.7's correction factor is 1.000.
- **The integer quotient.** Unchanged and load-bearing: a printed 5 is any true
  value in [5, 6). At the floor that is a 20%-wide bin, which is why everything
  below is reported as a ledger of biases in ticks rather than as a corrected
  number.
- **A cumulative mean paired with a steady-state one.** New, and the one real
  asymmetry. `total_hash_duration` and `total_packets_fwded` are both running
  totals from process start, so the printed figure is a cumulative mean that
  includes the warm-up — while `steady_mpps`, which the analysis divides
  alongside it, is deliberately the median of the samples *excluding* the cold
  first one (`extract_results.py:60-65`). The two are not taken over the same
  window. For `-m none` this moves nothing: the printed value is flat at 5 from
  the very first interval, because that arm has no table to warm. Where it bites
  is maglev, whose q=1 sequence decays `231 200 188 ... 167 166` over 31
  intervals; holding the recorded intervals fixed and solving for the tail gives
  a steady state of **163-165 against the 166 recorded**, about 1%. That is
  under one tick and changes no claim, but the pairing is an inconsistency in
  the method rather than noise, and it is recorded here so the next reader does
  not rediscover it as a discrepancy.

#### The instrument: rebuilt rather than re-examined

The stronger question is whether a pair of `rte_rdtsc()` reads can resolve a
five-cycle region at all. Two properties of `rte_rdtsc()` say it might not, and
neither is visible from the rig's output:

- It is plain `rdtsc` with no fence (`dpdk-21.11/include/rte_cycles.h`;
  `rte_rdtsc_precise()` is the fenced variant and is not what `main.c:309` calls).
  The disassembly of `l2fwd_main_loop` confirms it — bare `rdtsc` at `403bcf`
  and `403c9b`, no `lfence` on either side. A non-serialising closing read can
  retire while the loop's stores are still in the store buffer, so the region
  can read **less** than the work costs.
- Executing `rdtsc` twice is not free, and whatever an *empty* region reads is
  charged to every burst in every arm — divided by 64 at a full burst and by 2
  at the tail of the queue sweep.

§5.20 already put a number on the second one, `C = 32 ± 12` cycles per burst,
but it came from a fit with **R² = 0.45** over data whose small-burst end is
mostly rounding, and the section says so and calls it a bound. Rebuilding the
loop replaces that bound with a measurement.

`l2fwd/bench_timed_region.c` reproduces the `-m none` branch and times it with
the same unfenced pair, in floating point so truncation is out of the picture.
The reproduction is verified at the instruction level, not argued: the rig's
inner loop at `403dd0-403df5` and the benchmark's are the same eleven
instructions in the same order — two dependent loads off the mbuf, two reloads
of the source MAC, and stores of 8, 4 and 2 bytes. Two attempts were needed to
get there and both failures are worth recording, because each one silently
measured something cheaper than the target:

- A file-scope `static` MAC initialised in place was **constant-folded**, which
  removed both reloads and merged three stores into two.  Making the table a
  runtime-filled global restored the shape.
- Selecting the burst with `k % pool` *after* the opening timestamp charged a
  64-bit divider to the region under test — about 35 ticks per burst, which
  moved the headline from 3.5 to 4.1 cycles per packet. Hoisting the index above
  the opening read fixed it. This is the same class of defect as §5.13's: a
  plausible number, internally consistent, describing the harness.

#### What it measures

Every figure below is at a 64-packet burst, on an idle bench core via
`benchctl run --cpus 4`, median of 15 repetitions.

| | ticks/burst | cycles/packet |
|---|---|---|
| empty region, bare pair (the rig's instrument) | **29.95** | 0.47 |
| empty region, `lfence`-bracketed | 56.7 | 0.89 |

The floor is **29.95 ticks per burst**, stable to ±0.02 across every footprint,
stride and core it was run on. §5.20's `C = 32 ± 12` is confirmed, and can now
be stated as a measurement rather than a bound.

The loop itself, read through the rig's own unfenced instrument, depends
entirely on where the packets are:

| packet footprint | what has run out | cycles/packet |
|---|---|---|
| 0.1 MiB | nothing — L1/L2 resident | **3.47** |
| 2.8 MiB | past L2 | 7.04 |
| 8.4 MiB | past L2, at the L2 TLB's reach | 7.77 |
| 28.1 MiB | inside L3, past the L2 TLB | 22.02 |
| 84.4 MiB | past L3 | 21.98 |
| 421.9 MiB | past L3, DRAM + page walks | 25.48 |

**The rig's 5 falls inside that bracket**, above the issue-limited floor of the
same eleven instructions and far below a cache miss — which is where a
DDIO-fed forwarder belongs, since the NIC writes packet data into L3 before the
core reads it and the mbuf pool is recycled continuously. The number is not
implausible; it is where the mechanism predicts.

#### The bias ledger, in ticks

Three effects act on the printed 5 at a 64-packet burst, and the honest
statement is their sum rather than any one of them:

| effect | direction | size |
|---|---|---|
| integer truncation (`main.c:218`) | understates | −[0, 1) |
| instrument floor, 29.95/64 | overstates | +0.47 |
| stores hidden by the unfenced closing read, 58.7/64 | understates | −0.92 |

Working backwards, a printed 5 corresponds to a true region cost of about
**5.5 to 6.5 cycles per packet**. Every term is smaller than one tick, so the
claim "5 cycles per packet" survives at the resolution the rig actually has, and
no mechanism should be built on the difference — which is precisely the error
§5.13 made and §5.19 cleaned up after.

The store-shadow term is uniform across arms: it is a property of the timestamp
pair, not of the engine between them. So dramblast's 98 and maglev's 166 are
understated in the same way, and every *difference* taken on this page — which
is every comparison that matters — is untouched.

#### What was not changed, and why

`main.c:218` was left emitting an integer. Printing a float would remove the
truncation term outright, but it changes the binary that the whole of
`results_reproduced.json` was taken with, and re-baselining costs a full sweep
against a generator that is currently unusable (§5.28). The truncation is
bounded, signed and now accounted for, which is cheaper than a re-baseline. The
diff is one line if it is ever worth the cost:

```c
-    printf("\nCycle per fwd packet: %lu",
-           total_hash_duration / total_packets_fwded);
+    printf("\nCycle per fwd packet: %.3f",
+           (double)total_hash_duration / total_packets_fwded);
```

#### Repository additions

- `l2fwd/bench_timed_region.c` — the `-m none` branch reproduced
  instruction-for-instruction and timed with both a bare and an `lfence`-bracketed
  `rdtsc` pair, across burst sizes and packet footprints. `--pool` sizes the
  packet working set; `--csv` appends so one file holds every arm.
- `l2fwd/plot_timed_region.py` — `docs/timed_region.svg`: cycles per packet
  against burst size for each footprint, with the instrument floor drawn as
  `floor/b` and the rig's own (64, 5) marked, so the bracket is visible rather
  than tabulated. Carries the separate legend and the `<text>`-extent check
  §5.22 made standard.

### 5.30 The DRAM ceiling, measured at last: 184.2 GB/s, and why 360.0 was wrong in method (2026-09-17)

§5.28 built `l2fwd/dram_ceiling.sh`, could not run it, and recorded the refusal
rather than the result. The run has now happened. This section records what it
found, which is both a number and a reason.

#### What unblocked it

Nothing in this repository. §5.28's account of the blocker — a denial on the
`sudo systemd-run --scope --slice=bench.slice` that `benchctl` itself performs —
describes a `benchctl` that no longer exists. It was replaced with a narrower
operation that writes its own pid to `/sys/fs/cgroup/bench.slice/cgroup.procs`
and `exec`s. §5.28's diagnosis was correct when written and is now stale; it is
left in place, as retractions on this page are, because the reasoning about why a
committed script could not route around a per-session permission is still sound
and would apply again.

One practical note for the next person: `benchctl` resolves the caller's identity
from the tmux session name, so a run launched from a session named for the job
(`dramceiling`) is refused against a lease held by `nb`. `BENCH_SESSION=nb` is the
fix, and the tool's own error message says so.

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

`DRAM_ACHIEVED_GBS=184.2`, written by the script that measured it, with the cpu
list, the method and the plateau verdict appended beside it. The guard at
`dram_ceiling.sh:78` reported `0-23`, so this is 24 distinct physical cores and
not the housekeeping set wearing a 24-core label.

**It plateaued.** 4.0% growth over the last step, under the script's 5%
threshold, so the figure is a measured ceiling rather than a lower bound — which
is the defect the withdrawn 360.0 carried and the reason the sweep exists.

#### Why the withdrawn 360.0 was wrong, and it is not the reason that was given

The withdrawn value was the peer session's 24-thread READ ceiling scaled by a
mixed/read ratio of 1.582 measured at **four** cores. It was retracted on the
grounds that the ratio was assumed constant with thread count and untested, and
that the peer's figure had not plateaued. Both concerns were right to raise. The
sweep shows the first one is not merely untested but false, and false in the
strong sense that the ratio **inverts**:

| threads | read | mixed | mixed/read |
|---|---|---|---|
| 4 | 62.2 | 97.7 | **1.57** |
| 24 | 228.3 | 184.2 | **0.81** |

At four cores a mixed stream beats a read-only one; by twenty-four, read
overtakes mixed decisively. So the extrapolation was not an imprecise estimate of
the right quantity, it was the wrong operation. 360.0 against a true 184.2 is
nearly 2x high.

**The peer's read number was right all along.** Our independently measured
24-thread read arm is **228.3** against the peer's `PEER_READ_24=227.53` — 0.3%
apart, different session, different probe invocation. Their non-plateau
reproduces too: we read +16.7% from 16 to 24 threads on the read arm against
their +18.3%. The READ arm is genuinely still climbing. The MIXED arm, which is
the one that becomes the denominator, is not. Only the ratio assumption was
broken, and it is worth separating those, because "the peer's number was
unreliable" would have been the easy and wrong lesson to draw.

#### The direction of the error was backwards

`.dram_ceiling`'s header states that dividing by a lower bound **overstates**
utilisation. With the field empty, `analyse_saturation.dram_ceiling()` fell
through to preference 2, `PEER_READ_24=227.53`, labelled "a LOWER BOUND". But
227.53 is a *read* ceiling, and at 24 threads the read ceiling **exceeds** the
mixed one. It was therefore not a lower bound on the quantity actually wanted; it
was an over-estimate of it by 23.5%.

Every DRAM utilisation drawn against it is consequently **understated** by that
factor, not overstated. The caveat that travelled with the figures pointed the
wrong way, which is worse than no caveat, because a reader discounting in the
named direction moves further from the truth.

#### The hedge that was supposed to make this safe is not on the figure

The reassurance carried in `analyse_saturation.py`'s own docstring is that the
peer figure "is a lower bound and is named as such, so utilisations against it
read as 'at least'" (`:41`). If that were true on the page, every published
percentage would remain true as written, because the real denominator is smaller
and the real percentages are therefore larger.

It is not true on the page. The label is built by splitting `CEIL_SRC` at the
first comma and keeping only the head:

```python
            put("DRAM bandwidth (of %s)" % CEIL_SRC.split(",")[0], gbs / DRAM_CEIL)
```
<sub>`l2fwd/analyse_saturation.py:242`</sub>

`CEIL_SRC` is `"peer 24-thread READ ceiling, a LOWER BOUND"`. Everything after
the comma — which is the entire qualifier — is discarded. The rendered label in
`docs/saturation.svg` reads:

```
DRAM bandwidth (of peer 24-thread READ ceiling)
```

and the strings `least` and `lower bound` appear **zero** times anywhere in that
file. So the caveat exists in the source, in `.dram_ceiling`'s header and in this
document, and in none of the places a reader of the figure would look. It also
pointed the wrong way, per the subsection above. A qualifier that is absent from
the artefact and inverted where it is present is not a hedge.

Two further points against leaning on it. The percentages are rendered bare —
`"%7.1f%%"` at `:259` and `'%d%%'` at `:304` — with no "≥" or "at least" anywhere
in the formatting. And the surviving half of the label does at least say *READ
ceiling*, so a careful reader could in principle notice that a read ceiling is
the wrong denominator for a mixed workload; that is an inference available to an
expert, not a stated caveat.

The change is one line and is **not applied**, because it alters a figure whose
underlying data cannot currently be regenerated, and a relabelled plot drawn on
a stale denominator would be worse than an honestly stale one:

```diff
-            put("DRAM bandwidth (of %s)" % CEIL_SRC.split(",")[0], gbs / DRAM_CEIL)
+            put("DRAM bandwidth (of %s)" % CEIL_SRC, gbs / DRAM_CEIL)
```

With `DRAM_ACHIEVED_GBS=184.2` now present, `CEIL_SRC` becomes the short
`"measured mixed ceiling"` and carries no comma, so the split is harmless from
here on and the defect is self-closing for future runs. It is recorded because it
silently removed a caveat for every figure drawn before today.

**The shape of this defect is one this document has already paid for twice.** It
is not a missing caveat — it is a string operation that silently *ate* one, on a
field whose second clause was the entire safety argument. Compare the integer
quotient of "Measurement-harness defects" (§ at line 469): `total_hash_duration /
total_packets_fwded` with both operands `uint64_t`, truncating a quantity that is
not an integer and losing the fractional part with nothing downstream able to
notice. Both are apparatus quietly discarding the part that says how much to
trust the number, in a way no consumer can detect, because what arrives looks
exactly like a well-formed answer. The generalisation worth carrying: when a
value and its qualifier travel in one string or one expression, any operation
that narrows it is a candidate for having dropped the qualifier rather than the
value. (The same family produced the cancelled-alarm incident in the DRAMHiT
tree, which is that project's to record, not this one's.)

#### `docs/saturation.svg` is stale, and cannot be refreshed from disk

The new denominator is in place and `analyse_saturation.py` will now select it as
preference 1, "measured mixed ceiling". The committed figure does **not** reflect
it: it was drawn against 227.53. It cannot simply be re-plotted, because the raw
`.perf` and `.log` files from the saturation run are not in the tree and are not
in any scratchpad — regenerating the figure requires re-running `saturation.sh`,
which needs the box and a working generator, and node1's generator has been
unusable since ~21:45 on 2026-09-16. This is recorded rather than quietly fixed
so that nobody reads the percentages on that page as current.

#### Cross-checked against a sibling project's defect, and clear

The DRAMHiT session hit a harness bug worth checking for here before trusting
184.2: its perf greps matched `instructions:u`, and the `:u` suffix is one perf
emits only **outside** `bench.slice`. The probe therefore matched nothing in
precisely the environment it was written to run in — an instrument that behaves
differently depending on whether it is inside the cpuset, which is invisible to
anyone who tests outside a lease and then runs under one.

This sweep ran inside `bench.slice`, so the same failure would have applied.
Checked and clear: no `:u` or `:k` suffix appears in any perf-reading code in
this tree. `counter_groups.sh` specifies everything as raw encodings —
`cpu/event=0x48,umask=0x01,name=.../` for core events and
`uncore_imc_N/event=0x05,umask=0xcf/` for the CAS counters this ceiling is built
from (`:73-74`) — and raw encodings resolve identically inside and outside the
slice. The only `cycles:u` strings in the tree are in
`l2fwd/compare_userspace_ice.py`, where they are *data*: keys being looked up in
another project's result CSVs, not events being requested from perf here.

Two further reasons to trust the number rather than only the absence of this bug.
`counter_groups.sh` already carries the `<not supported>` guard (`:23-27`) —
counters written as a plain comma list report `<not supported>` at a run time of 0
while still claiming 100.00% enabled, which is the same "absent counter read as a
measured zero" trap this document has paid for before. And the read arm
reproduced an independent session's figure to 0.3%, which a dead counter cannot
do.

#### Repository additions

- `l2fwd/dram_ceiling_out/dram_ceiling.csv` — the full six-point sweep, both
  arms, with the core list per row, so the inversion above is checkable rather
  than asserted.
- `docs/dram_ceiling.svg` — achieved bandwidth against thread count, both arms,
  the nominal 307.2 GB/s peak drawn for reference and the plateau verdict
  rendered on the figure. Separate legend and the `<text>`-extent check
  (`plot_dram_ceiling.py:155`, which `sys.exit`s rather than warning).

### 5.31 Reading the saturation figure back, and the two resources that were never on the list (2026-09-17)

§5.30 filled `DRAM_ACHIEVED_GBS` and noted that `docs/saturation.svg` is stale
against it. The obvious next question is whether the staleness matters. Answering
it needs the plotted values, and the raw `.perf` files are gone — so they were
recovered from the figure's own geometry, by calibrating the y-axis against its
gridline labels (`y=365` is 0%, `y=65` is 100%) and mapping each `<polyline>`
back to its legend entry by stroke colour. Values below are therefore accurate to
roughly ±0.3 percentage points, which is far inside every margin they are used
for. This is a weaker source than the raw counters and is used only because the
raw counters no longer exist; the re-run supersedes it.

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

Applying §5.30's 1.235x correction moves DRAM bandwidth from 1.7-4.1% to
2.1-5.1%. No conclusion on the page turns on that. §5.27 pre-registered "the
honest prediction is that **DRAM bandwidth is not the limit**... about 2%", and
the measurement lands there. That prediction is confirmed rather than rescued by
the correction, which is the stronger of the two outcomes.

So the re-run is worth doing for the denominator, but not urgently, and nothing
published needs withdrawing on account of it. That is worth stating plainly,
because §5.30 establishes an error and it would be easy to leave the impression
that the saturation results are in doubt. They are not.

#### What the figure cannot explain, and it is the interesting part

At q=23 the core is **58.4% memory-bound** while every instrument that measures a
memory *resource* reads between 1% and 5%: DRAM bandwidth 4.1%, L3-miss stalls
1.2%, fill buffers 1.0%, page walker 1.0%. Those cannot simultaneously describe a
bandwidth or a capacity limit. The core is waiting on memory and nothing on
§5.27's list accounts for it.

This is also the first measurement bearing on the §1 collapse, which §5.27 noted
"no measurement has ever shown what the forwarder runs *out of*". The shape is
now visible: link rate climbs to 98.0% of line rate at q=8, then falls to 81.7%
and 12.8% as workers are added, while `retiring` collapses 60.2% -> 15.3% and
backend-bound rises 18.6% -> 73.1%. Throughput is lost to the core going
backend-bound, not to any shared resource reaching its ceiling.

#### Two resources from the original brief are absent from the enumeration

§5.27's table lists nine resources. The brief it was written against also named
**DRAM latency**, as a thing distinct from DRAM bandwidth, and **coherence
traffic between cores**. Neither appears in the table, and neither is
instrumented: `counter_groups.sh` and `analyse_saturation.py` contain no snoop,
no HITM, and no latency event of any kind.

They are also precisely the two candidates the q=23 signature points at. High
memory-bound with low bandwidth is what latency that is not being hidden looks
like; and a per-core cost that worsens as workers are added, with no shared
ceiling reached, is what inter-core interference looks like. §5.27's own opening
argument applies to itself here: *a bottleneck that was never on the list cannot
be found by refining the measurement of one that was.*

#### One instrument exists and is not on the figure

MLP is computed — `analyse_saturation.py:248-250` divides
`l1d_pend_miss_pending` by `l1d_pend_miss_pending_cycles` — but is reported as a
count rather than a utilisation, because it has no fixed ceiling, and so never
reached the plot. It is the single measurement that discriminates latency-bound
from bandwidth-bound: an MLP pinned at a hard ceiling while DRAM bandwidth sits
at 4% is a latency answer. Recovering it requires the same re-run.

#### What the next run should carry

Ordered so that one sweep answers the open question rather than re-confirming a
closed one:

1. Add DRAM-latency and coherence encodings to `counter_groups.sh`, and validate
   them against `bench_membw` first. Per §5.23, a raw encoding on this part is
   untrusted until checked against a workload with a known answer, because a
   generic alias can map to nothing and read zero.
2. Re-run `saturation.sh`. This picks up `DRAM_ACHIEVED_GBS=184.2`
   automatically, recovers MLP, and closes the `.split(",")[0]` label defect of
   §5.30 without a code change, since the new `CEIL_SRC` carries no comma.
3. Make the find-mask fix an arm of the same sweep. §5.27 pre-registered that the
   shipped and fixed builds should differ in DRAM traffic, and that comparison
   has never been run.

Steps 2 and 3 need the box and a working generator; node1's has been unusable
since ~21:45 on 2026-09-16. Step 1 needs neither.
