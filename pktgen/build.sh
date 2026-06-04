gcc -O3 pktgen.c $(pkg-config --cflags --libs libdpdk) -o my_pktgen
