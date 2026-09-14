#!/bin/bash
# Record the page backing every l2fwd run actually got, rather than the backing
# its flag asked for. MADV_HUGEPAGE is advisory and MADV_NOHUGEPAGE can fail, so
# a -B arm can silently measure a different page size than its label claims --
# and sweep.sh only samples the 1 GiB pool, which cannot tell 2 MiB from 4 KiB.
# Sampled from outside because the runs are already in flight.
OUT=${1:-/users/sohamb/sweeps/page_watch.log}
while true; do
  for P in $(pgrep -f '^/users/sohamb/NetBlast/l2fwd/./build/l2fwd' 2>/dev/null); do
    CMD=$(tr '\0' ' ' < /proc/$P/cmdline 2>/dev/null)
    read -r RSS THP HTLB < <(sudo awk '
      /^Rss:/{r=$2} /^AnonHugePages:/{t=$2} /^Private_Hugetlb:/{h=$2}
      END{print r, t, h}' /proc/$P/smaps_rollup 2>/dev/null)
    [ -n "$RSS" ] && [ "${RSS:-0}" -gt 1000000 ] 2>/dev/null && \
      echo "$(date -u +%H:%M:%S) pid=$P rss=$RSS thp=$THP hugetlb=$HTLB cmd=$CMD" >> "$OUT"
  done
  sleep 3
done
