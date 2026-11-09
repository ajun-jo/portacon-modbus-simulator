# Modbus simulator for PortaCON / ptcsw testing

Stands in for the field devices a Porta Studio project describes, so ptcsw on a PortaCON can be tested without real
hardware. To ptcsw it looks like those devices: same ports, slave ids, addresses, data types and word order. It also
gives testers control over values, timing and faults, and logs every request with ms timestamps.
Python 3 standard library only; nothing to install.

```
Porta Studio project ──build──► ptcsw (device map compiled in)
        │                          │ opens the serial path / IP:port that application.json names
        └─ studio_to_init.py ──► machines/com1.init.json ──► modbus_plc_sim.py ── pty ~/modbus-plc-simulator/ttyCOM1
                                                                               └─ or TCP 127.0.0.1:5020…
```

The simulator replaces devices only: ptcsw, IoTLink and the cloud stay real. It never edits application.json or
ptcsw, and the tools that change values use a separate control port, so they're never a second master on a port.
`docs/reference-modbus-sim.md` has notes on `~/modbus-sim`, the RTU simulator from the Oct 29 tests, which this one
follows.

## Testing a Studio project on a PortaCON

1. **Export the project JSON from Studio and build the simulator files** (one per Studio port, every device on it
   by slave id, every enabled datapoint):
   ```bash
   python3 studio_to_init.py "metadata/studio/포타콘 미들웨어 크래쉬 버그 테스팅.json"
   ```
   ```
   machines/com1.init.json  COM1 pty:ttyCOM1, control port 6101
     slave 1 port 1 device: dp82=ir 70 FLOAT32, dp81=ir 90 FLOAT32, dp125=ir 106 FLOAT32
   machines/com2.init.json  COM2 pty:ttyCOM2, control port 6102
   …
   ```
   Each DP starts at a value that identifies it (dp81 = 81.5), so you can tell DPs apart in the cloud.
2. **Start them on the PortaCON**, with the web UI:
   ```bash
   ./remote.sh portacon@192.168.10.97 start com1 com2 com3
   ./remote.sh portacon@192.168.10.97 ui          # http://192.168.10.97:8080
   ```
3. **Point ptcsw at them in IoTLink's deploy config**, not only in the file on the PortaCON (every cloud deploy rewrites
   application.json). Use the full absolute path; relative paths fail silently:

   | Studio port | application.json key | Simulator path |
   |---|---|---|
   | COM1 | `interfaces.rs232.port` | `/home/portacon/modbus-plc-simulator/ttyCOM1` |
   | COM2 | `interfaces.rs485.port` | `/home/portacon/modbus-plc-simulator/ttyCOM2` |
   | COM3 | `interfaces.com3.port` | `/home/portacon/modbus-plc-simulator/ttyCOM3` |
   | TCP | the device's IP and port | `127.0.0.1` and the file's port |

   Baud and parity don't matter on a pty (so a pty can't catch a wrong serial setting).
4. **Deploy.** A config-only deploy reboots the PortaCON, which stops the simulators: start them again (step 2).
   Values and write counts are kept across restarts.
5. **Check:** the machine's badge in the web UI turns green ("read 1s ago") and the log shows ptcsw's batch reads.
   On the PortaCON, `journalctl -u ptc_auto_service.service -f` should show `registered slot N … with K datapoints`
   and no `batch read … failed: Connection timed out`.

When the project changes in Studio, export it again and rerun `studio_to_init.py`. It keeps the values, control
ports and schedules already in the files. `--prefix crash` names the files `crash-com1`, … to keep several projects.

## Web UI

`./run.sh ui` (or `./remote.sh <host> ui`) serves it on port 8080. For each simulator:

- **Endpoint:** the pty path to put in application.json (with a copy button), or the TCP port; the slave ids; whether
  and when a master last read it, and from where.
- **Values:** every tag in engineering units, editable (switches for BOOL/BIT, Enter to set numbers and strings),
  with the raw registers. Values ptcsw writes flash yellow.
- **Writes:** how many times the master wrote each DP since the last reset, and writes to addresses without a tag.
  "No repeat writes" passes when the count stays at 1. **Reset write counts** zeroes them.
