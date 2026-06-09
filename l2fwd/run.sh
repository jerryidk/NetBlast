#!/bin/bash

# 1. Verify arguments
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <number_of_cores> <mode>"
    echo "Example: $0 4 dramblast"
    exit 1
fi

NUM_CORES=$1
MODE=$2

# 2. Validate that NUM_CORES is a positive integer
if ! [[ "$NUM_CORES" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: Number of cores must be a positive integer."
    exit 1
fi

# 3. Generate the even core ID list (e.g., 4 cores -> 0,2,4,6)
# Multiply (NUM_CORES - 1) by 2 to get the maximum core ID needed
MAX_CORE=$(( (NUM_CORES - 1) * 2 ))
# Use seq to generate a comma-separated list jumping by 2
CORE_LIST=$(seq -s, 0 2 $MAX_CORE)

# 4. Determine prefetcher status based on mode
if [ "$MODE" == "dramblast" ]; then
    PREFETCH_STATUS="off"
elif [ "$MODE" == "maglev" ]; then
    PREFETCH_STATUS="off"
else
    # Default fallback just in case
    PREFETCH_STATUS="off"
fi

# 5. Execute the sequence
echo "Running 8gb hashtable"

# Toggle prefetch control script
../scripts/prefetch_control.sh "$PREFETCH_STATUS"

set -x
# Run the command with dynamically assigned core list (-l) and queue count (-q)
sudo ./build/l2fwd \
    --in-memory \
    -l "${CORE_LIST}" \
    -m 2000 \
    -b 0000:00:05.0 \
    -- \
    -p 1 \
    -q "${NUM_CORES}" \
    -no-mac-updating \
    -m "${MODE}" \
    -c 536870912

set +x
