# Notes on `~/modbus-sim` (the reference RTU simulator on CA111111)

`~/modbus-sim` on test unit CA111111 (192.168.10.97) is the Modbus RTU simulator used for the Oct 29 ptcsw tests
(porta_mw_p PR #68, datapoint writes). It behaved correctly against ptcsw, so this simulator follows it wherever
they overlap. Nothing here changes it; both run side by side with their own paths.

Its files there (read on 2026-11-09): `modbus_rtu_sim.py` (342 lines, stdlib only), `run.sh`, `delay_cycle.sh`,
`check_sim.py` (libmodbus through ctypes), `README.md`, and per-port `ttyRTU*.init.json`, `.state.json`, `.delay`,
`.log`, `.pid`.

## How it's set up

| | |
|---|---|
| Ports | `/home/portacon/modbus-sim/ttyRTU` (for rs485, COM2) and `ttyRTU2` (for rs232, COM1), one process each |
| Start | `./run.sh ttyRTU`, `./run.sh ttyRTU2` (setsid nohup, survives logout, **not a reboot**) |
| Starting values | `<name>.init.json`, raw words per unit and table: `{"1": {"ir": {"0": 33, "100": 44}}}` |
| Value cycles | `<name>.cycle`, one `--cycle` per line: `1:ir:123=5,10@5` (unit:table:addr=values@seconds) |
| Delays | `<name>.delay`: `{"default": 300, "unit": {"2": 800}, "fc": {"4": 1200}}`, re-read within 0.1 s |
| Delay cycles | `./delay_cycle.sh ttyRTU 5 1000 1500` rewrites the delay file every period (a background bash loop) |
| Refused writes | hr and coil 9000-9099, exception 02 (`--refuse`, `--refuse-code`) |
| Check | `python3 check_sim.py <port> <unit>` through libmodbus, the library ptcsw uses |

## Behaviour worth copying (and copied)

- **pty handling.** `os.openpty()`, `tty.setraw()` on the slave at once (so nothing is echoed back before ptcsw
  configures the port), and the slave fd is **kept open** so reads on the master don't fail with EIO while ptcsw has
  the port closed. The path is a symlink to `/dev/pts/N`, replaced atomically (`symlink` to `.tmp`, then
  `os.replace`). A restart makes a new pty; ptcsw reopens the path after a few failed reads and finds it.
- **Every unit id 1-247** answers, each with its own tables; unset addresses read 0. Unit 0 (broadcast) writes are
  applied with no reply.
- **Framing** by length from the function code (8 bytes for FC1-6, 9 + byte count for FC15/16). A 0.1 s silence with
  a partial frame drops it. A bad CRC drops the buffer. An unknown function code drops the buffer (no exception reply).
- **Delay precedence:** a function code entry wins over a unit entry, which wins over the default.
- **Busy device:** while it waits to send a delayed reply, it ignores what arrives, then logs
  `reply delayed 1000ms, ignored 8 bytes sent meanwhile`. This is what reproduces ptcsw's late-reply mix-up (PR #66).
- **Read-only tables:** FC2 and FC4 only read; di and ir are set from the init file or cycles.
- **Log lines:** `unit=1 fc=4 read ir[100..100] = [44]`, with ms timestamps.

## What this simulator does differently, and why

| Reference | Here | Why |
|---|---|---|
| Restart clears values | Values and write counts are kept (`<name>.state.json`); `./run.sh reset` restores the starting values | Brief requirement 4 |
| Raw words in init files | Typed tags (`FLOAT32`, word order, ...) from the Studio export | Testers think in "HR 234 FLOAT32 = 5.12" |
| Cycles in a separate file, raw values | Schedules by tag in engineering units, editable live from the UI | Requirement 5 |
| Delay cycle by a bash loop rewriting the file | `"cycle"` inside the delay file, handled by the simulator | One process to stop; no stray loops |
| One delay change logged every period, forever | Same (`reply delay now 1000 ms (cycle)`), but the log rotates at 20 MB | ttyRTU.log had reached 61 MB and ttyRTU2.log 101 MB by Nov 4 |
| No write counts | Per-DP write counts from the master | Requirement 8 ("no repeat writes") |
| Values changed by editing files | A control port (127.0.0.1 only) for the client and web UI | Never a second master on the pty (requirement 9) |
| Unknown function code: buffer dropped, no reply | Exception 01 | Standard Modbus; logged |

## State on 2026-11-09

The PortaCON had rebooted (uptime 2 h 50 min), so both reference simulators and their delay-cycle loops were dead,
and their pid files stale. Their last delay settings were 1000/1500 ms (ttyRTU) and 500/1000 ms (ttyRTU2). The
application.json on the unit pointed COM3 at `/dev/ttyAMA4` (4800 baud) with one device, `dv65`, so neither pty was
in use.
