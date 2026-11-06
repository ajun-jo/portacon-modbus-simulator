# Modbus PLC simulator (ADL2012)

Fake Modbus PLCs for testing a server that polls PLCs: start one, point the server at it, change values, and check what the server shows.
It needs Python 3 and nothing else. Every machine is on Ethernet, so the simulator is TCP only: one port per machine.

## Machines

The machine files in `machines/` are generated from the ADL2012 PLC data sheet, `metadata/ADL2012_설비별_PLC데이터_정리표_260928.xlsx` (the `ADL2012 PLC 어드레스` and `ADL2012 알람리스트` tabs):

| Machine | Name | Real PLC (IP:port, link) | Simulator port | Unit id | Tags |
|---|---|---|---|---|---|
| 권선기 1–4호기 | `wind1`–`wind4` | Yaskawa, 192.168.141.10/.20, 142.10/.20 : 10600/10601, WiFi bridge | 5022–5025 | 1–4 | 52 (1 pending) |
| SP접착기 1–2호기 | `sp1`, `sp2` | Mitsubishi Q, 192.168.141.30, 142.30 : 4000, WiFi bridge | 5020, 5021 | 5, 6 | 35 |
| 6면측정TP기 1호기 | `tp1` | Keyence, 192.168.141.40 : 5000, wired | 5026 | 7 | 51 |

Each machine file records its real PLC under `real_plc` (make, IP, port, connection), for reference only.

`machines/address_map.csv` lists the Modbus address of every tag, 0-based and 1-based (`4xxxxx`), for setting up your server. It opens in Excel.

**Address mapping.** The sheet holds each vendor's own addresses, and Modbus only has 0–65535 per table, so the generator maps them like this:

| PLC | Words | Bits |
|---|---|---|
| Mitsubishi Q | `D`/`R`/`W`/`ZR` n → hr n | `M`/`L`/`B`/`X`/`Y` n → coil n |
| Yaskawa (MEMOBUS numbering) | `MW` n → hr n | `MB` *word*+*hex bit* → coil word×16+bit (MB4400 → coil 7040); when that's past 65535, bit *b* of hr *word* (MB00055336 → hr 5533 bit 6) |
| Keyence | `DM`/`ZF`/`EM`/`FM` n → hr n | `R`/`MR`/`LR`/`CR` n → coil n |

- **Bit-in-register tags** (type `BIT`): on every 권선기, 15 alarms live in holding-register bits. 단선 1-1~1-8 are hr 22400 bits 0–7; the rest are hr 5533, 5553, 5563 and 5613. Reading one reads the register and returns its bit; `set` changes only that bit (read-modify-write). `show` writes them as `hr 5533.6`, and the CSV has a `bit` column.
- **Pending tags:** `현재 LOT No.` (MW520070) is past 65535, so it's a placeholder with `"addr": null`. It shows as pending and isn't served. Once the vendor gives a usable address, put it in `"addr"` in `machines/wind1.init.json` (…`wind4`) and restart. Regenerating keeps the address you set.
- **Byte layout** (`defaults` in each file): 32-bit values store the low word at the lower address. Strings are consecutive words with the first character in the **low byte** of each word (Mitsubishi `LH`), so `LOT2611A1` is `4F4C 3254 3136 4131 0031`. This is confirmed for Mitsubishi; check tp1's `품명` (Keyence DM1160) against a real reading.
- **Starting values are invented.** Counters are above 65535 so word-order mistakes show. Every `LOT No.` has an odd length, so a byte swap is obvious: the registers above read as `'OL2T16A1'`. Alarms are off and 가동중 is on.

**When the sheet changes,** put the new .xlsx in `metadata/` and run `python3 xlsx_to_init.py`. It regenerates the files but keeps each tag's value, any address you gave a pending tag, and port, unit_id and defaults. To change how addresses map, edit `MAPPING` in `xlsx_to_init.py`.

## Protocols

- **Modbus TCP, RTU-over-TCP and ASCII-over-TCP** on each machine's TCP port, and UDP on the same port. The framing is detected per connection, so you can switch your server's protocol without restarting the simulator.
- With RTU or ASCII framing, the simulator answers only its own unit id. It stays silent for other ids and executes broadcasts (unit 0) without replying. Modbus TCP answers any unit id unless `--tcp-unit-check` is given.
- Supported function codes are 01–06, 15 and 16. Any other code gets exception 01.

## Running

```bash
./run.sh sp1                                   # start or restart one machine (a restart reloads the starting values)
for m in sp1 sp2 wind1 wind2 wind3 wind4 tp1; do ./run.sh $m; done   # all seven
./run.sh status
./run.sh stop sp1
```

- `sp1.log` logs every request and decodes the tags in it, e.g. `read hr 1104 x2 = [D619 0001]  {투입수=120345}` or `read hr 22400 x1 = [0081]  {단선 1-1=1, …, 단선 1-8=1}`. Use it to see exactly what your server asked for.
- `echo '{"default": 300}' > sp1.delay` delays every reply by 300 ms. The file is re-read when it changes; `rm sp1.delay` turns the delay off.
- Writes to hr 9000–9099 and coil 9000–9099 get exception 04, for testing write-error handling.

## Changing values while your server polls

The client talks Modbus TCP to the simulator's port. That works whatever framing your server uses, since the port accepts all three.

```bash
python3 modbus_client.py show sp1                                  # every tag, its PLC address and value
python3 modbus_client.py get wind1 투입수 "히터칩1 START 온도"
python3 modbus_client.py set wind1 "단선 1-3=1"                     # one alarm bit in hr 22400
python3 modbus_client.py read hr 5533 --bit 6 --sim wind1          # one bit of a register, raw
python3 modbus_client.py set sp1 투입수=5000 가동중=0 비상정지알람=1 "LOT No.=LOT0002"
python3 modbus_client.py read hr 1104 2 --sim sp1                  # raw registers in hex
python3 modbus_client.py write hr 1104 --type INT32 -5 --sim sp1
python3 modbus_client.py write ir 10 --type FLOAT32 3.5 --sim sp1  # input registers and discrete inputs too
```

`./check_sim.sh sp1` runs a self-test. It writes test values, so restart the simulator afterwards.

## Machine file format

```json
{"tag": "투입수", "group": "data", "table": "hr", "addr": 1104, "type": "INT32", "value": 120345, "plc_addr": "D1104"}
```

- `table`: `coil` (0x), `di` (1x), `ir` (3x) or `hr` (4x).
- `addr`: the 0-based protocol address. Register "40001" in a device manual is `hr` 0.
- `type`: BOOL (coil/di), BIT (one bit of an hr/ir register; `"bit": 0-15`), INT16, UINT16, INT32, UINT32, FLOAT32, STRING (`words` = length in registers) or WORDS (a list of raw registers).
- `"addr": null` marks a pending tag: listed by `show`, not served.
- `defaults` sets the byte layout for every tag, and any tag can override it:
  - `word_order`: `little` puts the low word at the lower address. `big` puts the high word first.
  - `byte_order`: `little` puts a string's first character in the low byte.
  - `encoding`: `utf-8` by default. Use `cp949` for Korean text if your server expects it.
- `group`, `plc_addr` and `note` are informational. `show` displays `plc_addr`.

The original Mitsubishi MC protocol simulator is kept in `mc_legacy/`.
