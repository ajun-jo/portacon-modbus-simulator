#!/bin/bash
# Starts or restarts one Modbus PLC simulator in the background. It survives logout but not a reboot.
#   ./run.sh sp1                              start/restart with machines/sp1.init.json (port and unit id from the file)
#   ./run.sh stop sp1                         stop it
#   ./run.sh status                           show which simulators are running
# A restart reloads the starting values and drops written ones.
# <name>.delay sets a reply delay in ms and is re-read on change: echo '{"default": 300}' > sp1.delay
cd "$(dirname "$0")"

running() { [ -f "$1.pid" ] && kill -0 "$(cat "$1.pid")" 2>/dev/null; }

case "$1" in
  stop)
    name=${2:?usage: ./run.sh stop <name>}
    if running "$name"; then kill "$(cat "$name.pid")"; echo "stopped $name"; else echo "$name is not running"; fi
    rm -f "$name.pid"
    exit 0 ;;
  status)
    for f in machines/*.init.json; do
      name=$(basename "$f" .init.json)
      if running "$name"; then echo "$name: running (pid $(cat "$name.pid"))"; else echo "$name: stopped"; fi
    done
    exit 0 ;;
  ""|-h|--help)
    sed -n '2,7p' "$0"; exit 1 ;;
esac

name=$1; shift
[ -f "machines/$name.init.json" ] || { echo "no machines/$name.init.json (names: $(cd machines && ls *.init.json | sed "s/.init.json//" | tr "\n" " "))"; exit 1; }
if running "$name"; then
  kill "$(cat "$name.pid")"; sleep 1
fi
rm -f "$name.state.json"
launcher=(nohup)
command -v setsid >/dev/null && launcher=(setsid nohup)   # setsid exists on Linux, not macOS
"${launcher[@]}" python3 -u modbus_plc_sim.py --init "$PWD/machines/$name.init.json" --state "$PWD/$name.state.json" \
  --refuse "hr:9000-9099,coil:9000-9099" --delay-file "$PWD/$name.delay" "$@" \
  >>"$name.log" 2>&1 </dev/null &
echo $! >"$name.pid"
sleep 1
if running "$name"; then
  tail -n 3 "$name.log"
else
  echo "$name failed to start:"; tail -n 5 "$name.log"; rm -f "$name.pid"; exit 1
fi
