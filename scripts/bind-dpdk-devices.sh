#!/bin/bash

# Check if correct number of arguments are provided
if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <driver_name> <eth_interface1> [<eth_interface2> ...]"
    echo "Example: $0 vfio-pci eth1 eth2"
    exit 1
fi

DRIVER_NAME=$1
shift # Shift arguments so $@ only contains the interface names

# Locate the DPDK devbind script
# Checking for both dpdk-devbind.py and dpdk-binddev.py just in case
DEVBIND_PATH=$(which dpdk-devbind.py 2>/dev/null || which dpdk-binddev.py 2>/dev/null)

if [ -z "$DEVBIND_PATH" ]; then
    echo "Error: dpdk-devbind.py not found in PATH. Please ensure DPDK utilities are installed."
    exit 1
fi

# 1. Load the requested VM/DPDK driver
echo "--> Loading driver: $DRIVER_NAME..."
sudo modprobe "$DRIVER_NAME" 2>/dev/null || {
    # Some drivers like vfio-pci might need explicit binding or are already built-in
    echo "    Note: modprobe $DRIVER_NAME failed or driver already loaded. Proceeding..."
}

interfaces=("$@")
pci_addrs=()

# 2. Discover PCI addresses automatically
echo "--> Discovering PCI addresses..."
# Grab the current network status to parse
DPDK_STATUS=$(sudo "$DEVBIND_PATH" --status Network)

for eth_if in "${interfaces[@]}"; do
    # Use grep -w to match the exact interface name (e.g., prevents 'eth1' from matching 'eth10')
    # and awk to print the first column (the PCI address)
    pci_addr=$(echo "$DPDK_STATUS" | grep -w "if=$eth_if" | awk '{print $1}')

    if [ -z "$pci_addr" ]; then
        echo "    Error: Could not find PCI address for interface '$eth_if' in dpdk-devbind.py output."
        echo "    (Note: If the interface is already bound to DPDK, it may no longer show an 'if=' attribute)."
        exit 1
    fi

    pci_addrs+=("$pci_addr")
    echo "    Mapped $eth_if to PCI address $pci_addr"
done

# 3. Bring down the ethernet interfaces
echo "--> Bringing down network interfaces..."
for eth_if in "${interfaces[@]}"; do
    if ip link show "$eth_if" > /dev/null 2>&1; then
        echo "    sudo ip link set $eth_if down"
        sudo ip link set "$eth_if" down
    else
        echo "    Warning: Interface $eth_if not found or already unmanaged. Skipping 'ip link down'."
    fi
done

# 4. Bind devices to the new driver
echo "--> Binding devices to $DRIVER_NAME..."
echo "    sudo $DEVBIND_PATH --bind=$DRIVER_NAME ${pci_addrs[*]}"
sudo "$DEVBIND_PATH" --bind="$DRIVER_NAME" "${pci_addrs[@]}"

# 5. Check Status
echo -e "\n--> Checking DPDK status:"
sudo "$DEVBIND_PATH" --status Network
