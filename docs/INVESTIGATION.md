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

**Consequence for the experiment design.** A plain hoist is now a *weak* test: if
the allocator is ~11% of `C`, removing it should move `C` by ~80 cycles, against
a 7% estimator spread of ~50. That is marginal. The **amplification arm** becomes
the primary measurement instead -- inject `N` extra `aligned_alloc`/`free` pairs
per burst and check that `C` rises by `N x ~60` cycles. At `N = 8` that is ~480
cycles, far above the noise floor, and it calibrates the allocator's true
per-burst contribution on this machine rather than relying on a literature value
for the tcache path. The hoist then becomes the confirmation, with the
amplification arm supplying the expected effect size.

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
   would move ~10% of it and could not distinguish "small" from "zero". §5.9
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

- **What the ~645 cycles of per-burst work actually are.** Known: it is 86%
  executed instructions rather than waiting (§5.6), it is resolved at sixteen
  standard errors, and maglev has no equivalent. The two candidates are the
  allocator round trip and the batching machinery itself; `-A` and `-Q` separate
  them and cannot mimic each other.
- **Why the refactored binary is 6% faster per packet** (§5.10). Behaviour is
  identical and the difference has the same sign at every queue count, so it is
  codegen; which change, exactly, has not been chased. It matters only as a
  reminder that a binary is a variable.
- **Whether the cross-core page-table contention seen on 4 KiB pages** (§5.12)
  is really last-level-cache pressure from the page table's own working set.
  The size and the page-size dependence both fit, but nothing has measured it
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
`644.5 / B < 163.4 − 95.2`, i.e. while **B > 9.4 packets**. Above that burst size
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
that the per-burst cost is unhidden DRAM latency at the start of a short burst
(a pipeline ramp), which predicted the opposite. What 551 cycles of executed work
per burst actually *is* remains open; §5.9 is the experiment that settles it.

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
1.0 for whatever this work is.

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
| dramblast, refactored | 87.7 | 717.6 ± 42 | **98.9 (−6.1%)** | 177.4 (+0.9%) |
| maglev, pre-refactor | 163.4 | not resolved | 162.7 | 157.8 |
| maglev, refactored | 163.0 | not resolved | 162.4 (−0.2%) | 158.5 (+0.4%) |

**maglev is unchanged**, which matters more than it looks: maglev's allocation
mechanism genuinely changed (`aligned_alloc(4096, 8 GiB)` became an explicit
`mmap` plus `MADV_HUGEPAGE`), and on this host `defrag=madvise`, so the new path
takes synchronous compaction where the old one did not. Measured, the new path
reaches 99.98% THP coverage against the old path's 99.3%. That is a real
difference in what the kernel did, and it is worth 0.2% of run time — so it can
be set aside, having been measured rather than argued away.

**dramblast is 6.1% cheaper at a full burst and unchanged at a short one.** The
per-burst coefficient moved by 1.2σ of the combined fit error, i.e. not at all;
the per-packet coefficient moved by 7.6 cycles. Behaviour is identical — the
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

Cost at q=1 — one worker, a full 64-packet burst — in core cycles per packet:

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

The comparison is made at **q=1**, where every arm sits at a full 64-packet
burst. This matters: adding allocator pairs makes the forwarder slower, which
makes it stay oversubscribed further up the sweep, which changes the burst sizes
it reaches — so the arms are not at comparable bursts anywhere else, and fitting
each arm over its own burst range compares conditions that differ in two things
at once. At a matched burst, the difference in cost per packet multiplied by 64
*is* the difference in cost per burst, with no model in between.

| pairs per burst | core cycles/packet | vs hoisted | × 64 = per burst | per pair |
|---|---|---|---|---|
| 0 (hoisted) | 90.8 | — | — | — |
| 1 (as shipped) | 97.8 | 7.0 | 447 | **447** |
| 3 | 114.7 | 23.9 | 1532 | 511 |
| 5 | 130.7 | 39.9 | 2554 | **511** |
| 9 | 162.6 | 71.8 | 4597 | **511** |

**One `aligned_alloc(64, …)`/`free` pair costs about 450 cycles — 215 ns.** The
shipped pair is 447 by this route and 462 by differencing the two fitted
per-burst coefficients, two estimates agreeing to 3%, the second at 10.6σ.

So the allocator is **60-64% of the per-burst cost**, against the "at most ~11%"
this document previously claimed. The remainder — the batching machinery itself
— is measured directly by the hoisted arm at **256 ± 13 cycles per burst**,
rather than extrapolated from a fitted intercept.

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
pair costs 511 cycles at matched burst and 365 from the weighted fit. The reason
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

**And the thing that was got wrong.** An earlier draft explained the gap between
the two estimators by arguing that the shipped pair must cost *more* than an
incremental one, because its `free` is separated from its `alloc` by the whole
batch while the amplification pairs are back-to-back in a loop. Measured at
matched burst, the shipped pair costs 447 cycles and an incremental one 511:
the shipped pair is **cheaper**, by 64 cycles. The argument was not merely
unsupported, it had the sign backwards. It is recorded here as measured, with no
replacement story, because inventing a second mechanism to explain the first
one's failure is how this section got into trouble in the first place.

