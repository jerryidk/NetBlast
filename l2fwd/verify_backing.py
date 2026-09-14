"""Check that every run got the page backing its -B flag asked for.

`sweep.sh` samples the 1 GiB hugepage pool, which distinguishes 1 GiB from
not-1 GiB and nothing else -- it cannot tell a 2 MiB arm from a 4 KiB one. Both
of those are advisory: MADV_HUGEPAGE can be declined under memory fragmentation
and MADV_NOHUGEPAGE can fail, and in either case the run completes, the numbers
look reasonable, and the dataset is labelled with a page size it never had.

page_watch.sh samples each live l2fwd's own /proc/<pid>/smaps_rollup, which
reports what the kernel actually did. This reads that log and states, per
(-B flag, mode), whether the mapping was really backed the way it claims.

Expected, for the 8 GiB table:
    -B 1g       AnonHugePages ~0          Private_Hugetlb ~10 GiB (8 + DPDK's 2)
    -B thp2m    AnonHugePages ~8 GiB      Private_Hugetlb ~2 GiB  (DPDK only)
    -B 4k       AnonHugePages ~0          Private_Hugetlb ~2 GiB  (DPDK only)

Note `Private_Hugetlb` always carries DPDK's own -m 2000, so the discriminator
is the 8 GiB step above that, not the absolute value.

Usage: python3 verify_backing.py [page_watch.log]
"""
import collections
import pathlib
import re
import sys

LOG = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                   else "/users/sohamb/sweeps/page_watch.log")
GIB = 1024 * 1024  # the log is in kB

# THP coverage below this fraction of the table means the arm did not get what
# it asked for. Not 100%: mmap gives no 2 MiB alignment guarantee, so the
# leading and trailing partial 2 MiB regions of an 8 GiB mapping stay on 4 KiB
# pages -- under 4 MiB of 8192, i.e. 0.05%.
THP_OK = 0.98

rows = collections.defaultdict(list)
for line in LOG.read_text(errors="replace").splitlines():
    m = re.search(r"rss=(\d+) thp=(\d+) hugetlb=(\d+) cmd=(.*)", line)
    if not m:
        continue
    rss, thp, htlb, cmd = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    # Split on the EAL/application separator, which is a bare " -- ". Splitting
    # on "--" alone lands inside "--in-memory" and then picks up DPDK's own
    # `-m 2000` as the application mode.
    app = cmd.split(" -- ", 1)[1] if " -- " in cmd else cmd
    mode = (re.search(r"-m (\w+)", app) or [None, "?"])[1]
    flag = (re.search(r"-B (\S+)", cmd) or [None, "as-shipped"])[1]
    rows[(flag, mode)].append((rss, thp, htlb))

if not rows:
    sys.exit(f"no samples in {LOG}")

print(f"{'-B flag':12} {'mode':10} {'n':>4} {'THP GiB':>8} {'Hugetlb GiB':>12}  verdict")
bad = 0
for (flag, mode), v in sorted(rows.items()):
    thp = max(t for _, t, _ in v) / GIB
    htlb = max(h for _, _, h in v) / GIB
    if flag in ("1g", "as-shipped") and mode == "dramblast":
        ok, want = htlb > 9.0 and thp < 1.0, "1 GiB hugetlb"
    elif flag == "1g":
        ok, want = htlb > 9.0 and thp < 1.0, "1 GiB hugetlb"
    elif flag == "thp2m" or (flag == "as-shipped" and mode == "maglev"):
        ok, want = thp / 8.0 >= THP_OK, "2 MiB THP"
    elif flag == "4k":
        ok, want = thp < 0.5 and htlb < 3.0, "4 KiB"
    else:
        ok, want = True, "?"
    bad += not ok
    print(f"{flag:12} {mode:10} {len(v):>4} {thp:>8.2f} {htlb:>12.2f}  "
          f"{'OK' if ok else '*** NOT ' + want + ' ***'}  (wanted {want})")

print()
print("every arm got the backing it claims" if not bad else
      f"{bad} arm(s) did NOT get their stated backing -- those rows are invalid")
sys.exit(1 if bad else 0)
