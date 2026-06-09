# l2fwd-maglev

An implementation of the Maglev load balancer integrated into DPDK l2fwd.

## Quickstart

## build
nix develop -c bash build.sh

## set up machine

1. discover network devices pci addr, `lspci | grep -i net`
2. make sure network devices is using dpdk compatible driver `dpdk-devbind.py`
3. enable hugepages `enable_hugepages.sh` 

## run

```
sudo ./build/l2fwd -l0-1 -m100 -b0000:00:05.0  -- -p 0x3 --no-mac-updating -m dramblast -c 32
```

before `--` is EAL (DPDK) arguments.
after is application arguments.

## Other Resources

DPDK C: https://github.com/mars-research/l2fwd-maglev

ixy.rs generator: https://github.com/mars-research/ixy.rs.mempunch/blob/master/examples/maglevgen.rs
(specific # of flows)

pktgen: https://github.com/mars-research/ixy.rs.mempunch/blob/redleaf/pktgen-config.txt
(infinite # of flows)

RedLeaf: domains/lib/libbenchnet/maglev.rs

Old Rust: https://github.com/mars-research/maglev-demo
