# PortaCON Modbus Simulator

Test a Porta Studio project on a PortaCON **without real PLCs or field devices**.

The simulator pretends to be the Modbus devices in your Studio project — same ports, slave IDs, addresses and data
types — so ptcsw reads and writes them exactly as it would in the field. You control the values, can make devices slow
or unresponsive, and every request ptcsw sends is logged with millisecond timestamps.

- Runs on the PortaCON itself. No sudo, nothing to install (Python 3 standard library only).
- Speaks **Modbus RTU / ASCII** (on virtual serial ports) and **Modbus TCP**.
- Only the devices are simulated: ptcsw, IoTLink and the cloud stay real.
- It never edits `application.json` or ptcsw.

```
Porta Studio project ──build & deploy──► ptcsw on the PortaCON
                                              |
                                              |
                                              | (reads/writes over the serial path in 프로필 → COM1 설정 → 포트 경로)
                                              |
                                              ▼
                                  this simulator (/home/portacon/modbus-plc-simulator/ttyCOM1)
```

---

## Quick start

### 1. Install on the PortaCON (once per PortaCON)

From a laptop with this repo:

We're currently using CA111111 PortaCON at our office.
```bash
ssh-copy-id portacon@192.168.10.97          # so SSH stops asking for a password
./remote.sh portacon@192.168.10.97 install
```

This copies the simulator to `~/modbus-plc-simulator`, starts the web UI on port **8080**, and makes the simulators
come back by themselves after a reboot.

### 2. Load your Studio project into the simulator

Open **`http://<PortaCON IP>:8080`** → **Studio project** → **Load Studio project…** and pick the project JSON you
exported from Porta Studio.

The simulator creates one simulated device per port (COM1–COM3) with your devices and DPs, and shows the serial path
for each port.

### 3. Point Porta Studio at the simulator, then build and deploy

In Porta Studio, open **프로필** and set **포트 경로** for each COM port you use:

| Studio port | 포트 경로 (copy exactly) |
|---|---|
| COM1 | `/home/portacon/modbus-plc-simulator/ttyCOM1` |
| COM2 | `/home/portacon/modbus-plc-simulator/ttyCOM2` |
| COM3 | `/home/portacon/modbus-plc-simulator/ttyCOM3` |

Leave 보드레이트, 패리티, 데이터 비트 and 스톱 비트 as they are — they don't matter for the simulator.

> **Watch out for spaces.** A leading or trailing space in 포트 경로 (`" /home/..."`) makes the deploy fail.
> Set the path in Porta Studio, not by editing `application.json` on the PortaCON: every deploy overwrites that file.

### 4. Check the values

Open **IoTLink → data analysis** and compare with the web UI's **Expected in IoTLink** column. That's it.

---

## What to put in your Studio project

The simulator takes everything from your project. For each DP, Studio needs:

| Setting | Example | Notes |
|---|---|---|
| Port | COM1 | Decides which `ttyCOM` path ptcsw uses |
| Slave ID | 1 | Every slave ID on a port is simulated separately |
| Register type / function code | Coil, FC 01 | See the table below |
| Address | 90 | Sent as is, 0-based (Studio HR 234 = register 234) |
| Data type | BOOL | BOOL, INT16, UINT16, INT32, UINT32, FLOAT32, STRING |
| Endian | Little | Little = low word at the lower address |
| Write settings (optional) | FC 05, every 10 s | For DPs ptcsw writes |

| Register type | Read | Write | Holds |
|---|---|---|---|
| Coil (0x) | FC 01 | FC 05, FC 15 | bits (BOOL) |
| Discrete input (1x) | FC 02 | — | bits, read-only |
| Input register (3x) | FC 04 | — | numbers, read-only |
| Holding register (4x) | FC 03 | FC 06, FC 16 | numbers and strings |

### What value each DP starts with

Starting values encode where the DP lives, so a wrong address, port or endian shows up as a **wrong number**, not just
a missing one:

