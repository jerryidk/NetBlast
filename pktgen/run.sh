#!/bin/bash
#
# Run the packet generator in the 2-queue-pair configuration (2 TX + 2 RX),
# pinned to a fixed set of CPUs at the TOP of the range, so it stays put and
# leaves the low-numbered cores free for the forwarder under test.
#
# This is the configuration the committed figures in docs/ were measured with.
#
# Why not literally two CPUs
# --------------------------
# pktgen.c:308-311 computes `nb_workers = rte_lcore_count() - 1` and refuses to
# start with fewer than two workers, so the floor is 3 lcores: 1 main + 1 TX +
# 1 RX. Two CPUs cannot run this program without a source change.
#
# Why 5 lcores by default
# -----------------------
# Workers are split in half, TX then RX (pktgen.c:335). At 3 lcores that is a
# single TX core, which tops out at ~72 Mpps -- below the 93.28 Mpps line rate
# of this 100 GbE link, so the forwarder would never be fully loaded. 5 lcores
# gives 2 TX + 2 RX, which reaches line rate. Measured: 2 TX cores and 8 TX
# cores produce identical results (worst deviation 1.1%), so 2 is enough and
# more only loads the generator host.
#
# How the pinning is made to stick
# --------------------------------
#   -l        DPDK pins each lcore thread to exactly one CPU.
#   taskset   restricts the whole process affinity mask, so DPDK's non-lcore
#             control threads (interrupt, telemetry) also cannot drift onto
#             CPUs outside the set. Without this, -l pins the workers but the
#             control threads inherit the full mask.
#
# Flow count
# ----------
# -r is PER TX CORE, not total: pktgen.c:419 computes
# `max_capable_flows = (g_ip_mask + 1) * total_tx_queues`, and each TX core
# bases its addresses at RTE_IPV4(10, lcore_id, 0, 0) (pktgen.c:81). It is
# therefore scaled below to hold total flows at (1<<20)*16 = 16777216 -- the
# value docs/data.txt documents -- whatever the TX core count is.
#
# Usage:  ./run.sh                 # 2 queue pairs on the last 5 CPUs (default)
#         NCORES=3 ./run.sh        # 1 queue pair, ~72 Mpps -- below line rate
#         PCI=17:00.1 ./run.sh     # different port

set -euo pipefail

NCORES=${NCORES:-5}
PCI=${PCI:-17:00.0}
PAYLOAD=${PAYLOAD:-68}
TOTAL_FLOWS=${TOTAL_FLOWS:-16777216}   # (1<<20)*16, must be a power of 2

if [ "$NCORES" -lt 3 ]; then
    echo "Error: NCORES must be >= 3 (1 main + 1 TX + 1 RX)." >&2
    echo "       pktgen.c:310 rejects fewer than 2 worker cores." >&2
    exit 1
fi

NUM_CPUS=$(nproc --all)   # --all: ignore any inherited affinity mask
FIRST=$(( NUM_CPUS - NCORES ))
LAST=$(( NUM_CPUS - 1 ))
CORE_LIST="${FIRST}-${LAST}"

# Workers are split in half, TX first (pktgen.c:335).
WORKERS=$(( NCORES - 1 ))
TX_CORES=$(( WORKERS / 2 ))
RANGE_PER_CORE=$(( TOTAL_FLOWS / TX_CORES ))

if (( RANGE_PER_CORE & (RANGE_PER_CORE - 1) )); then
    echo "Error: TOTAL_FLOWS/$TX_CORES = $RANGE_PER_CORE is not a power of 2;" >&2
    echo "       pktgen.c:275 rejects it. Pick a different NCORES or TOTAL_FLOWS." >&2
    exit 1
fi

echo "CPUs ${CORE_LIST} (of 0-${LAST}): 1 main + ${TX_CORES} TX + $(( WORKERS - TX_CORES )) RX"
echo "flows: ${TX_CORES} x ${RANGE_PER_CORE} = ${TOTAL_FLOWS}"

set -x
sudo taskset -c "${CORE_LIST}" ./my_pktgen \
    -l "${CORE_LIST}" \
    -a "${PCI}" \
    -- \
    -r "${RANGE_PER_CORE}" \
    -p "${PAYLOAD}"
