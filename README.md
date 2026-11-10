# PortaCON Modbus Simulator

Simulates the Modbus RTU/TCP devices of a Porta Studio project on a PortaCON, so ptcsw can be tested without real
hardware. Python 3 only, no sudo. We're currently using the CA111111 PortaCON at our office (`192.168.10.97`).

## Setup

```bash
git clone https://github.com/ajun-jo/portacon-modbus-simulator.git
cd portacon-modbus-simulator
./remote.sh portacon@<ip> install                  # copies it to the PortaCON

# then on the PortaCON (~/modbus-plc-simulator):
python3 studio_to_init.py "project.json" --fresh
./run.sh reset com1 com2 com3
```

In Porta Studio, set **프로필 → COM1 설정 → 포트 경로** to `/home/portacon/modbus-plc-simulator/ttyCOM1`
(`ttyCOM2`, `ttyCOM3` for the other ports; no spaces), then build and deploy.

## Monitor

`http://<PortaCON IP>:8080` shows each DP's live value, what IoTLink should show, and a log of which DP ptcsw read or
wrote. Start/Stop is its only control.

## Terminal

```bash
./run.sh status                            # ./run.sh stop com1 · ./run.sh com1
python3 modbus_client.py show com1         # values and ptcsw's write counts
python3 modbus_client.py set com1 dp81=0
```

Delays, schedules and auto-reset are files next to each simulator: `com1.delay`, `com1.schedule.json`,
`com1.reset.json`.

## Log

`READ` ptcsw read it · `WRITE` ptcsw wrote it · `SET` changed in the simulator · `RESET` auto-reset to 0.
ptcsw's writes are also in `com1.writes.log`.
