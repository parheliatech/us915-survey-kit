#!/usr/bin/env bash
# 902-928 MHz spectrum survey: power survey + raw-IQ scan + decoding + report, resumable after a power cut.
#
# Usage: ./survey.sh [COMMAND] [RUN]
#   all        (default) every step below in order; steps already finished are skipped
#   check      dongle, tools and disk space
#   selftest   synthetic end-to-end test (no radio)
#   power      rtl_power survey for POWER_HOURS (continues where it stopped)
#   iqscan     raw-IQ scan, IQ_PASSES passes over 15 windows, decoding each dwell as it goes (continues where it stopped)
#   process    decode any dwells still waiting (after a crash, or with KEEP_IQ=yes to re-run)
#   report     build summary.json and report/report.html
#   status     what has been done in this run
#   stop       ask a running iqscan to stop after the current dwell
#   new        start a new run (then the same as all)
#   autostart  resume `all` automatically after a reboot or power cut (cron @reboot); noautostart removes it
# RUN is a folder name under DATA_DIR; default: the most recent run, or a new one named <date>_<site>.
# Settings: survey.conf (copied from survey.conf.example by install.sh).
set -uo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
cd "$KIT"
[ -f survey.conf ] || cp survey.conf.example survey.conf
. ./survey.conf
: "${MIN_FREE_GB:=5}" "${MAX_PARALLEL:=2}"
. lib/env.sh
CMD="${1:-all}"

slug() { echo "$1" | tr -cs 'A-Za-z0-9' '-' | sed 's/^-//; s/-$//' | tr 'A-Z' 'a-z'; }
case "$DATA_DIR" in /*) ;; *) DATA_DIR="$KIT/$DATA_DIR" ;; esac
mkdir -p "$DATA_DIR"
NEWRUN="$DATA_DIR/$(date +%Y%m%d_%H%M)_$(slug "$SITE_NAME")"
if [ -n "${2:-}" ]; then
  RUN="$DATA_DIR/$2"
elif [ "$CMD" = "new" ]; then
  RUN="$NEWRUN"; CMD=all
else
  RUN="$(ls -dt "$DATA_DIR"/*/ 2>/dev/null | head -n 1)"; RUN="${RUN%/}"
  [ -n "$RUN" ] || RUN="$NEWRUN"
fi
STATE="$RUN/state"; mkdir -p "$STATE" "$RUN/power" "$RUN/dwells"
IQ="${IQ_DIR:-$RUN/iq}"; mkdir -p "$IQ"
log() { echo "$(date '+%F %T') $*" | tee -a "$RUN/survey.log"; }
done_mark() { touch "$STATE/$1.done"; }
is_done() { [ -f "$STATE/$1.done" ]; }

write_site() {
  python3 - "$RUN/site.json" <<EOF
import json, sys
json.dump(dict(name="""$SITE_NAME""", location="""$SITE_LOCATION""", operator="""$OPERATOR""", antenna="""$ANTENNA""",
               gain=$GAIN, ppm=float("""${PPM:-0}""")), open(sys.argv[1], 'w'), indent=1)
EOF
}

ppm_value() {  # the dongle error given to the decoders: survey.conf, else 0. The report measures the real error from
  # decoded packets on known channels. (rtl_test -p was tried and is not used: it measures against the PC clock and
  # USB timing and was 10 ppm off on the test dongle.)
  echo "${PPM:-0}"
}

cmd_check() {
  local ok=1
  for c in rtl_test rtl_sdr rtl_power python3; do command -v $c >/dev/null || { echo "MISSING: $c (run ./install.sh)"; ok=0; }; done
  python3 -c "from gnuradio import lora_sdr" 2>/dev/null || { echo "MISSING: gr-lora_sdr (run ./install.sh)"; ok=0; }
  local rt; rt="$(timeout 15 rtl_test -d "$DEVICE" -t 2>&1)"
  if grep -q "Found [1-9]" <<<"$rt" && ! grep -qi "usb_claim_interface error\|Failed to open" <<<"$rt"; then echo "OK: RTL-SDR found"
  else echo "PROBLEM: no usable RTL-SDR (plugged in? another program using it? output: $(tail -n 2 <<<"$rt" | tr '\n' ' '))"; ok=0; fi
  local need_gb
  local free_iq=$(df -BG --output=avail "$IQ" | tail -n 1 | tr -dc 0-9)
  # one dwell is DWELL_S x 4.8 MB; keep MIN_FREE_GB spare (iqscan pauses below it)
  local scan_gb=$(( IQ_PASSES * 15 * DWELL_S * 48 / 10000 + 1 ))   # whole scan, if nothing were deleted
  if [ "$KEEP_IQ" = yes ]; then need_gb=$(( scan_gb + MIN_FREE_GB )); else need_gb=$(( (scan_gb < IQ_CAP_GB ? scan_gb : IQ_CAP_GB) + MIN_FREE_GB )); fi
  echo "Disk: ${free_iq} GB free for IQ in $IQ, about ${need_gb} GB needed (KEEP_IQ=$KEEP_IQ)"
  [ "$free_iq" -ge "$need_gb" ] || { echo "PROBLEM: not enough disk space"; ok=0; }
  local hrs=$(python3 -c "print(round($POWER_HOURS + $IQ_PASSES*15*($DWELL_S+3)/3600, 1))")
  echo "Planned: power survey ${POWER_HOURS} h, then IQ scan ${IQ_PASSES} passes x 15 windows x ${DWELL_S} s: about ${hrs} h in total"
  [ $ok = 1 ] && echo "check passed" || { echo "check FAILED"; return 1; }
}

