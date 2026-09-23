# l2fwd-maglev

An implementation of the Maglev load balancer integrated into DPDK l2fwd.

## Quickstart

## build
nix develop -c bash build.sh

## set up machine

1. discover network devices pci addr, `lspci | grep -i net`
2. make sure network devices is using dpdk compatible driver `dpdk-devbind.py`
3. enable hugepages `enable_hugepages.sh` 

## run

```
sudo ./build/l2fwd -l0-1 -m100 -b0000:00:05.0  -- -p 0x3 --no-mac-updating -m dramblast -c 32
```

before `--` is EAL (DPDK) arguments.
after is application arguments.

### Measurement flags

Three application flags exist only to take experiments apart. Each is passed in
argv rather than in the environment, because the sweep harness launches through
`sudo systemd-run`, which strips the environment.

| flag | meaning |
|---|---|
| `-B <1g\|thp2m\|4k>` | page backing for the lookup table. Default is whatever the mode ships with: 1 GiB hugetlb for dramblast, transparent 2 MiB for maglev. Use this to compare the two engines on the *same* backing — as shipped they differ in address translation as well as algorithm. |
| `-A <n>` | how many `aligned_alloc`/`free` round trips each burst performs. `0` is as shipped (exactly one), `-1` hoists the buffer to a per-core allocation made once at start-up, and `n > 0` adds `n` extra pairs. Sweeping it prices a round trip on this machine instead of quoting one from a paper. |
| `-m none` | no lookup at all: the forwarding loop's third branch (`main.c`), which writes the destination MAC exactly as the two engines do and skips only the table. This is the floor every per-packet number is read against — without it, a cost attributed to an engine silently includes however much of the harness sits inside the timed region. Swept by the `trio` block alongside both engines. |
| `-Q <n>` | prefetch pipeline depth, i.e. the find queue's size. Power of two, at least 4. Default 64. A burst of `B` packets fills the pipeline `ceil(B/Q)` times, so shortening it separates per-fill cost from per-burst cost — at the default they are the same event and cannot be told apart. |

| `-P <alpha>` | dramblast only. Prefill table to load factor alpha (0, 0.95] with filler keys before forwarding; traffic unchanged. Prints measured `dramblast table prefill/exit` lines. |
| `-S <K>[p]`, `-D <prefix>` | probe build only (`build-probe/`, `meson setup build-probe -Dnbprobe=true`). Per-burst phase timer every K-th poll, `p` adds six PMCs; `-D` dumps ring to `<prefix>_l<lcore>.nbp`. Shipped binary refuses both. See `libsashstore/nbprobe.h`, `docs/REFLECT_PATH.md`. |

Profiling tools: `nix develop ..#profile` (perf 7.2, pcm, pahole, llvm-mca, bpftrace, xed,
likwid). Tools only; l2fwd still builds from default shell. `build-ptw/`
(`-Dnbprobe=true -Dnbptw=true`) adds per-packet ptwrite marks for Intel PT.

## Measurement harness

Everything that produced a number lives here, not in scratch dir. Two files:
`harness.sh` runs things (shell), `analysis.py` reads results (Python).

```
./harness.sh sweep <outdir> <tag> <mode>  # one ten-queue sweep; QUEUES=... for subset
./harness.sh matrix [block ...]           # experiment matrix: control crossover
                                          #   alloc depth repeat depthrep trio
./harness.sh saturation <outdir> <tag> [mode]   # which resource runs out first
./harness.sh validate [cpu] [bench_membw]       # prove PMU counters (clobbers .dram_ceiling!)
./harness.sh ceiling <outdir> [--hk]            # DRAM ceiling -> .dram_ceiling
./harness.sh clock {pinned|turbo|show}          # core clock arms
./harness.sh codegen [binary]             # alloc/free pairs and prefetches still exist
./harness.sh pagewatch & python3 analysis.py backing <log>   # backing each run ACTUALLY got

python3 analysis.py extract /users/sohamb/sweeps ../docs/results_reproduced.json \
        /users/sohamb/sweeps/*.out        # logs -> docs/results_reproduced.json
python3 analysis.py fit [--plot]          # P + C/B burst model
python3 analysis.py matrix [--plot]       # every result, with its own falsification test
python3 analysis.py plot-matrix           # three-panel figure, no dependencies
python3 analysis.py report                # docs/report.html
python3 analysis.py plot-sweep            # queue-sweep PNGs (matplotlib, nix shell)
python3 analysis.py saturation <outdir>   # docs/saturation.svg
python3 analysis.py plot-latency <outdir> # docs/latency_vs_bandwidth.svg
python3 analysis.py plot-ceiling <csv>    # docs/dram_ceiling.svg
python3 analysis.py selftest [--real]     # fire perf multiplexing guard
python3 analysis.py probe <out.json> <log ...>   # nbprobe logs + ring dumps -> per-phase table
python3 analysis.py ptw <perf-script.txt ...>    # PT ptwrite trace -> per-packet timeline
```