- **Reply delay:** fixed, or cycling (e.g. 500 ↔ 1000 ms every 10 s). Changes apply within 0.1 s.
- **Value schedules:** step a DP through values (e.g. dp81 through 5, 10 every 5 s).
- **Restart** keeps values; **Reset values** goes back to the file's starting values and zeroes the counts.
- **Request log:** every read, write, exception and delayed reply, with ms timestamps, FC, address, raw registers and
  decoded tags.

There's no login: anyone who can reach port 8080 can change simulator values (nothing else).

## Simulator behaviour

- **Endpoints:** one process per Studio port. A pty endpoint (`"endpoint": "pty:ttyCOM1"`) is a symlink at that path
  to a `/dev/pts/N`; ptcsw opens it like a serial port, and a restart swaps in a new pty that ptcsw finds by itself.
  A TCP endpoint (`"tcp:5020"`) listens on that port (and UDP).
- **Framing:** RTU or ASCII on a pty (from the file's `"framing"`, or detected); Modbus TCP, RTU-over-TCP or
  ASCII-over-TCP on TCP, detected per connection.
- **Slave ids:** every id has its own values. Ids listed in the file get their tags' starting values; any other id
  still answers (reading 0), and the log says so once. Unit 0 on RTU/ASCII is a broadcast: applied, no reply.
- **Function codes:** FC1-4 read, FC5, 6, 15 and 16 write; written values read back unchanged. Addresses are sent as
  is, 0-based (Studio HR 234 is register 234). Anything else gets exception 01.
- **Refused writes:** hr and coil 9000-9099 answer writes with exception 02 (change `"refuse"`/`"refuse_code"` in
  the file). Reads there work.
- **Delays** (`<name>.delay`, or the UI): `{"default": 300}`, `{"unit": {"2": 800}}`, `{"fc": {"4": 1200}}` (FC wins
  over unit, which wins over the default) and `{"cycle": {"ms": [500, 1000], "every_s": 10}}`. On a pty, requests
  that arrive while a delayed reply is pending are dropped, like a busy device (`ignored 8 bytes sent meanwhile`);
  on TCP they queue.
- **Schedules** (`<name>.schedule.json`, the file's `"schedules"`, or the UI):
  `[{"unit": 1, "tag": "dp81", "values": [5, 10], "every_s": 5}]`, or `"table"`/`"addr"`/`"type"` instead of a tag.
- **Persistence:** values and write counts are saved to `<name>.state.json` after every write and loaded on start.
  `./run.sh reset <name>` deletes it and restarts with the starting values.
- **Logs:** `<name>.log`, rotated to `<name>.log.1` at 20 MB.

## Commands

```bash
./run.sh com1 sp1          # start or restart (values kept)       ./run.sh all       every file in machines/
./run.sh stop com1         # ./run.sh stop all                    ./run.sh status
./run.sh reset com1        # starting values, counts zeroed       ./run.sh ui        web UI on :8080

python3 modbus_client.py show com1                     # every tag: value and the master's write count
python3 modbus_client.py set com1 dp81=12.5            # --unit 2 for another slave on the same port
python3 modbus_client.py read ir 90 --type FLOAT32 --sim com1
python3 modbus_client.py reset-counts com1
python3 check_sim.py com1                              # self-test as a master; then ./run.sh reset com1
```

`check_sim.py` refuses to run while another master read the simulator in the last 10 s, so it doesn't collide with
ptcsw. From your laptop, `./remote.sh <host> <command>` runs any of these on the PortaCON (`start`, `stop`, `reset`,
`status`, `ui`, `show`/`get`/`set`/`read`/`write`/`reset-counts`, `log`, `check`, `deploy`). Run
`ssh-copy-id portacon@192.168.10.97` once so SSH doesn't ask for a password, and copy `remote.hosts.example` to
`remote.hosts` for short names (`./remote.sh office start all`). Identify a PortaCON by IP: every unit's hostname is
`reComputer-R100x`.

## Simulator file format

```json
{"endpoint": "pty:ttyCOM1", "framing": "rtu", "control_port": 6101,
 "defaults": {"word_order": "little"},
 "devices": [{"unit": 1, "name": "port 1 device", "tags": [
   {"tag": "dp81", "table": "ir", "addr": 90, "type": "FLOAT32", "word_order": "little", "value": 81.5}]}],
 "schedules": [], "refuse": "hr:9000-9099,coil:9000-9099", "refuse_code": 2}
```

- `table`: `coil`, `di`, `ir` or `hr`. `addr`: the 0-based protocol address. `"addr": null` lists a tag without
  serving it (a placeholder for an address that isn't known yet).
- `type`: BOOL (coil/di), BIT (one bit of an hr/ir register, `"bit": 0-15`), INT16, UINT16, INT32, UINT32, FLOAT32,
  STRING (`words` = length in registers) or WORDS (raw registers).
- `word_order`: `little` puts the low word at the lower address (Studio `endianness: little`). `byte_order` (strings):
  `little` puts the first character in the low byte. `encoding`: `utf-8` by default.
- `control_port`: the 127.0.0.1-only Modbus TCP port the client and web UI use. TCP endpoints default to their port
  + 1000.
- The short form `{"port": 5020, "unit_id": 5, "tags": [...]}` is one device on a TCP endpoint (the ADL2012 files).

## ADL2012 machines

`machines/sp1`…`wind4` are generated from the ADL2012 PLC data sheet by `xlsx_to_init.py`. They're TCP endpoints
(ports 5020-5026) for Studio projects that read those machines over Modbus TCP.

| Machine | Name | Real PLC (IP:port, link) | Simulator port | Unit id | Tags |
|---|---|---|---|---|---|
| 권선기 1–4호기 | `wind1`–`wind4` | Yaskawa, 192.168.141.10/.20, 142.10/.20 : 10600/10601, WiFi bridge | 5022–5025 | 1–4 | 52 (1 pending) |
| SP접착기 1–2호기 | `sp1`, `sp2` | Mitsubishi Q, 192.168.141.30, 142.30 : 4000, WiFi bridge | 5020, 5021 | 5, 6 | 35 |
| 6면측정TP기 1호기 | `tp1` | Keyence, 192.168.141.40 : 5000, wired | 5026 | 7 | 51 |

`machines/address_map.csv` lists every tag's Modbus address (0-based and `4xxxxx`); it opens in Excel. The sheet's
vendor addresses map like this:

| PLC | Words | Bits |
|---|---|---|
| Mitsubishi Q | `D`/`R`/`W`/`ZR` n → hr n | `M`/`L`/`B`/`X`/`Y` n → coil n |
| Yaskawa (MEMOBUS numbering) | `MW` n → hr n | `MB` *word*+*hex bit* → coil word×16+bit (MB4400 → coil 7040); past 65535, bit *b* of hr *word* (MB00055336 → hr 5533 bit 6) |
| Keyence | `DM`/`ZF`/`EM`/`FM` n → hr n | `R`/`MR`/`LR`/`CR` n → coil n |

- 15 권선기 alarms are bits inside holding registers (type BIT): 단선 1-1~1-8 are hr 22400 bits 0–7, the rest hr 5533,
  5553, 5563 and 5613. Setting one changes only that bit.
- `현재 LOT No.` (MW520070) is past 65535, so it's a placeholder (`"addr": null`) until the vendor gives a usable
  address. Regenerating keeps the address you set.
- 32-bit values store the low word first; strings put the first character in the low byte (Mitsubishi `LH`), so
  `LOT2611A1` is `4F4C 3254 3136 4131 0031`. Confirmed for Mitsubishi; check tp1's `품명` (Keyence) against a real
  reading. Starting values are invented: counters above 65535 and odd-length LOT numbers, so swaps show.
- When the sheet changes, put the new .xlsx in `metadata/` and run `python3 xlsx_to_init.py`.

Real MELSEC MC, LS XGT FEnet and EtherNet/IP devices aren't simulated here: this simulator speaks Modbus only. The
original MC simulator is in `mc_legacy/`.

## Not done yet (brief "Should" items)

Building the device map is done from the Studio export (#10). Still to do: exception replies on chosen addresses
beyond the fixed refuse range (#11), going silent on demand (#12), and running as a service so a reboot doesn't stop
it (#14). Requests arriving during a delay are already dropped on a pty (#13), as the reference simulator does.