| Data type | Starting value | Example (dp81) |
|---|---|---|
| FLOAT32 | `<dp>.<port><slave>` | COM1, slave 1 → **81.11**; COM3, slave 2 → **81.32** |
| INT32 / UINT32 | `<dp><port><slave, 3 digits>` | COM1, slave 1 → **811001** |
| INT16 / UINT16 | `<dp><port>` | COM1 → **811** |
| BOOL | 1 | |

The **Expected in IoTLink** column already applies the DP's Studio expression (`*5` on 81.11 → 405.55).

---

## Checking the results

| What you see in IoTLink | What it means | What to check |
|---|---|---|
| The expected value | Working ✅ | |
| A huge or tiny number (81.11 shows as 2.4e-41) | Bytes or words are swapped | The DP's endian in Studio |
| Another DP's number (81.31 instead of 81.11) | Wrong address or port | The DP's address and port in Studio |
| Nothing, and the web UI says "not read yet" | ptcsw isn't asking this device | 포트 경로 in 프로필; `journalctl -u ptc_auto_service -f` on the PortaCON (look for `registered slot`) |
| Nothing, but the web UI shows ptcsw reading it | ptcsw reads it; the problem is after the PortaCON | IoTLink / server side |

The web UI's **ptcsw reading it** column shows when ptcsw last read each DP, so "never shows in IoTLink" and "ptcsw
never asks for it" are easy to tell apart. Write-only DPs never appear on dashboards; their write counts are in the same
table.

---

## Common tests

All of these can be done in the web UI (click a port in the left column) or from the command line on the PortaCON
(`cd ~/modbus-plc-simulator`).

### Change a value while ptcsw is polling

```bash
python3 modbus_client.py set com1 dp81=12.5
python3 modbus_client.py show com1            # every DP: value and how many times ptcsw wrote it
```

Your changes go through a separate control port, so they never collide with ptcsw and aren't counted as ptcsw writes.

### Did ptcsw write this value, or did the device change it?

- **ptcsw's writes** are counted per DP (`show com1`, or the UI's **Writes**) and listed in `com1.writes.log`, each
  with the time since the previous write to that address (`+10.004 s since the last write here`).
- **Changes you or a schedule make** are logged in `com1.log` and are *not* counted as writes.

### Overwrite test: does a periodically written DP recover?

Make the "PLC" force values to 0 every 25 s, then watch which DPs come back:

```bash
cat > com1.schedule.json <<'EOF'
[{"unit": 1, "tag": "dp81", "values": [0, 0], "every_s": 25},
 {"unit": 1, "tag": "dp82", "values": [0, 0], "every_s": 25}]
EOF
```

A DP with a periodic write (e.g. every 10 s) returns to its written value within one period; a DP without one stays at
0. To stop, empty the file (`echo '[]' > com1.schedule.json`); values stay where they are, so set them back or
`./run.sh reset com1`. Schedule changes are picked up within a second, no restart needed.

### Step a value over time

```bash
echo '[{"unit": 1, "tag": "dp81", "values": [5, 10], "every_s": 5}]' > com1.schedule.json
```

### Make a device slow

```bash
echo '{"default": 300}' > com1.delay                             # every reply 300 ms late
echo '{"cycle": {"ms": [500, 1000], "every_s": 10}}' > com1.delay # alternate 500 / 1000 ms every 10 s
rm com1.delay                                                    # back to normal
```

Also per slave (`{"unit": {"2": 800}}`) or per function code (`{"fc": {"4": 1200}}`). While a delayed reply is
pending on a serial port, new requests are dropped like a busy device would.

### Make a device go down

```bash
./run.sh silent com2 60     # no replies for 60 s, then it answers again
./run.sh silent com2 0      # end early
```

The port stays open, so ptcsw sees a dead device, not a missing port.

### A receiver that needs its command repeated

```bash
echo '[{"unit": 1, "table": "coil", "addr": 100, "after_s": 30}]' > com2.reset.json
```

Coil 100 goes back to 0 if ptcsw hasn't written it for 30 s. Logged as `RESET` in `com2.log` and `com2.writes.log`.

### Test write errors

Writes to holding registers and coils **9000–9099** are refused with Modbus exception 02. Reads there work.

### Start over

```bash
./run.sh reset com1         # starting values back, write counts zeroed
```

---

## Command reference

