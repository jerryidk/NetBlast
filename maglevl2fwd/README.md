# l2fwd-maglev

An implementation of the Maglev load balancer integrated into DPDK l2fwd.

## Quickstart

nix develop -c bash build.sh

## Other Resources

DPDK C: https://github.com/mars-research/l2fwd-maglev

ixy.rs generator: https://github.com/mars-research/ixy.rs.mempunch/blob/master/examples/maglevgen.rs
(specific # of flows)

pktgen: https://github.com/mars-research/ixy.rs.mempunch/blob/redleaf/pktgen-config.txt
(infinite # of flows)

RedLeaf: domains/lib/libbenchnet/maglev.rs

Old Rust: https://github.com/mars-research/maglev-demo
