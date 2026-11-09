#!/bin/bash
# Run the simulator on a portacon (or any Linux box with Python 3) over SSH, from anyone's machine.
#   ./remote.sh <host> start all | start wind1 sp1   copy the simulator there, then start (restart) machines
#   ./remote.sh <host> stop all  | stop wind1
#   ./remote.sh <host> ui                            copy the simulator there and start the web UI: http://<ip>:8080
#   ./remote.sh <host> status
#   ./remote.sh <host> show wind1 | get wind1 투입수 | set wind1 투입수=5000 "단선 1-3=1"
#   ./remote.sh <host> read hr 22400 --sim wind1     (and write: raw access, as in modbus_client.py)
#   ./remote.sh <host> log wind1                     follow the request log (Ctrl+C); outside a terminal, last 40 lines
#   ./remote.sh <host> check sp1                     self-test (writes test values; start it again afterwards)
#   ./remote.sh <host> deploy                        only copy the files
# <host> is user@ip, or a name from remote.hosts (one "name user@ip" per line, e.g. "office portacon@192.168.10.97").
# Hostnames aren't unique (every reComputer is "reComputer-R100x"), so use the IP.
# Files go to ~/modbus-plc-simulator on that box, and ptcsw there polls 127.0.0.1:5020-5026.
# One-time setup per person: ssh-copy-id user@ip   (so SSH logs in without a password)
set -e
cd "$(dirname "$0")"
SSH=${REMOTE_SSH:-ssh}
DIR=modbus-plc-simulator  # relative to the remote home directory

usage() { sed -n '2,16p' "$0"; exit 1; }
[ $# -ge 2 ] || usage
host=$1; cmd=$2; shift 2
if [ -f remote.hosts ]; then
  target=$(awk -v n="$host" '$1 == n { print $2; exit }' remote.hosts)
  [ -n "$target" ] && host=$target
fi

# Quote each argument for the remote shell: 'it'\''s' style keeps Korean and spaces intact in any shell.
q() { local a out=""; for a in "$@"; do out="$out '$(printf '%s' "$a" | sed "s/'/'\\\\''/g")'"; done; printf '%s' "$out"; }
remote() { $SSH "$host" "cd $DIR && $1"; }

machines() {
  local all m
  all=$(ls machines/*.init.json | sed 's#machines/##; s#\.init\.json##')
  [ $# -gt 0 ] || { echo "say which machines: all, or some of: $(echo $all)" >&2; exit 1; }
  if [ "$1" = all ]; then echo $all; return; fi
  for m in "$@"; do
    echo "$all" | grep -qx "$m" || { echo "no machine $m (machines: $(echo $all))" >&2; exit 1; }
  done
  echo "$@"
}

deploy() {
  COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -czf - \
    modbus_plc_sim.py modbus_client.py webui.py run.sh check_sim.sh machines/*.init.json \
    | $SSH "$host" "mkdir -p $DIR && tar -xzf - -C $DIR"
  echo "copied the simulator to $host:~/$DIR"
}

case $cmd in
  deploy) deploy ;;
  start)
    list=$(machines "$@")
    deploy
    remote "for m in $list; do ./run.sh \$m | tail -n 1; done" ;;
  stop)
    list=$(machines "$@")
    remote "for m in $list; do ./run.sh stop \$m; done" ;;
  ui)
    deploy
    remote "./run.sh ui$(q "$@")"
    echo "open http://${host#*@}:8080 in a browser (or the --port you gave)" ;;
  status) remote "./run.sh status" ;;
  show|get|set|read|write) remote "python3 modbus_client.py$(q "$cmd" "$@")" ;;
  log)
    list=$(machines "$@")
    if [ -t 0 ] && [ -t 1 ]; then
      $SSH -t "$host" "tail -n 20 -f $DIR/$list.log"  # in a terminal: follow until Ctrl+C
    else
      $SSH "$host" "tail -n 40 $DIR/$list.log"        # elsewhere (e.g. ! in Claude Code): last lines, then exit
    fi ;;
  check) remote "./check_sim.sh$(q "$@")" ;;
  *) usage ;;
esac