On the PortaCON, in `~/modbus-plc-simulator`:

```bash
./run.sh status                 # what's running, and on which port
./run.sh com1                   # start or restart (keeps current values)
./run.sh all                    # start every simulator in machines/
./run.sh stop com1              # ./run.sh stop all
./run.sh reset com1             # back to starting values, write counts zeroed
./run.sh silent com1 60         # no replies for 60 s
./run.sh ui                     # web UI on port 8080

python3 modbus_client.py show com1                         # all DPs with values and write counts
python3 modbus_client.py get com1 dp81 dp82
python3 modbus_client.py set com1 dp81=12.5 --unit 2       # --unit picks the slave when a port has several
python3 modbus_client.py read coil 70 21 --sim com1        # raw addresses: coil, di, ir, hr (0-based)
python3 modbus_client.py read ir 90 --type FLOAT32 --sim com1
python3 modbus_client.py write coil 70 1 --sim com1
python3 modbus_client.py reset-counts com1
python3 check_sim.py com1                                  # self-test; then ./run.sh reset com1
```

From your laptop, `./remote.sh <host> <command>` runs the same things on a PortaCON: `install`, `start`, `stop`,
`reset`, `status`, `ui`, `show`, `get`, `set`, `read`, `write`, `reset-counts`, `log`, `check`, `deploy`. Copy
`remote.hosts.example` to `remote.hosts` to use short names (`./remote.sh office status`). Identify PortaCONs by IP:
every unit's hostname is `reComputer-R100x`.

`check_sim.py` refuses to run while ptcsw has read the simulator in the last 10 s, so it never collides with a live test.

## Web UI

`http://<PortaCON IP>:8080`. For each simulated port:

- **Endpoint:** the path for 포트 경로 (with a copy button), the slave IDs, and when ptcsw last read it.
- **Values:** every DP, editable (switches for BOOL, Enter to set numbers and strings), with raw registers. Values
  ptcsw writes flash yellow.
- **Writes:** how many times ptcsw wrote each DP. **Reset write counts** zeroes them.
- **Reply delay**, **Value schedules**, **Go silent:** the tests above, without editing files.
- **Restart** keeps values; **Reset values** goes back to the starting values.
- **Request log:** every read, write, exception and delayed reply, with timestamps, function codes, addresses and
  decoded values. Filter to "writes, resets and silence only" for write-period checks.

There's no login: anyone who can reach port 8080 can change simulator values (and nothing else).

## Files on the PortaCON

| File | What it is |
|---|---|
| `machines/<name>.init.json` | A simulated port: its devices, DPs and starting values (made from your Studio project) |
| `<name>.log` | Every request (rotates at 20 MB) |
| `<name>.writes.log` | Only ptcsw's writes and auto-resets |
| `<name>.state.json` | Current values and write counts, kept across restarts |
| `<name>.delay`, `.schedule.json`, `.reset.json` | Test controls, re-read within a second of a change |
| `ttyCOM1`–`ttyCOM3` | The virtual serial ports ptcsw opens |

After a reboot, everything that was running starts again by itself (a cron `@reboot` entry runs `./run.sh boot`).
Values and write counts are kept.

## Troubleshooting

| Problem | Fix |
|---|---|
| Deploy fails right after changing 포트 경로 | Check for spaces before or after the path |
| ptcsw logs `Connection timed out` for the device | 포트 경로 still points at a real port (e.g. `/dev/ttyAMA2`); set the `ttyCOM` path and redeploy |
| Nothing works after the PortaCON was off | `./run.sh status`; if a simulator is stopped, `./run.sh <name>` |
| Values went back to the starting values | Someone ran `./run.sh reset`; values otherwise survive restarts |
| Web UI doesn't open | `./run.sh ui`, then check port 8080 is reachable from your network |

---

## Reference

### Simulator file format

