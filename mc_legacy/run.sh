#!/bin/bash
# Starts or restarts one MC protocol PLC simulator in the background. It survives logout but not a reboot.
# Usage: ./run.sh <name> <port>, e.g. ./run.sh sp1 4000 (SP접착기 1호기) and ./run.sh sp2 4001 (SP접착기 2호기).
# <name>.init.json sets starting values (a restart reloads them and drops written values).
# <name>.delay sets a reply delay in ms and is re-read on change: echo '{"default": 300}' > sp1.delay
cd "$(dirname "$0")"
name=${1:?name}; port=${2:?port}
if [ -f "$name.pid" ] && kill -0 "$(cat "$name.pid")" 2>/dev/null; then
  kill "$(cat "$name.pid")"; sleep 1
fi
rm -f "$name.state.json"
setsid nohup python3 -u mc_plc_sim.py --port "$port" \
  --init "$PWD/$name.init.json" --state "$PWD/$name.state.json" \
  --refuse "D:9000-9099,M:9000-9099" --delay-file "$PWD/$name.delay" \
  >>"$name.log" 2>&1 </dev/null &
echo $! >"$name.pid"
sleep 1
tail -n 1 "$name.log"
