#!/bin/bash
# usage: test.sh <sim dir> <port> [host]
D=$1; PORT=$2; HOST=${3:-127.0.0.1}
C() { python3 "$D/mc_client.py" --host "$HOST" --port "$PORT" "$@"; }
for mode in "" "--ascii" "--udp" "--iqr" "--ascii --iqr --udp"; do
 echo "== mode [$mode]"
 C $mode read M5 1 --bit
 C $mode read D1104 --type INT32
 C $mode read R771 5 --type STRING
 C $mode read D8960 8 --type STRING
 C $mode read D1000 --type UINT16
done
echo "== writes"
C write D1104 --type INT32 " -5"; C --ascii read D1104 --type INT32
C --ascii write R771 --type STRING --words 5 "ABC"; C read R771 5; C read R771 5 --type STRING
C write M5 --bit 0; C --ascii read M5 1 --bit
C write M0 --bit 1010110011; C read M0 1; C --ascii read M0 10 --bit
echo "-- refused write:"; C write D9000 1234
