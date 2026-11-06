#!/bin/bash
# Self-test against a running simulator. It writes test values, so restart it afterwards (./run.sh <name>).
# Usage: ./check_sim.sh <name> [host], e.g. ./check_sim.sh sp1
cd "$(dirname "$0")"
SIM=${1:?usage: ./check_sim.sh <name> [host]}; HOST=${2:-127.0.0.1}
C() { echo "\$ modbus_client.py $*"; python3 modbus_client.py "$@" --host "$HOST"; }
R() { C "$@" --sim "$SIM"; }
C show "$SIM"
echo "== set by tag"
C set "$SIM" 투입수=-5 가동중=0 "LOT No.=ABC" 택타임=65535
echo "== raw registers and coils"
R read hr 1104 2
R read hr 1104 --type INT32
R read hr 771 5 --type STRING
R write coil 0 1010110011; R read coil 0 10
R write hr 20 --type FLOAT32 3.5; R read hr 20 --type FLOAT32
R write hr 30 0x1234 0x5678; R read hr 30 2
echo "== read-only tables through the FC 65 back door"
R write ir 10 --type INT32 -7; R read ir 10 --type INT32
R write di 3 1; R read di 0 8
echo "== expected errors"
R write hr 9000 1234        # refused range: exception 04
R read hr 65535 2           # past the end: exception 02
C set "$SIM" 택타임=70000 2>&1 || true   # out of range for UINT16, caught before sending
