# Event groups for the saturation study, and the ceilings they are read against.
#
# Sourced by validate_counters.sh and saturation.sh so that the two cannot drift
# apart -- validating one set of encodings and then measuring with another is
# the failure this file exists to prevent.
#
# Why groups at all: docs/INVESTIGATION.md section 5.24 established that a
# multiplexed counter silently scales its result, so every group here is sized
# to fit the PMU at once and each run is checked for 100% enabled. The core
# groups are deliberately small for one further reason -- a peer session found
# that three simultaneous 0x48-family events collide in the scheduler, so
# L1D_PEND_MISS.PENDING/PENDING_CYCLES and FB_FULL are split across two groups
# rather than gathered into one.
#
# Raw encodings, not perf's generic aliases: this part (family 6, model 207,
# Emerald Rapids) has no JSON event file, so `perf list` shows only the
# architectural events and an alias may map to nothing and read zero.

# --- core: where the issue slots go (top-down level 1) ---------------------
#
# THE BRACES ARE LOAD-BEARING. The topdown-* events are derived from the
# PERF_METRICS MSR and must be programmed in one group with `slots` as the
# group leader. Written as a plain comma list they report `<not supported>`
# with a run time of 0 and an enabled percentage of 100.00 -- so a
# multiplexing check passes, nothing errors, and every top-down number is
# silently absent. Measured on this host: without braces, slots counts
# 21,979,161,492 and all four metrics read `<not supported>`; with braces the
# same workload reports 19.4% retiring / 0.0% bad-spec / 73.4% frontend /
# 7.3% backend.
G_TOPDOWN1="{slots,topdown-retiring,topdown-bad-spec,topdown-fe-bound,topdown-be-bound}"

# --- core: which backend resource (top-down level 2) ----------------------
G_TOPDOWN2="{slots,topdown-mem-bound,topdown-fetch-lat,topdown-heavy-ops,topdown-br-mispredict}"

# --- memory-level parallelism: how many misses are in flight --------------
# PENDING / PENDING_CYCLES = mean outstanding L1D misses while any is
# outstanding. A dependent pointer chase must give ~1.0; that is the known
# answer validate_counters.sh checks.
G_MLP="cycles,\
cpu/event=0x48,umask=0x01,name=l1d_pend_miss_pending/,\
cpu/event=0x48,umask=0x01,cmask=0x01,name=l1d_pend_miss_pending_cycles/"

# --- fill buffers: is the miss-handling structure itself the limit? -------
# FB_FULL counts cycles in which a request could not be issued because no fill
# buffer was free. As a fraction of cycles it is a saturation figure for the
# L1D's miss-handling capacity, which is what a software prefetch pipeline of
# depth 64 is most likely to run into.
G_FB="cycles,\
cpu/event=0x48,umask=0x02,name=l1d_pend_miss_fb_full/,\
cpu/event=0x20,umask=0x08,name=offcore_reqs_outstanding_data_rd/"

# --- address translation and last-level cache -----------------------------
G_TLBMEM="cycles,instructions,\
cpu/event=0x12,umask=0x0e,name=dtlb_walk_completed/,\
cpu/event=0x12,umask=0x10,name=dtlb_walk_active/,\
cpu/event=0xa3,umask=0x06,cmask=0x06,name=stalls_l3_miss/,\
LLC-load-misses"

# --- DRAM bandwidth (uncore, per socket; must be collected system-wide) ----
# A separate PMU from the core counters, so this does not compete with them for
# general-purpose counters and may be collected alongside.
#
# RAW ENCODINGS, and this one was nearly got wrong. The obvious spelling is
# perf's `uncore_imc_N/cas_count_read/` alias -- but there is no `events/`
# directory under /sys/bus/event_source/devices/uncore_imc_N at all on this
# part, only `format`, `alias`, `cpumask` and `type`, so that alias resolves to
# nothing. This is the same failure as the dTLB alias in section 5.23, in the
# one group that had been left aliased. CAS_COUNT.RD is event 0x05 umask 0xcf
# and CAS_COUNT.WR is umask 0xf0; each count is one 64 B cache line, so the
# analysis multiplies by 64 rather than relying on a .scale file that also does
# not exist here.
G_BW=$(for i in 0 1 2 3 4 5 6 7; do
         printf 'uncore_imc_%d/event=0x05,umask=0xcf,name=cas_rd_%d/,' $i $i
         printf 'uncore_imc_%d/event=0x05,umask=0xf0,name=cas_wr_%d/,' $i $i
       done | sed 's/,$//')
# Counts are cache lines, not bytes.
CAS_LINE_BYTES=64

# --- ceilings, measured or from the hardware configuration ----------------
# DRAM: 8 populated channels x DDR5-4800 x 8 B/transfer. Theoretical; the
# achievable figure is measured by validate_counters.sh and is the one the
# saturation study should divide by.
DRAM_PEAK_GBS=307.2
# NIC: PCIe Gen4 x16, 16 GT/s x 16 lanes x 128b/130b encoding, one direction.
PCIE_GBS=31.5
# Core: Golden Cove allocates 6 uops/cycle, so slots = 6 x cycles.
ISSUE_WIDTH=6
# Wire: 100 Gbps with 110 B frames plus 8 B preamble and 12 B inter-frame gap.
LINE_RATE_MPPS=96.15
