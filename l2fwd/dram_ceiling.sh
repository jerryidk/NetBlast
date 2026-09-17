#!/usr/bin/env bash
#
# The DRAM ceiling that the saturation study divides by, measured across thread
# counts instead of extrapolated from one.
#
# WHY THIS EXISTS
# ---------------
# `.dram_ceiling` carries DRAM_ACHIEVED_GBS empty on purpose. It previously held
# 360.0 GB/s, being the peer session's 24-thread READ ceiling scaled by a
# mixed/read ratio measured here at FOUR cores. That extrapolation was withdrawn
# because it rests on an untested assumption -- that the mixed/read ratio is
# independent of thread count -- and because the peer's 24-thread figure had not
# plateaued (+18.3% from 16 threads), making it a lower bound. Dividing a
# utilisation by a lower bound OVERSTATES saturation.
#
# validate_counters.sh could not do better: it runs from an agent shell, which
# user.slice confines to cpus 24-27,52-55, and `taskset` cannot escape a cpuset.
# Four cores was a limit, not a choice. Launched into bench.slice this script
# reaches cores 0-23 -- 24 distinct physical cores, since the sibling of core c
# is c+28 -- and can therefore measure the ceiling at the thread count l2fwd
# actually runs at rather than assuming a ratio carries.
#
# Sweeping the thread count, rather than measuring only at 24, is the point: a
# ceiling that is still climbing at the top of the sweep is a lower bound and
# must be reported as one. That is the defect being repaired, so the new figure
# must not reproduce it silently.
#
# METHOD, inherited unchanged from validate_counters.sh
# -----------------------------------------------------
#   (1) MIXED, not read-only. l2fwd fills a line and dirties it, so DRAM sees a
#       read and a write per line. A read-only peak is the wrong denominator in
#       the flattering direction. Both arms are measured; MIXED is the one that
#       becomes DRAM_ACHIEVED_GBS.
#   (2) DIFFERENCED over two pass counts. Every probe memsets its buffer to
#       fault pages in; a fixed window charges that memset to an interval chosen
#       by guesswork. Differencing 3 passes against 9 cancels it, along with
#       allocation and first touch.
#   (3) ONE BINARY, SAME CORES, BOTH ARMS. An earlier attempt took the ratio
#       between this rmw probe and the peer's separate read probe and got
#       mixed/read = 1.63 -- mixed ABOVE read, the opposite of the physical
#       expectation. It was measuring the two probes, not the memory system.
#
# REFUSES TO RUN ON THE WRONG CORES
# ---------------------------------
# The failure this guards against is the one that motivates benchctl: a probe
# confined to the housekeeping cpuset does not fail, it returns a plausible
# wrong number. So the script reads its own Cpus_allowed_list and exits unless
# it is actually on bench cores. --hk overrides for a deliberate 4-core
# comparison against the existing READ_4/MIXED_4 figures.
#
# Usage:
#   sudo ~/.bench-coord/benchctl run --cpus 0-23 --purpose "DRAM ceiling" \
#        --log <f> -- l2fwd/dram_ceiling.sh <outdir>
#
#   THREADS="1 2 4 8 16 24"   thread counts to sweep
#   MIB=2048                  per-thread buffer, MiB (must be >> 52.5 MiB L3)
#   WRITE_CEILING=1           update .dram_ceiling in place when done
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/counter_groups.sh"

OUT_DIR="${1:?usage: $0 <outdir>}"
BM="${BM:-$HERE/bench_membw}"
THREADS="${THREADS:-1 2 4 8 16 24}"
MIB="${MIB:-2048}"
WRITE_CEILING="${WRITE_CEILING:-1}"
ALLOW_HK="${ALLOW_HK:-0}"
[ "${2:-}" = "--hk" ] && ALLOW_HK=1

mkdir -p "$OUT_DIR"
CSV="$OUT_DIR/dram_ceiling.csv"

[ -x "$BM" ] || { echo "FATAL: bench_membw not built at $BM" >&2
                  echo "       cc -O2 -o $BM $HERE/bench_membw.c" >&2; exit 1; }

# --- the guard: are we actually on the cores we think we are? --------------
ALLOWED="$(awk '/Cpus_allowed_list/{print $2}' /proc/self/status)"
echo "cpus_allowed_list: $ALLOWED"
if [ "$ALLOW_HK" = "0" ]; then
  case "$ALLOWED" in
    *24-27*|*52-55*)
      echo "FATAL: confined to the housekeeping cpuset ($ALLOWED)." >&2
      echo "       taskset cannot escape a cpuset, so every core below would" >&2
      echo "       silently land on 24-27 and the sweep would report the same" >&2
      echo "       four-core number at every thread count -- a plausible wrong" >&2
      echo "       answer, which is the failure this file exists to avoid." >&2
      echo "       Launch via: benchctl run --cpus 0-23 -- $0 $OUT_DIR" >&2
      echo "       Or pass --hk to compare deliberately against READ_4." >&2
      exit 1;;
  esac
fi

MAXT=$(echo $THREADS | tr ' ' '\n' | sort -n | tail -1)
NAVAIL=$(echo "$ALLOWED" | tr ',' '\n' | awk -F- '{print ($2?$2:$1)-$1+1}' | paste -sd+ | bc)
if [ "$NAVAIL" -lt "$MAXT" ]; then
  echo "FATAL: $MAXT threads requested but only $NAVAIL cpus allowed ($ALLOWED)." >&2
  echo "       Oversubscribing would measure scheduling, not bandwidth." >&2
  exit 1
