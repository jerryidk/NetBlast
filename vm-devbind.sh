

# load vm driver
sudo modprobe uio_pci_generic

# need to make eth1 maps to 6.0
# and eth2 maps to 7.0
sudo ip link set eth1 down
sudo ip link set eth2 down

# bind those devices
sudo $(which dpdk-devbind.py) --bind=uio_pci_generic 0000:00:06.0 0000:00:07.0

#check
dpdk-devbind.py --status

