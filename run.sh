#!/bin/bash
# Starts or restarts simulators in the background. They survive logout but not a reboot.
#   ./run.sh sp1 com1                         start/restart (endpoint, devices and tags from machines/<name>.init.json)
#   ./run.sh all                              every file in machines/
#   ./run.sh stop sp1 | stop all
#   ./run.sh reset sp1 | reset all            back to the file's starting values (and zero write counts), restarting
#   ./run.sh status                           which are running, and on what
#   ./run.sh ui / ./run.sh stop ui            web UI on port 8080 (./run.sh ui --port 8081 for another port)
# Values and write counts are kept across restarts in <name>.state.json.
# <name>.delay sets reply delays and <name>.schedule.json value schedules; both are re-read on change.
cd "$(dirname "$0")"

names() { for f in machines/*.init.json; do basename "$f" .init.json; done; }
running() { [ -f "$1.pid" ] && kill -0 "$(cat "$1.pid")" 2>/dev/null; }
launcher=(nohup)
command -v setsid >/dev/null && launcher=(setsid nohup)   # setsid exists on Linux, not macOS

stop() {
  if running "$1"; then kill "$(cat "$1.pid")"; echo "stopped $1"; sleep 0.3; else echo "$1 is not running"; fi
  rm -f "$1.pid"
}

start() {
  local name=$1
  [ -f "machines/$name.init.json" ] || { echo "no machines/$name.init.json (names: $(names | tr '\n' ' '))"; return 1; }
  running "$name" && { kill "$(cat "$name.pid")"; sleep 1; }
  "${launcher[@]}" python3 -u modbus_plc_sim.py --init "$PWD/machines/$name.init.json" --state "$PWD/$name.state.json" \
    --log "$PWD/$name.log" --delay-file "$PWD/$name.delay" --schedule-file "$PWD/$name.schedule.json" \
    >>"$name.out" 2>&1 </dev/null &
  echo $! >"$name.pid"
  sleep 1
  if running "$name"; then
    echo "$name: $(grep -E 'listening on' "$name.log" | tail -n 1 | cut -d' ' -f2-)"
  else
    echo "$name failed to start:"; tail -n 5 "$name.out"; rm -f "$name.pid"; return 1
  fi
}

list() { if [ "$1" = all ]; then names; else printf '%s\n' "$@"; fi; }

case "$1" in
  ""|-h|--help) sed -n '2,11p' "$0"; exit 1 ;;
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
  ui)
    shift
    running ui && { kill "$(cat ui.pid)"; sleep 1; }
    "${launcher[@]}" python3 -u webui.py "$@" >>ui.log 2>&1 </dev/null &
    echo $! >ui.pid
    sleep 1
    if running ui; then tail -n 1 ui.log; else echo "ui failed to start:"; tail -n 5 ui.log; rm -f ui.pid; exit 1; fi
    exit 0 ;;
esac

fail=0
for n in $(list "$@"); do start "$n" || fail=1; done
exit $fail
