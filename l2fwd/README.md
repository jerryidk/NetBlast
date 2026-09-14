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
| `-Q <n>` | prefetch pipeline depth, i.e. the find queue's size. Power of two, at least 4. Default 64. A burst of `B` packets fills the pipeline `ceil(B/Q)` times, so shortening it separates per-fill cost from per-burst cost — at the default they are the same event and cannot be told apart. |

## Measurement harness

Everything that produced a number lives here, not in a scratch directory.

```
./sweep.sh <outdir> <tag> <mode>     # one ten-queue sweep; QUEUES=... for a subset
./run_matrix.sh [block ...]          # the experiment matrix: control crossover
                                     #   alloc depth repeat depthrep
python3 extract_results.py /users/sohamb/sweeps ../docs/results_reproduced.json \
        /users/sohamb/sweeps/*.out   # logs -> docs/results_reproduced.json
python3 analyse_matrix.py [--plot]   # every result, with its own falsification test
python3 plot_matrix.py               # the three-panel figure, no dependencies
python3 make_report.py               # docs/report.html
bash check_codegen.sh                # the alloc/free pairs and prefetches still exist
./page_watch.sh & python3 verify_backing.py <log>   # the backing each run ACTUALLY got
```

Two cautions learned the hard way, both written up in `docs/INVESTIGATION.md`:

- Serving N queues needs **N+1** lcores — one worker each plus the main lcore.
  Giving N makes DPDK exit with "Not enough cores", and the run then reports a
  floor rather than a measurement.
- `verify_backing.py` must be run *after* `page_watch.sh` has actually sampled
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