fi
CORELIST=$(echo "$ALLOWED" | tr ',' '\n' \
           | awk -F- '{for(i=$1;i<=($2?$2:$1);i++) print i}' | paste -sd' ')

# --- one differenced measurement -------------------------------------------
# $1 mode  $2 nthreads -> "<total>,<read>,<write>" GB/s
run_probe() {   # $1 mode  $2 passes  $3 cores -> "<read lines>,<write lines>"
  local out
  out=$(perf stat -a -e "$G_BW" -x, -- \
        bash -c 'for c in '"$3"'; do
                   taskset -c $c '"$BM"' '"$1"' '"$MIB"' '"$2"' >/dev/null 2>&1 &
                 done; wait' 2>&1)
  printf '%s,%s' \
    "$(awk -F, '/cas_rd_/{s+=$1} END{print s+0}' <<<"$out")" \
    "$(awk -F, '/cas_wr_/{s+=$1} END{print s+0}' <<<"$out")"
}

ceiling_for() {   # $1 mode  $2 cores -> "<total>,<read>,<write>"
  local t1s t1e t2s t2e a b dt rd wr
  t1s=$(date +%s.%N); a=$(run_probe "$1" 3 "$2"); t1e=$(date +%s.%N)
  t2s=$(date +%s.%N); b=$(run_probe "$1" 9 "$2"); t2e=$(date +%s.%N)
  dt=$(awk -v p="$t1s" -v q="$t1e" -v r="$t2s" -v s="$t2e" 'BEGIN{print (s-r)-(q-p)}')
  rd=$(awk -v x="${a%%,*}" -v y="${b%%,*}" -v d="$dt" \
       'BEGIN{printf "%.1f",(d>0)?(y-x)*'"$CAS_LINE_BYTES"'/d/1e9:0}')
  wr=$(awk -v x="${a##*,}" -v y="${b##*,}" -v d="$dt" \
       'BEGIN{printf "%.1f",(d>0)?(y-x)*'"$CAS_LINE_BYTES"'/d/1e9:0}')
  printf '%s,%s,%s' "$(awk -v p="$rd" -v q="$wr" 'BEGIN{printf "%.1f",p+q}')" "$rd" "$wr"
}

echo "nthreads,mode,total_gbs,read_gbs,write_gbs,cores" > "$CSV"
echo "=== DRAM ceiling sweep: threads [$THREADS], ${MIB} MiB/thread ==="
for nt in $THREADS; do
  CORES=$(echo $CORELIST | tr ' ' '\n' | head -n "$nt" | paste -sd' ')
  for mode in streamn rmwn; do
    R=$(ceiling_for "$mode" "$CORES")
    label=$([ "$mode" = streamn ] && echo read || echo mixed)
    echo "$nt,$label,${R%%,*},$(cut -d, -f2 <<<"$R"),$(cut -d, -f3 <<<"$R"),\"$CORES\"" >> "$CSV"
    printf '  %2d threads  %-5s  %6s GB/s  (%s read + %s write)\n' \
      "$nt" "$label" "${R%%,*}" "$(cut -d, -f2 <<<"$R")" "$(cut -d, -f3 <<<"$R")"
  done
done

# --- the headline, and whether it is a ceiling or a lower bound -------------
TOP=$(awk -F, -v t="$MAXT" '$1==t && $2=="mixed"{print $3}' "$CSV")
PREV=$(echo $THREADS | tr ' ' '\n' | sort -n | tail -2 | head -1)
PREV_V=$(awk -F, -v t="$PREV" '$1==t && $2=="mixed"{print $3}' "$CSV")
GROWTH=$(awk -v a="$PREV_V" -v b="$TOP" 'BEGIN{printf "%.1f",(a>0)?(b/a-1)*100:0}')
PLATEAUED=$(awk -v g="$GROWTH" 'BEGIN{print (g<5)?"yes":"no"}')

echo
echo "mixed ceiling at $MAXT threads: $TOP GB/s"
echo "growth from $PREV to $MAXT threads: ${GROWTH}%  (plateaued: $PLATEAUED)"
if [ "$PLATEAUED" = "no" ]; then
  echo "NOTE: still climbing at the top of the sweep, so this is a LOWER BOUND."
  echo "      Utilisations divided by it are OVERSTATED -- the same caveat the"
  echo "      withdrawn 360.0 carried. Extend THREADS if more cores are free."
fi

if [ "$WRITE_CEILING" = "1" ]; then
  CF="$HERE/.dram_ceiling"
  BOUND=$([ "$PLATEAUED" = "yes" ] && echo "measured plateau" || echo "LOWER BOUND, still climbing")
  {
    echo "# --- measured by dram_ceiling.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"
    echo "# ${MAXT} threads on cpus: $(echo $CORELIST | tr ' ' '\n' | head -n $MAXT | paste -sd,)"
    echo "# ${MIB} MiB/thread, differenced over 3 vs 9 passes, mixed (rmwn) arm."
    echo "# NOT comparable to READ_4/MIXED_4 above: those were taken on cpus"
    echo "# 24-27, the contended housekeeping set. Different cores, different"
    echo "# contention, and 24-27 is where the 178-tick reading came from."
    echo "# Status: $BOUND (+${GROWTH}% from $PREV threads)."
    echo "# Full series: $CSV"
  } >> "$CF"
  sed -i "s/^DRAM_ACHIEVED_GBS=.*/DRAM_ACHIEVED_GBS=$TOP/" "$CF"
  echo "updated $CF: DRAM_ACHIEVED_GBS=$TOP"
fi
echo "series -> $CSV"
