# archive

One-off investigation tools. Question each one asked is answered and written
up in `docs/INVESTIGATION.md`. Kept so result can be re-derived, not maintained.

Paths fixed so they still run from here (`../analysis.py` for shared plot
helpers, `../build`, `../libsashstore`, `../opt`, `../harness.sh sweep`).
Last commit with them at old `l2fwd/` paths: `98ea20e`.

| file | what | INVESTIGATION.md |
|---|---|---|
| `run_maskfix_sweep.sh`, `analyse_maskfix_sweep.py` | shipped vs find-mask-fix binaries, interleaved | §5.26 |
| `check_dramblast_arms.sh`, `bench_dramblast_path.c`, `test_dramblast_find.c`, `plot_dramblast_arms.py` | dramblast find-path arms (hoist, prefetch hint, vec spill) off-NIC | §5.26 |
| `bench_timed_region.c`, `plot_timed_region.py` | cost of timed region vs footprint | §5.29 |
| `bench_flowhash.c` | FNV vs CRC32C flow hash, throughput and latency | §5.26 |
| `plot_clock_arms.py` | pinned vs turbo arms figure | §3.5, §5.3 |
| `check_arms.py` | per-arm burst-model fit check | §3.5, §5.7 |
| `check_report_numbers.py` | every literal in report.html still matches data | §5.25 |
| `compare_userspace_ice.py` | NetBlast vs userspace-ice whole-loop cost; constants from old instrument | `docs/NETBLAST_VS_USERSPACE_ICE.md` |
