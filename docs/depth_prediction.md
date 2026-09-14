# A per-fill ramp model for the prefetch pipeline, and its test

Written 2026-09-14 09:16 UTC, while the `d32` arm was running.

**Disclosure about what had been seen.** The model below was derived from the
`d8` and `d16` arms, which were complete. `d32` was in flight; its `q=1` point
(101 cycles/packet at burst 64) was on screen when the arithmetic was done, so
that single point is *not* out of sample. Its `q=2..5` points, which are the
replicates that actually resolve the prediction, had not been taken. The `d32`
arm is therefore a within-sample check on one point and an out-of-sample check
on four.

## The model

The burst-cost model `cycles/packet = P + C/B` was derived on the shipped build,
where the prefetch queue is 64 deep and the RX burst is at most 64. There, one
burst is exactly one fill and drain of the pipeline, so "per burst" and "per
pipeline fill" name the same event and the model cannot tell them apart.

Shortening the queue separates them. With queue depth `Q` and burst `B`, the
pipeline fills and drains `ceil(B/Q)` times per burst. So:

    cycles/packet  =  W  +  ramp * ceil(B/Q) / B  +  alloc / B

where `W` is steady-state per-packet work, `ramp` is the cost of one fill/drain,
and `alloc` is the 447-cycle `aligned_alloc`/`free` pair, which happens once per
burst at every depth because the buffer is sized by the burst length and not by
the queue depth (`l2fwd/libsashstore/dramblast.c:243`).

This immediately explains why the fit reports `P` rising and `C` falling as the
queue shortens, which looked like the two parameters trading places. They are
not trading: at `Q = 64` the ramp is paid once per burst and lands in `C`; at
`Q = 8` it is paid eight times per 64-packet burst and lands in `P`. The same
cycles move from one term to the other because the event they belong to changes
its rate.

It also explains why `d8`'s fitted `C` (375 +/- 49) sits below the 447-cycle
allocator floor without anything being wrong: at depth 8 the ramp has almost
entirely left `C`, so `C` is the allocator plus whatever other genuinely
per-burst work exists, and the fit's remaining freedom is absorbed by the
correlated `P`.

## Calibration

At burst 64, averaging the five queue counts that stayed there:

| depth | cycles/packet | excess over depth 64 | 1/Q - 1/64 |
|---|---|---|---|
| 8  | 119.0 | 18.6 | 0.109375 |
| 16 | 106.8 |  6.4 | 0.046875 |
| 64 | 100.4 |  —   | 0 |

A line through the origin gives **ramp = 165 cycles per pipeline fill**.

Two points, one parameter, so this is a calibration and not yet a test.

## The prediction

At burst 64, depth 32 should cost

    100.4 + 165 * (1/32 - 1/64)  =  103.0 cycles/packet

against 100.4 if depth does not matter at all at this burst. Per-point scatter
within an arm is 1-2 cycles, so the mean of the five burst-64 points has a
standard error near 0.5 and the two hypotheses are about five sigma apart.

**Falsified if** the depth-32 mean at burst 64 comes out at or below 101.4, i.e.
indistinguishable from no effect, or above 104.6.

## Why this is not the whole story

The ramp explains the *cycles*. It does not explain the instruction counts,
which at burst 64 are 397.9 per packet at depth 64, 411.8 at depth 16 and 413.9
at depth 8. A per-fill instruction overhead would make depth 8's excess twice
depth 16's; instead the two are nearly equal (+16.0 and +13.9), which looks like
a fixed step taken as soon as the queue is smaller than the burst rather than
anything proportional to the number of fills. That step is not explained here.

The cycles-versus-instructions split is the load-bearing part of the depth
result and does not depend on the model at all. From depth 64 to depth 8 at a
matched burst of 64, instructions per packet rise **4.0%** while cycles per
packet rise **18.5%**, so IPC falls from 3.96 to 3.48. A shallower prefetch
pipeline does not make the forwarder do meaningfully more work. It makes it
wait.

---

## Outcome (2026-09-14, after the arm completed)

    predicted   103.0        null (no effect)   100.4
    measured    102.2 +/- 0.58  (mean of five burst-64 runs)

1.3 sigma from the prediction, 3.1 sigma from the null. Not falsified.

The ramp is confirmed by a second, independent route as well: fitting
`W + ramp*ceil(B/Q)/B + K/B` to all forty points from the four depth arms at
once gives `ramp = 166 +/- 25`, against the 165 calibrated here from two points
at a single burst. The two routes share no algebra.

The instruction-count anomaly noted above stands unexplained. Depth 32 gives
405.0 instructions per packet, between depth 64's 397.8 and depth 16's 411.9, so
the step-at-first-shortening reading is wrong too — the instruction count rises
smoothly with the number of fills while the *excess* is not proportional to it.
No account of that is offered here.
