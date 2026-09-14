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
    # `total=` is optional: page_watch.sh gained that field when its own RSS
    # threshold was found to exclude every hugetlb-backed run. Old lines in the
    # log predate it, and both must parse or the fix would silently drop the
    # history it was meant to complete.
    m = re.search(r"rss=(\d+) thp=(\d+) hugetlb=(\d+)(?: total=\d+)? cmd=(.*)", line)
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

# An arm with no samples must not pass silently. The loop above can only judge
# rows that exist, so a watcher that never saw an arm produced a clean bill of
# health for it -- which is exactly what happened: page_watch.sh gated on Rss,
# hugetlb pages are not counted in Rss, and so every dramblast run on 1 GiB
# pages was missing from the log while this script printed "every arm got the
# backing it claims". Absence of evidence was being reported as evidence.
EXPECTED = [("as-shipped", "dramblast"), ("as-shipped", "maglev"),
            ("thp2m", "dramblast"), ("4k", "dramblast"),
            ("1g", "maglev"), ("4k", "maglev")]
missing = [k for k in EXPECTED if k not in rows]
MIN_SAMPLES = 3
thin = [(k, len(rows[k])) for k in EXPECTED
        if k in rows and len(rows[k]) < MIN_SAMPLES]

print()
for flag, mode in missing:
    print(f"*** NO SAMPLES for {flag} / {mode} -- this arm was never verified")
for (flag, mode), n in thin:
    print(f"*** only {n} sample(s) for {flag} / {mode} -- below the {MIN_SAMPLES} "
          f"needed to call it verified")
if not bad and not missing and not thin:
    print("every arm got the backing it claims")
else:
    parts = []
    if bad:
        parts.append(f"{bad} arm(s) did NOT get their stated backing")
    if missing:
        parts.append(f"{len(missing)} arm(s) were never sampled")
    if thin:
        parts.append(f"{len(thin)} arm(s) were sampled too thinly to judge")
    print("; ".join(parts) + " -- do not read this as a clean verification")
sys.exit(1 if (bad or missing or thin) else 0)
