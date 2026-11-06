# MC protocol PLC simulator (ADL2012 SP접착기)

Simulates the two Mitsubishi Q PLCs from the ADL2012 address sheet (`ADL2012 PLC 어드레스` tab), so ptcsw's `mc_protocol` driver can be QA'd on this gateway.

| Simulated PLC | Real PLC | Simulator address | Starting values |
|---|---|---|---|
| SP접착기 1호기 | 192.168.141.30:4000 | `127.0.0.1:4000` (also `192.168.10.97:4000` on the LAN) | `sp1.init.json` |
| SP접착기 2호기 | 192.168.142.30:4000 | `127.0.0.1:4001` (also `192.168.10.97:4001`) | `sp2.init.json` |

Both PLCs listen on the same TCP+UDP port. The frame format is detected per request (3E or 4E, binary or ASCII; Q-style or iQ-R-style device spec), so any `use_ascii`/`use_udp`/`series` setting works without restarting.

## Starting values

| Tag | Device | Type | 1호기 | 2호기 |
|---|---|---|---|---|
| 가동중 | M5 | BOOL | 1 | 0 |
| 투입수 | D1104 | INT32 | 120345 | 70210 |
| 양품수 | D1106 | INT32 | 120308 | 70173 |
| 전체 불량수 | D1108 | INT32 | 37 | 37 |
| 도포형상 불량수 | D1110 | INT32 | 12 | 12 |
| 도포타임아웃 SP수 | D1114 | INT32 | 3 | 3 |
| DR탑재 불량수 | D1116 | INT32 | 9 | 9 |
| 히터테이블 인덱스 타임아웃수 | D1158 | INT32 | 2 | 2 |
| 히터 이재 취출미스수 | D1172 | INT32 | 5 | 5 |
| 수납 이재 취출미스수 | D1168 | INT32 | 6 | 6 |
| LOT No. | R771 (5 words) | STRING | `LOT2611A01` (10 chars, fills all 5 words, no NUL) | `LOT2611B7` (9 chars, odd length) |
| 품명 | D8960 (8 words) | STRING | `ADL2012-SP-A01` (14 chars) | `ADL2012-SP-B2` (13 chars, odd length) |
| 택타임 | D1000 | UINT16 | 42 | 38 |

INT32 is stored the Mitsubishi way: low word at the lower address. Strings are stored the Mitsubishi way: first character in the low byte (`LH`), NUL-padded.

## Running

- `./run.sh sp1 4000` and `./run.sh sp2 4001` start or restart a simulator. A restart reloads the starting values and drops written ones.
- The simulators run in the background without sudo. They survive logout but stop on a reboot; run both commands again after one.
- `<name>.log` logs every request, with words in hex and as LH text. `<name>.state.json` holds the current memory.
- `echo '{"default": 300}' > sp1.delay` delays every reply by 300 ms; it's re-read on change. `rm sp1.delay` turns it off.
- Writes to D9000–D9099 and M9000–M9099 are refused with end code 0xC051, for testing write-error handling.

## Changing values while ptcsw polls

MC is request/response per connection, so a second client doesn't disturb ptcsw.

```bash
python3 mc_client.py --port 4000 write R771 --type STRING --words 5 "ABC"
python3 mc_client.py --port 4000 read R771 5 --type STRING
python3 mc_client.py --port 4000 write D1104 --type INT32 123456
python3 mc_client.py --port 4000 write M5 --bit 0
python3 mc_client.py --port 4000 write D8960 4241 0043 0000 0000 0000 0000 0000 0000   # raw words
```

Add `--ascii`, `--udp` or `--iqr` to use another frame format. `./check_sim.sh ~/mc-sim 4000` runs the full self-test, but it writes test values, so restart the simulator afterwards.