cmd_power() {
  [ "$POWER_HOURS" = 0 ] && { log "power survey skipped (POWER_HOURS=0)"; return; }
  local have=$(python3 - "$RUN/power" <<'EOF'
import glob, sys, datetime as dt
tot = 0
for f in glob.glob(sys.argv[1] + '/*.csv'):
    ts = []
    for line in open(f):
        p = line.split(',')
        if len(p) > 6:
            try: ts.append(dt.datetime.strptime(p[0].strip() + ' ' + p[1].strip(), '%Y-%m-%d %H:%M:%S'))
            except ValueError: pass
    if len(ts) > 1: tot += (max(ts) - min(ts)).total_seconds()
print(int(tot))
EOF
)
  local left=$(python3 -c "print(max(0, int($POWER_HOURS*3600 - $have)))")
  if [ "$left" -le "$(python3 -c "print(max(10, int($POWER_HOURS*36)))")" ]; then log "power survey complete"; return; fi
  local out="$RUN/power/power_$(date +%Y%m%d_%H%M%S).csv"
  log "power survey: ${left} s to go -> $out"
  rtl_power -d "$DEVICE" -f 902M:928M:25k -g "$GAIN" -i "$POWER_INTERVAL" -c "$POWER_CROP" -e "${left}s" "$out" 2>> "$RUN/power/rtl_power.log"
}

cmd_power_analyze() {
  ls "$RUN"/power/*.csv >/dev/null 2>&1 || return 0
  log "analysing power survey"
  python3 tools/power_analyze.py "$RUN/power" "$RUN"/power/*.csv
}

cmd_iqscan() {
  local keep=(); [ "$KEEP_IQ" = yes ] && keep=(--keep-iq)
  rm -f "$RUN/STOP"
  log "IQ scan: $IQ_PASSES passes, dwell $DWELL_S s, gain $GAIN, ppm $(ppm_value)"
  python3 tools/iqscan.py "$RUN" --iq-dir "$IQ" --passes "$IQ_PASSES" --dwell "$DWELL_S" --gain "$GAIN" --ppm "$(ppm_value)" \
    --iq-cap-gb "$IQ_CAP_GB" --min-free-gb "$MIN_FREE_GB" --max-parallel "$MAX_PARALLEL" --fsk-seconds "$FSK_SECONDS" --device "$DEVICE" "${keep[@]}"
}

cmd_process() {
  local keep=(); [ "$KEEP_IQ" = yes ] && keep=(--keep-iq)
  local n=0 miss=0
  for m in "$RUN"/dwells/*.meta.json; do
    [ -e "$m" ] || continue
    [ -f "${m%.meta.json}.done" ] && continue
    n=$((n + 1))
    python3 tools/process_dwell.py "$m" --ppm "$(ppm_value)" --fsk-seconds "$FSK_SECONDS" "${keep[@]}" || miss=$((miss + 1))
  done
  log "process: $n dwell(s) needed processing, $miss could not be finished (IQ missing or a decoder failed; see dwells/*.process.log)"
}

cmd_report() {
  write_site
  cmd_power_analyze
  python3 tools/summarize.py "$RUN" && python3 tools/report.py "$RUN" && log "report: $RUN/report/report.html"
}

cmd_status() {
  echo "Run: $RUN"
  for s in power iqscan report; do printf '  %-10s %s\n' "$s" "$(is_done $s && echo done || echo -)"; done
  local m=$(ls "$RUN"/dwells/*.meta.json 2>/dev/null | wc -l) d=$(ls "$RUN"/dwells/*.done 2>/dev/null | wc -l)
  echo "  dwells recorded: $m of $((IQ_PASSES * 15)), processed: $d"
  ls "$RUN"/power/*.csv >/dev/null 2>&1 && echo "  power CSV: $(du -ch "$RUN"/power/*.csv | tail -n 1 | cut -f1)"
  [ -f "$RUN/report/report.html" ] && echo "  report: $RUN/report/report.html"
}

lock() {  # one survey at a time: the dongle can only be used by one program
  exec 9>"$DATA_DIR/.survey.lock"
  flock -n 9 || { echo "another survey.sh is already running (it holds $DATA_DIR/.survey.lock)"; exit 1; }
}
case "$CMD" in all|power|iqscan|process) lock ;; esac

step() {  # run a step once; a step that fails stops `all` so it can be fixed and resumed
  local name=$1; shift
  if is_done "$name"; then echo "($name already done)"; return 0; fi
  "$@" && done_mark "$name"
}

case "$CMD" in
  check) cmd_check ;;
  selftest) python3 tools/selftest.py ;;
  power) cmd_power && done_mark power ;;
  iqscan) cmd_iqscan && done_mark iqscan ;;
  process) cmd_process ;;
  report) cmd_report && done_mark report ;;
  status) cmd_status ;;
  stop) touch "$RUN/STOP"; echo "iqscan will stop after the current dwell ($RUN/STOP)" ;;
  autostart)
    ( crontab -l 2>/dev/null | grep -v '# survey-kit autostart'; \
      echo "@reboot sleep 90 && cd '$KIT' && ./survey.sh all >> '$DATA_DIR/autostart.log' 2>&1  # survey-kit autostart" ) | crontab -
    echo "After a reboot the survey resumes by itself (log: $DATA_DIR/autostart.log). Remove with ./survey.sh noautostart" ;;
  noautostart) crontab -l 2>/dev/null | grep -v '# survey-kit autostart' | crontab -; echo "autostart removed" ;;
  all)
    log "survey run: $RUN"
    write_site
    cmd_check || exit 1
    step power cmd_power || exit 1
    step iqscan cmd_iqscan || exit 1
    cmd_process
    rm -f "$STATE/report.done"; step report cmd_report || exit 1
    log "finished: open $RUN/report/report.html" ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
