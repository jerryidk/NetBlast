#!/bin/bash
#
# Switch the machine between the two measurement arms, changing ONLY the core
# clock. Everything else that affects variability -- C-states, irqbalance,
# nmi_watchdog, THP defrag, the cpuset partition -- is deliberately left alone in
# both arms, so that pinned-vs-turbo is a single-variable contrast.
#
#   ./set_clock.sh pinned   ->  every core fixed at 2.100 GHz, turbo off
#   ./set_clock.sh turbo    ->  governor free to range 0.8-3.7 GHz, turbo on
#   ./set_clock.sh show     ->  report current state, change nothing
#
# Why 2.100 GHz specifically
# -------------------------
# It is simultaneously this SKU's `base_frequency` and exactly the invariant TSC
# rate (`rte_get_tsc_hz: 2100000000`). At that setting l2fwd's `rte_rdtsc()`
# deltas -- which it prints as "Full-loop cyc per fwd packet" but which are really
# TSC ticks, i.e. time -- become numerically equal to core cycles, so the
# tick-to-cycle correction is exactly 1.000 rather than an estimated ~1.74.
#
# Why both arms are needed
# ------------------------
# Pinning buys comparability and costs representativeness: production runs with
# turbo, so absolute numbers at 2.1 GHz describe a machine nobody deploys on
# (throughput drops from ~93 to ~56 Mpps). The turbo arm is the headline number
# and also supplies the per-queue-count delivered frequency r(q) needed to
# convert the pinned arm's ticks into the turbo arm's cycles. See
# docs/INVESTIGATION.md, "Pre-registered: what the pinned re-baseline must show".
#
# NOT persistent: every one of these is a runtime sysfs write and resets on
# reboot. See docs/INVESTIGATION.md section 3.4b.

set -u

MODE="${1:-show}"
PINNED_KHZ=2100000        # = base_frequency = TSC rate
MINF=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq)
MAXF=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq)

write_all() {   # write_all <sysfs leaf name> <value>
    for f in /sys/devices/system/cpu/cpu*/cpufreq/"$1"; do
        echo "$2" | sudo tee "$f" > /dev/null
    done
}

case "$MODE" in
  pinned)
    # max before min: the kernel rejects a min above the current max, so raising
    # min first on a core whose max is still low would silently fail on that core
    # and leave the machine half-configured -- which reads as "it worked".
    write_all scaling_max_freq "$PINNED_KHZ"
    write_all scaling_min_freq "$PINNED_KHZ"
    echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo > /dev/null
    ;;
  turbo)
    # min before max here, for the mirror-image reason.
    write_all scaling_min_freq "$MINF"
    write_all scaling_max_freq "$MAXF"
    echo 0 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo > /dev/null
    ;;
  show) ;;
  *) echo "usage: $0 {pinned|turbo|show}" >&2; exit 1 ;;
esac

# Verify by reading back EVERY core, not cpu0. A partial write is the failure
# mode that matters: it leaves a subset of cores at a different clock, which
# perturbs the sweep without failing anything.
NMAX=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq | sort -u | tr '\n' ' ')
NMIN=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq | sort -u | tr '\n' ' ')
echo "mode=$MODE  no_turbo=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)"
echo "  distinct scaling_max across all cores: $NMAX"
echo "  distinct scaling_min across all cores: $NMIN"
[ "$(echo "$NMAX" | wc -w)" -eq 1 ] || echo "  WARNING: cores disagree on scaling_max_freq"
[ "$(echo "$NMIN" | wc -w)" -eq 1 ] || echo "  WARNING: cores disagree on scaling_min_freq"

# sysfs reports the P-state *request*, never delivery: an idle core here reads
# 3.63-3.70 GHz while perf counts it at 506 kcycles/s. So confirm with perf.
echo "  (delivered frequency is NOT readable from sysfs on this box -- sweep.sh"
echo "   measures it per run with 'perf stat -e cycles'; see INVESTIGATION.md)"