`harness.sh sweep` knobs added 2026-09-22: `L2FWD_BIN` (default `./build/l2fwd`; probe arms
use `./build-probe/l2fwd`), `SAMPLE_AFTER` (wait for `Link UP`, then N s, before perf: for
`-P` runs whose init is 5-30 s). Row now ends `bin=<binary>`.

`./harness.sh` or `python3 analysis.py` alone prints sub list. Each sub takes
same args, env knobs, output paths as old script it replaced.

`analysis.py` imports no plotting library at module scope: SVG subs must run
from plain shell, outside nix dev shell. matplotlib subs import it lazily.

`bench_membw.c` stays own file: `harness.sh ceiling`/`validate` build and run it.

`archive/` holds one-off investigation tools whose question is answered (mask-fix
sweep, dramblast arms, timed-region and flowhash benches, clock arms, report
number checks, userspace-ice comparison). Kept runnable, not maintained. See
`archive/README.md`.

### Old name -> new name (2026-09-22)

`docs/INVESTIGATION.md` and commit messages cite old names.

| old | new |
|---|---|
| `sweep.sh` | `harness.sh sweep` |
| `run_matrix.sh` | `harness.sh matrix` |
| `saturation.sh` | `harness.sh saturation` |
| `validate_counters.sh` | `harness.sh validate` |
| `dram_ceiling.sh` | `harness.sh ceiling` |
| `set_clock.sh` | `harness.sh clock` |
| `page_watch.sh` | `harness.sh pagewatch` |
| `check_codegen.sh` | `harness.sh codegen` |
| `counter_groups.sh` | `counter_groups()` in `harness.sh` |
| `extract_results.py` | `analysis.py extract` |
| `fit_burst_model.py` | `analysis.py fit` |
| `analyse_matrix.py` | `analysis.py matrix` |
| `make_report.py` | `analysis.py report` |
| `plot_matrix.py` | `analysis.py plot-matrix` |
| `plot_sweep.py` | `analysis.py plot-sweep` |
| `analyse_saturation.py` | `analysis.py saturation` |
| `plot_latency_saturation.py` | `analysis.py plot-latency` |
| `plot_dram_ceiling.py` | `analysis.py plot-ceiling` |
| `verify_backing.py` | `analysis.py backing` |
| `test_perf_guard.py` | `analysis.py selftest` |
| `plotlib.py`, `perf_csv.py` | shared sections of `analysis.py` |
| `run_maskfix_sweep.sh`, `analyse_maskfix_sweep.py`, `check_dramblast_arms.sh`, `bench_dramblast_path.c`, `test_dramblast_find.c`, `plot_dramblast_arms.py`, `bench_timed_region.c`, `plot_timed_region.py`, `bench_flowhash.c`, `plot_clock_arms.py`, `check_arms.py`, `check_report_numbers.py`, `compare_userspace_ice.py` | `archive/` |

Two cautions learned the hard way, both written up in `docs/INVESTIGATION.md`:

- Serving N queues needs **N+1** lcores — one worker each plus the main lcore.
  Giving N makes DPDK exit with "Not enough cores", and the run then reports a
  floor rather than a measurement.
- `analysis.py backing` must be run *after* `harness.sh pagewatch` has actually sampled
  the arms in question. It fails loudly on an arm it has no samples for, because
  it used to pass silently on one.

## Other Resources

DPDK C: https://github.com/mars-research/l2fwd-maglev

ixy.rs generator: https://github.com/mars-research/ixy.rs.mempunch/blob/master/examples/maglevgen.rs
(specific # of flows)

pktgen: https://github.com/mars-research/ixy.rs.mempunch/blob/redleaf/pktgen-config.txt
(infinite # of flows)

RedLeaf: domains/lib/libbenchnet/maglev.rs

Old Rust: https://github.com/mars-research/maglev-demo
