# CLAUDE.md: setting up a test for the user

The user describes Porta Studio DPs. Your job is to make the simulator on their PortaCON serve those DPs with the
values they want, tell them what to enter in Porta Studio, and, after they deploy, check what ptcsw actually reads.

## Where things are

| What | Where |
|---|---|
| PortaCON | `ssh portacon@<ip>` (office unit: CA111111, `192.168.10.97`). Never use `sudo`. |
| Simulator | `~/modbus-plc-simulator` on the PortaCON, installed by `./remote.sh portacon@<ip> install` |
| One config per port | `machines/<name>.init.json` on the PortaCON (gitignored; never edit the copies in this repo) |
| Web monitor | `http://portacon-simulator.local:8080` (mDNS name, `./run.sh ui --name <x>` to change) or `http://<ip>:8080`; read-only |
| ptcsw | `/home/portacon/workspace/ptcsw` (root, runs under `ptc_auto_service`) |

**Never touch** ptcsw, `~/workspace/application.json` or `~/modbus-sim`. The user deploys from Porta Studio.

## Ports and names

| Porta Studio | Config file | Endpoint | Porta Studio setting |
|---|---|---|---|
| Modbus RTU on COM1 / 2 / 3 | `com1` / `com2` / `com3` | `pty:ttyCOM1` (control port 6101 / 6102 / 6103) | 프로필 → COM1 설정 → 포트 경로 = `/home/portacon/modbus-plc-simulator/ttyCOM1` |
| Modbus TCP | `tcp1`, `tcp2`, … | `tcp:5020`, `tcp:5021`, … | device IP = `<ip>`, port = 5020 … |

## Turning the user's DP list into a config

Ask for anything missing: **port, slave ID, and per DP: register type, address, data type, report interval, read
FC (and write FC if both), and the value to send.** Don't guess the data type or the value.

| Register type | `table` | Read FC | Write FC |
|---|---|---|---|
| Coil | `coil` | 01 | 05 / 15 |
| Discrete input | `di` | 02 | – |
| Input register | `ir` | 04 | – |
| Holding register | `hr` | 03 | 06 / 16 |

Types: `BOOL`, `BIT`, `INT16`, `UINT16`, `INT32`, `UINT32`, `FLOAT32`, `STRING` (needs `words`), `WORDS`.
32-bit types take `"word_order": "little"` (low word first, Porta Studio's `LE`) or `"big"`.

```json
{
 "machine": "DEMO THERMOMETER 2",
 "endpoint": "tcp:5020",
 "devices": [{"unit": 1, "name": "DEMO THERMOMETER 2", "tags": [
   {"tag": "dp160", "table": "ir", "addr": 200, "type": "FLOAT32", "word_order": "little", "value": 0.125, "report_ms": 1000},
   {"tag": "dp163", "table": "ir", "addr": 220, "type": "INT16", "value": 12345, "report_ms": 1000}
 ]}]
}
```

For RTU use `"endpoint": "pty:ttyCOM1", "framing": "rtu", "control_port": 6101`.

## Doing it

1. Back up first: `cp machines/com1.init.json machines/com1.init.json.bak-<what>`.
2. Write the file, then `./run.sh com1` (start or restart; also adds it to autostart).
3. Values are kept across restarts in `com1.state.json`, so also set them:
   `python3 modbus_client.py set com1 dp160=0.125 dp163=12345`
4. Verify: `python3 modbus_client.py show com1` and `python3 modbus_client.py read ir 200 2 --sim com1`.
5. Tell the user what to enter in Porta Studio. **The data type there must match the config**, or values come out
   garbled (a FLOAT32 read as INT16 gives the low word, e.g. 0.125 → 0).

`./run.sh status`, `./run.sh stop com1`, `./run.sh reset com1` (back to the file's values).

## Checking after a deploy

- Deployed? `stat -c %y ~/workspace/ptcsw` changes on every deploy; wait for it before checking.
- Is ptcsw pointed at the simulator? `cat ~/workspace/application.json`. `interfaces.rs232.port` must be the
  `ttyCOM1` path; if it says `/dev/ttyAMA2`, Porta Studio's 포트 경로 was reset and RTU reads time out.
- What the build expects: the device config is compiled in.
  `strings -n 3 ~/workspace/ptcsw | grep -A12 '"string_id": "dp160"'` shows type, address, word_count, endian.
- What ptcsw stored: `journalctl -u ptc_auto_service --since "-3 s" --no-pager | grep '\[Store\]'`.
  `dev` = device slot, `dp` = position in that device. `type=3` bits are an IEEE double (decode with
  `struct.unpack('>d', bytes.fromhex(...))`); `type=5` bits are a signed integer. Conversion expressions are already
  applied there.
- Simulator side: `tail com1.log` (lines like `READ dp160 = 0.125 by ptcsw`).

Report a table of device × DP with the value sent, the value stored and ✅/❌, and say why for each ❌.

## Other protocols (not in this repo)

On CA111111 only: MC protocol in `~/mc-sim` (`./run.sh <name> <port>`, `mc_client.py`) and EtherNet/IP in
`~/eip-sim` (`eip_attr_sim.py --model <file>`, 127.0.0.1:44818 only, since the PortaCON's own EIP service holds
the LAN port).
The web monitor shows running MC and EIP simulators under the **TCP** tab, next to the Modbus TCP devices. Give MC
tags a `"device": "<name>"` and EIP attributes a `"name": "dp163 (<device name>)"` so they show under the right name.
Once the monitor has seen one running, it remembers the command in `external.json`, so a stopped one stays listed
and the page's **Start simulator / Stop simulator** button (top right) covers it too. That button starts or stops
every simulator at once (Modbus, MC, EIP); each press is logged in `ui.log` with the browser's IP. MC and EIP
simulators don't come back after a reboot.
