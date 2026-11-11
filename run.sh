#!/bin/bash
# Starts or restarts simulators in the background. They survive logout but not a reboot.
#   ./run.sh sp1 com1                         start/restart (endpoint, devices and tags from machines/<name>.init.json)
#   ./run.sh all                              every file in machines/
#   ./run.sh stop sp1 | stop all
#   ./run.sh reset sp1 | reset all            back to the file's starting values (and zero write counts), restarting
#   ./run.sh status                           which are running, and on what
#   ./run.sh ui / ./run.sh stop ui            web monitor (read-only) at http://portacon-simulator.local:8080
#                                             (--name lab-sim for another .local name, --port 8081 for another port)
#   ./run.sh silent com2 60                   no replies for 60 s (device down), without a restart; silent com2 0 ends it
#   ./run.sh boot                             start whatever was running before a reboot (cron runs this @reboot)
# Values and write counts are kept across restarts in <name>.state.json.
# <name>.delay sets reply delays and <name>.schedule.json value schedules; both are re-read on change.
cd "$(dirname "$0")"

names() { for f in machines/*.init.json; do basename "$f" .init.json; done; }
running() { [ -f "$1.pid" ] && kill -0 "$(cat "$1.pid")" 2>/dev/null; }
# autostart.list: what should be running, so ./run.sh boot can bring it back after a reboot
want() { touch autostart.list; grep -qx "$1" autostart.list || echo "$1" >>autostart.list; }
unwant() { [ -f autostart.list ] && { grep -vx "$1" autostart.list >autostart.list.tmp; mv autostart.list.tmp autostart.list; }; true; }
launcher=(nohup)
command -v setsid >/dev/null && launcher=(setsid nohup)   # setsid exists on Linux, not macOS

stop() {
  [ "$1" = ui ] && running mdns && { kill "$(cat mdns.pid)"; rm -f mdns.pid; }
  if running "$1"; then kill "$(cat "$1.pid")"; echo "stopped $1"; sleep 0.3; else echo "$1 is not running"; fi
  rm -f "$1.pid"
  unwant "$1"
}

start() {
  local name=$1
  [ -f "machines/$name.init.json" ] || { echo "no machines/$name.init.json (names: $(names | tr '\n' ' '))"; return 1; }
  running "$name" && { kill "$(cat "$name.pid")"; sleep 1; }
  "${launcher[@]}" python3 -u modbus_plc_sim.py --init "$PWD/machines/$name.init.json" --state "$PWD/$name.state.json" \
    --log "$PWD/$name.log" --delay-file "$PWD/$name.delay" --schedule-file "$PWD/$name.schedule.json" \
    --reads-file "$PWD/$name.reads.json" --reset-file "$PWD/$name.reset.json" \
    --silent-file "$PWD/$name.silent.json" --writes-log "$PWD/$name.writes.log" \
    >>"$name.out" 2>&1 </dev/null &
  echo $! >"$name.pid"
  want "$name"
  sleep 1
  if running "$name"; then
    echo "$name: $(grep -E 'listening on' "$name.log" | tail -n 1 | cut -d' ' -f2-)"
  else
    echo "$name failed to start:"; tail -n 5 "$name.out"; rm -f "$name.pid"; return 1
  fi
}

list() { if [ "$1" = all ]; then names; else printf '%s\n' "$@"; fi; }

case "$1" in
  ""|-h|--help) sed -n '2,13p' "$0"; exit 1 ;;
  status)
    for name in $(names); do
      ep=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("endpoint") or "tcp:%s" % json.load(open(sys.argv[1])).get("port"))' "machines/$name.init.json" 2>/dev/null)
      if running "$name"; then echo "$name: running (pid $(cat "$name.pid")) on $ep"; else echo "$name: stopped ($ep)"; fi
    done
    running ui && echo "ui: running (pid $(cat ui.pid)), $(tail -n 1 ui.log | cut -d' ' -f2-)"
    exit 0 ;;
  stop)
    shift; [ $# -gt 0 ] || { echo "usage: ./run.sh stop <name>... | all"; exit 1; }
    for n in $(list "$@"); do stop "$n"; done
    exit 0 ;;
  reset)
    shift; [ $# -gt 0 ] || { echo "usage: ./run.sh reset <name>... | all"; exit 1; }
    for n in $(list "$@"); do rm -f "$n.state.json"; start "$n"; done
    exit 0 ;;
  silent)
    name=${2:?usage: ./run.sh silent <name> <seconds>}; secs=${3:-60}
    [ -f "machines/$name.init.json" ] || { echo "no machines/$name.init.json"; exit 1; }
    if [ "$secs" -gt 0 ] 2>/dev/null; then
      echo "{\"until\": $(( $(date +%s) + secs ))}" >"$name.silent.json.tmp" && mv "$name.silent.json.tmp" "$name.silent.json"
      echo "$name: no replies for $secs s, until $(date -d "@$(( $(date +%s) + secs ))" +%H:%M:%S 2>/dev/null || date -r $(( $(date +%s) + secs )) +%H:%M:%S)"
    else
      rm -f "$name.silent.json"; echo "$name: answering again"
    fi
    exit 0 ;;
  boot)
    rm -f ./*.pid   # from before the reboot: those process ids may belong to something else now
    [ -f autostart.list ] || exit 0
    for n in $(cat autostart.list); do
      if [ "$n" = ui ]; then "$0" ui; elif [ -f "machines/$n.init.json" ]; then start "$n"; else unwant "$n"; fi
    done
    exit 0 ;;
  ui)
    shift
    # --name: the .local name the monitor is published under (mDNS), kept in ui.name for ./run.sh boot
    if [ "$1" = --name ]; then echo "${2:?usage: ./run.sh ui --name <name>}" >ui.name; shift 2; fi
    mdns_name=$(cat ui.name 2>/dev/null || echo portacon-simulator)
    running ui && { kill "$(cat ui.pid)"; sleep 1; }
    running mdns && { kill "$(cat mdns.pid)"; sleep 0.3; }
    lan_ip=$(ip route get 1.1.1.1 2>/dev/null | grep -o 'src [0-9.]*' | cut -d' ' -f2)
    if command -v avahi-publish >/dev/null && [ -n "$lan_ip" ]; then
      "${launcher[@]}" avahi-publish -a -R "$mdns_name.local" "$lan_ip" >>mdns.log 2>&1 </dev/null &
      echo $! >mdns.pid
    fi
    "${launcher[@]}" python3 -u webui.py "$@" >>ui.log 2>&1 </dev/null &
    echo $! >ui.pid
    want ui
    sleep 1
    if running ui; then
      tail -n 1 ui.log
      running mdns && grep -q "Established" <(tail -n 2 mdns.log) && echo "ui: also at http://$mdns_name.local:8080"
    else echo "ui failed to start:"; tail -n 5 ui.log; rm -f ui.pid; exit 1; fi
    exit 0 ;;
esac

fail=0
for n in $(list "$@"); do start "$n" || fail=1; done
exit $fail
