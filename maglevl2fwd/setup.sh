#!/bin/bash

# Check if correct number of arguments are provided
if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <driver_name> <eth_interface1:pci_addr1> [<eth_interface2:pci_addr2> ...]"
    echo "Example: $0 vfio-pci eth1:0000:01:00.0 eth2:0000:01:00.1"
    exit 1
fi

DRIVER_NAME=$1
shift # Shift arguments so $@ only contains the interface:pci pairs

# Ensure the script is run with sudo/root privileges
if [ "$EUID" -ne 0 ]; then
    echo "Please run this script with sudo or as root."
    exit 1
fi

# 1. Load the requested VM/DPDK driver
echo "--> Loading driver: $DRIVER_NAME..."
sudo modprobe "$DRIVER_NAME" 2>/dev/null || {
    # Some drivers like vfio-pci might need explicit binding or are already built-in
    echo "    Note: modprobe $DRIVER_NAME failed or driver already loaded. Proceeding..."
}

# Arrays to hold processed interfaces and PCI addresses
interfaces=()
pci_addrs=()

# Parse the eth_interface:pci_addr pairs
for pair in "$@"; do
    if [[ "$pair" != *":"* ]]; then
        echo "Error: Invalid format '$pair'. Must be eth_interface:pci_addr"
        exit 1
    fi
    IFS=":" read -r eth_if pci_addr <<< "$pair"
    interfaces+=("$eth_if")
    pci_addrs+=("$pci_addr")
done

# 2. Bring down the ethernet interfaces
echo "--> Bringing down network interfaces..."
for eth_if in "${interfaces[@]}"; do
    if ip link show "$eth_if" > /dev/null 2>&1; then
        echo "    sudo ip link set $eth_if down"
        sudo ip link set "$eth_if" down
    else
        echo "    Warning: Interface $eth_if not found or already unmanaged. Skipping 'ip link down'."
    fi
done

# 3. Bind devices to the new driver
echo "--> Binding devices to $DRIVER_NAME..."
DEVBIND_PATH=$(which dpdk-devbind.py 2>/dev/null)

if [ -z "$DEVBIND_PATH" ]; then
    echo "Error: dpdk-devbind.py not found in PATH. Please ensure DPDK utilities are installed."
    exit 1
fi

# Execute binding for all provided PCI addresses at once
echo "    sudo $DEVBIND_PATH --bind=$DRIVER_NAME ${pci_addrs[*]}"
sudo "$DEVBIND_PATH" --bind="$DRIVER_NAME" "${pci_addrs[@]}"

# 4. Check Status
echo -e "\n--> Checking DPDK status:"
"$DEVBIND_PATH" --status Network