`studio_to_init.py <project.json>` writes these from a Studio project (the web UI's upload does the same). You can also
write one by hand:

```json
{"endpoint": "pty:ttyCOM1", "framing": "rtu", "control_port": 6101,
 "defaults": {"word_order": "little"},
 "devices": [{"unit": 1, "name": "port 1 device", "tags": [
   {"tag": "dp81", "table": "coil", "addr": 90, "type": "BOOL", "value": 1},
   {"tag": "dp83", "table": "ir", "addr": 100, "type": "FLOAT32", "word_order": "little", "value": 81.5}]}],
 "schedules": [], "refuse": "hr:9000-9099,coil:9000-9099", "refuse_code": 2}
```

- `endpoint`: `pty:ttyCOM1` (virtual serial port) or `tcp:5020` (Modbus TCP, also UDP).
- `framing`: `rtu` or `ascii` on a pty; detected automatically on TCP (Modbus TCP, RTU-over-TCP, ASCII-over-TCP).
- `table`: `coil`, `di`, `ir` or `hr`. `addr`: the 0-based protocol address. `"addr": null` lists a DP without
  serving it (for an address that isn't known yet).
- `type`: BOOL (coil/di), BIT (one bit of a register, `"bit": 0-15`), INT16, UINT16, INT32, UINT32, FLOAT32, STRING
  (`words` = length in registers) or WORDS (raw registers).
- `word_order`: `little` puts the low word at the lower address. `byte_order` (strings): `little` puts the first
  character in the low byte. `encoding`: `utf-8` by default.
- `control_port`: the 127.0.0.1-only port the client and web UI use. TCP endpoints default to their port + 1000.
- Slave IDs not listed still answer (reading 0), and the log notes it once. Slave 0 on RTU/ASCII is a broadcast:
  writes are applied, nothing is sent back.
- Unsupported function codes get exception 01.

### ADL2012 machines

`machines/sp1`…`wind4` simulate the ADL2012 machines over Modbus TCP, generated from the ADL2012 PLC data sheet by
`xlsx_to_init.py`:

| Machine | Name | Real PLC | Simulator port | Unit ID | DPs |
|---|---|---|---|---|---|
| 권선기 1–4호기 | `wind1`–`wind4` | Yaskawa, 192.168.141.10/.20, 142.10/.20 : 10600/10601 | 5022–5025 | 1–4 | 52 |
| SP접착기 1–2호기 | `sp1`, `sp2` | Mitsubishi Q, 192.168.141.30, 142.30 : 4000 | 5020, 5021 | 5, 6 | 35 |
| 6면측정TP기 1호기 | `tp1` | Keyence, 192.168.141.40 : 5000 | 5026 | 7 | 51 |

`machines/address_map.csv` (opens in Excel) lists every DP's Modbus address. Vendor addresses map like this:

| PLC | Words | Bits |
|---|---|---|
| Mitsubishi Q | `D`/`R`/`W`/`ZR` n → hr n | `M`/`L`/`B`/`X`/`Y` n → coil n |
| Yaskawa (MEMOBUS) | `MW` n → hr n | `MB` *word*+*hex bit* → coil word×16+bit (MB4400 → coil 7040); past 65535, bit *b* of hr *word* (MB00055336 → hr 5533 bit 6) |
| Keyence | `DM`/`ZF`/`EM`/`FM` n → hr n | `R`/`MR`/`LR`/`CR` n → coil n |

- 15 권선기 alarms are bits inside holding registers (type BIT): 단선 1-1~1-8 are hr 22400 bits 0–7, the rest hr 5533,
  5553, 5563 and 5613.
- `현재 LOT No.` (MW520070) is past 65535, so it's a placeholder until the vendor gives a usable address.
- 32-bit values store the low word first; strings put the first character in the low byte (Mitsubishi `LH`).
  Starting values are made up so swaps show (counters above 65535, odd-length LOT numbers).
- When the sheet changes, put the new .xlsx in `metadata/` and run `python3 xlsx_to_init.py`.

### Limitations

- Modbus only. Real MELSEC MC, LS XGT FEnet and EtherNet/IP devices aren't simulated here; the older MC protocol
  simulator is in `mc_legacy/`.
- Exceptions only on the fixed refuse range (hr/coil 9000–9099), not on arbitrary chosen addresses yet.
- `docs/reference-modbus-sim.md` has notes on `~/modbus-sim`, the older RTU simulator from the Oct 29 tests.
