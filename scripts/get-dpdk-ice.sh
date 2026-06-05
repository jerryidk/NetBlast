#!/bin/bash

TARGET_DIR="/lib/firmware/intel/ice/ddp/"

# Navigate to the Intel ICE DDP firmware directory
pushd "$TARGET_DIR" > /dev/null || { echo "Error: Directory $TARGET_DIR not found!"; exit 1; }

# Enable nullglob so the array is empty if no files match
shopt -s nullglob
ZST_FILES=(ice-*.pkg.zst)
shopt -u nullglob

# Check if we found exactly one, none, or multiple packages
if [ ${#ZST_FILES[@]} -eq 0 ]; then
    echo "Error: No ice-*.pkg.zst files found in $TARGET_DIR."
    popd > /dev/null
    exit 1
elif [ ${#ZST_FILES[@]} -gt 1 ]; then
    echo "Warning: Multiple ice packages found. Selecting the first one: ${ZST_FILES[0]}"
    PKG_ZST="${ZST_FILES[0]}"
else
    PKG_ZST="${ZST_FILES[0]}"
fi

echo "=== Found DDP package: $PKG_ZST ==="

# Extract the base name by stripping the '.zst' extension
PKG_NAME="${PKG_ZST%.zst}"

# Unpack the dynamically found DDP package (-f forces overwrite)
echo "=== Unpacking $PKG_ZST ==="
sudo unzstd -f "$PKG_ZST"

# Link the unpacked file to ice.pkg so DPDK can find it
echo "=== Creating symbolic link ==="
sudo ln -sf "$PKG_NAME" ice.pkg

# Verify the symbolic link was updated successfully
echo "=== Verification ==="
ls -l ice.pkg

# Return to the original directory
popd > /dev/null
