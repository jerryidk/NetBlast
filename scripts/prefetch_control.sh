#!/usr/bin/env bash

# 1. Require root privileges upfront
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)"
   exit 1
fi

# 2. Auto-install msr-tools on Ubuntu/Debian if missing
if ! command -v wrmsr &> /dev/null || ! command -v rdmsr &> /dev/null; then
    echo "msr-tools not found. Attempting to install on Ubuntu..."
    # -yq makes it quiet and non-interactive so it doesn't prompt you
    apt-get update -yq
    apt-get install -yq msr-tools

    # Verify installation succeeded
    if ! command -v wrmsr &> /dev/null; then
        echo "Error: Failed to install msr-tools. Please install manually."
        exit 1
    fi
    echo "Successfully installed msr-tools."
fi

# 3. Ensure the MSR module is loaded
if ! lsmod | grep -q '^msr '; then
  echo "Loading msr kernel module..."
  modprobe msr || { echo "Failed to load msr module"; exit 1; }
fi

# 4. Verify arguments
if [ $# -lt 1 ]; then
  echo "Usage: $0 <on|off|status>"
  echo "  on     -> Enables all prefetchers (0x0)"
  echo "  off    -> Disables all prefetchers (0xf)"
  echo "  status -> Reads current MSR 0x1a4 value on core 0"
  exit 1
fi

# MSR 0x1a4 Bit Definitions:
# Bit 0: L2 Hardware Prefetcher
# Bit 1: L2 Adjacent Cache Line Prefetcher
# Bit 2: DCU Hardware Prefetcher
# Bit 3: DCU IP Prefetcher

# 5. Execute the requested action
case "$1" in
    on)
        echo "Turning ON all prefetchers..."
        wrmsr -a 0x1a4 0x0
        ;;
    off)
        echo "Turning OFF all prefetchers..."
        wrmsr -a 0x1a4 0xf
        ;;
    status)
        echo "Current MSR 0x1a4 status (Core 0):"
        ;;
    *)
        echo "Invalid argument. Use 'on', 'off', or 'status'."
        exit 1
        ;;
esac

# Verify by reading core 0 (reading all cores clutters the terminal)
printf "Value: 0x%x\n" $(rdmsr -p 0 0x1a4)
