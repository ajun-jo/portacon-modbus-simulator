#!/usr/bin/env python3
"""Web monitor for the simulators: which DPs each simulated port serves, their live values, what IoTLink should show
for them, and a live log of who read or changed which DP. Its only control is a Start/Stop button per port.

  python3 webui.py                      http://<this machine>:8080
  python3 webui.py --port 8081 --bind 127.0.0.1

Apart from starting and stopping a port's simulator (the same as ./run.sh <name> and ./run.sh stop <name>), it
changes nothing. Configuration (values, delays, schedules, auto-reset, loading a Studio project) is done from the terminal with run.sh, modbus_client.py, studio_to_init.py and the files next to each
simulator. Values come from each simulator's <name>.state.json and ptcsw's reads from <name>.reads.json, so the
monitor never talks Modbus and never shows up in the request log.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from modbus_plc_sim import decode_value, load_config  # noqa: E402
import studio_to_init  # noqa: E402

# A master's read in the request log, e.g.
# 2026-11-10T02:23:04.420 READ   dp82 = 1   by ptcsw   coil 70   [FC01 coil 70 x21 · unit 1 · pty RTU]
READ_LINE = re.compile(r"^(\S+) READ .* by (?!manual)\S+ .*\[FC0[1-4] [^\]]* · unit (\d+) · (tcp \S+|udp \S+|pty) "
                       r"(TCP|RTU|ASCII)\]")


def machine_names():
    return sorted(f[:-10] for f in os.listdir(os.path.join(HERE, "machines")) if f.endswith(".init.json"))


def path(name, ext):
    return os.path.join(HERE, name + ext)


def pid_of(name):
    try:
        with open(path(name, ".pid")) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def tail(file, n, max_bytes=262144):
    try:
        with open(file, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]  # first line is probably cut
    return lines[-n:]


def last_poll(name):
    """The newest read request from a master in the log: who asked, with which framing, how many seconds ago.
    (check_sim.py uses this too, to stay off a port ptcsw is polling.)"""
    for line in reversed(tail(path(name, ".log"), 400)):
        m = READ_LINE.match(line)
        if m:
            try:
                ago = (datetime.now() - datetime.fromisoformat(m.group(1))).total_seconds()
            except ValueError:
                return None
            return {"ago": round(ago, 1), "peer": m.group(3), "framing": m.group(4), "unit": int(m.group(2))}
    return None


def read_json(file, default):
    try:
        with open(file, encoding="utf-8") as f:
            text = f.read().strip()
        return json.loads(text) if text else default
    except (OSError, ValueError):
        return default


def port_view(name):
    """One simulated port: where it listens, whether it runs and is polled, and every DP with its live value."""
    cfg = load_config(os.path.join(HERE, "machines", name + ".init.json"))
    ep = cfg["endpoint"]
    pid = pid_of(name)
    now = time.time()
    st = read_json(path(name, ".state.json"), {}) if pid else {}
    st = st if isinstance(st, dict) else {}
    units, writes = st.get("units", {}), st.get("writes", {})
    reads = read_json(path(name, ".reads.json"), {}) if pid else {}
    silent = (read_json(path(name, ".silent.json"), {}) or {}).get("until") or 0
    dps = []
    for unit, dev in sorted(cfg["devices"].items()):
        tables, counts, seen = units.get(str(unit), {}), writes.get(str(unit), {}), reads.get(str(unit), {})
        for t in dev["tags"]:
            row = {"dp": t["tag"], "unit": unit, "device": dev["name"], "table": t["table"], "addr": t["addr"],
                   "bit": t.get("bit") if t["type"] == "BIT" else None, "type": t["type"], "words": t["words"],
                   "pending": t["pending"], "expression": t.get("expression") or "", "report_ms": t.get("report_ms"),
                   "write_only": bool(t.get("write_only")), "writes": counts.get(t["tag"], 0)}
            if pid and st and not t["pending"]:
                m = tables.get(t["table"], {})
                words = [m.get(str(t["addr"] + i), 0) for i in range(t["words"])]
                try:
                    row["value"] = decode_value(t["type"], words, t)
                except Exception as ex:  # e.g. bytes that aren't valid text
                    row["value"], row["error"] = None, str(ex)
                if row.get("value") is not None and not isinstance(row["value"], str):
                    try:
                        row["iotlink"] = studio_to_init.apply_expression(row["expression"], row["value"])
                    except ValueError:
                        pass
                elif isinstance(row.get("value"), str):
                    row["iotlink"] = row["value"]
            if t["tag"] in seen:
                row["read_ago"] = round(now - seen[t["tag"]][0], 1)
                row["reads"] = seen[t["tag"]][1]
            dps.append(row)
    port = cfg.get("studio_port") or name.upper()
    return {"name": name, "title": cfg.get("machine", name), "port": port,
            "group": port if ep["kind"] == "pty" else "TCP", "controllable": True,
            "proto": "Modbus TCP" if ep["kind"] == "tcp" else "Modbus " + (cfg.get("framing") or "RTU").upper(),
            "kind": ep["kind"], "link": ep.get("path"), "tcp": ep.get("port"), "framing": cfg.get("framing"),
            "running": pid is not None, "poll": last_poll(name) if pid else None,
            "silent_left": round(silent - now) if silent > now else 0, "dps": dps}


def overview():
    ports = []
    for n in machine_names():
        try:
            ports.append(port_view(n))
        except SystemExit as ex:  # a broken file shouldn't hide the others
            ports.append({"name": n, "title": n, "port": n.upper(), "error": str(ex), "running": False, "dps": []})
    for sim in other_sims().values():
        try:
            ports.append(sim["view"]())
        except Exception as ex:  # a half-written file shouldn't break the page
            ports.append({"name": sim["name"], "title": sim["name"], "port": "TCP", "group": "TCP",
                          "proto": sim["proto"], "error": str(ex), "running": True, "dps": []})
    return {"ports": ports, "now": time.time()}


# ---------------------------------------------------------------- MC protocol and EtherNet/IP simulators
# Those simulators aren't part of this repo (mc_plc_sim.py, eip_attr_sim.py). The monitor shows the ones that are
# running on this machine, found by process name, read-only: values from their state/model files and ptcsw's
# requests from their logs, turned into the same log line format as the Modbus simulator's.

def _proc_args(pid):
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            args = f.read().decode("utf-8", "replace").split("\0")
        cwd = os.readlink("/proc/%s/cwd" % pid)
        out = os.readlink("/proc/%s/fd/1" % pid)
    except OSError:
        return None
    return args, cwd, out if out.startswith("/") and os.path.isfile(out) else None


def _opt(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def other_sims():
    """{name: {"name", "proto", "view": fn, "log": fn}} for each running MC / EtherNet/IP simulator."""
    sims = {}
    if not os.path.isdir("/proc"):
        return sims
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        got = _proc_args(pid)
        if not got:
            continue
        args, cwd, out = got
        script = next((os.path.basename(a) for a in args[:3] if a.endswith(".py")), "")
        if script == "mc_plc_sim.py":
            init = os.path.join(cwd, _opt(args, "--init", ""))
            name = "mc-" + os.path.basename(init).split(".")[0]
            sims[name] = _mc_sim(name, init, os.path.join(cwd, _opt(args, "--state", "")), out,
                                 int(_opt(args, "--port", 0) or 0))
        elif script == "eip_attr_sim.py":
            model = os.path.join(cwd, _opt(args, "--model", "model.json"))
            name = "eip-" + os.path.basename(model).split(".")[0]
            sims[name] = _eip_sim(name, model, out, _opt(args, "--host", "0.0.0.0"), int(_opt(args, "--port", 44818)))
    return sims


def _iso_ago(ts, now):
    try:
        return round(now - datetime.fromisoformat(ts).timestamp(), 1)
    except ValueError:
        return None


def _ext_view(name, proto, port, title, dps, lines, now, conv):
    """A port-like dict for the page, with read_ago per DP and the newest poll, from converted log lines."""
    last, poll = {}, None
    for line in lines:
        for e in conv(line):
            m = re.match(r"^(\S+) READ\s+(.+?) = ", e)
            if m:
                last[m.group(2)] = m.group(1)
                poll = m.group(1)
    for d in dps:
        if d["dp"] in last:
            d["read_ago"] = _iso_ago(last[d["dp"]], now)
    ago = _iso_ago(poll, now) if poll else None
    return {"name": name, "title": title, "port": "TCP", "group": "TCP", "proto": proto, "kind": "tcp",
            "tcp": port, "framing": None, "running": True, "controllable": False,
            "poll": {"ago": ago} if ago is not None else None, "silent_left": 0, "dps": dps}


MC_TYPES_WORDS = {"INT16": 1, "UINT16": 1, "INT32": 2, "UINT32": 2, "FLOAT32": 2}
MC_LINE = re.compile(r"^(\S+) (BIN|ASCII)/(3E|4E) (read|write) ([A-Z]+)([0-9A-F]+) (word|bit) x(\d+) = (\S.*?)(?: text\(|$)")


def _mc_sim(name, init, state, log, port):
    def tags():
        return [t for t in read_json(init, []) if isinstance(t, dict)]

    def nwords(t):
        return int(t.get("words", 1)) if t["type"] in ("STRING", "WORDS") else MC_TYPES_WORDS.get(t["type"], 1)

    def decode(t, words):
        opts = {"word_order": "little", "byte_order": "big" if t.get("byte_order", "LH").upper() == "HL" else "little"}
        return decode_value(t["type"], words, opts)

    def conv(line):
        """One MC request line -> one log line per DP it covers."""
        m = MC_LINE.match(line)
        if not m:
            return []
        ts, kind, frame, op, dev, start, unit, count, data = m.groups()
        start, count = int(start, 16) if dev in ("X", "Y", "B", "W") else int(start), int(count)
        is_bit = unit == "bit"
        vals = list(data.strip()) if is_bit else [int(w, 16) for w in re.findall(r"[0-9A-F]{4}", data.split("]")[0])]
        out = []
        for t in tags():
            if t.get("dev") != dev or (t["type"] == "BOOL") != is_bit:
                continue
            off, n = t["addr"] - start, (1 if is_bit else nwords(t))
            if off < 0 or off + n > count or off + n > len(vals):
                continue
            try:
                v = int(vals[off]) if is_bit else decode(t, vals[off:off + n])
            except Exception:
                continue
            verb = "READ" if op == "read" else "WRITE"
            vtxt = '"%s"' % v if isinstance(v, str) else ("%.7g" % v if isinstance(v, float) else str(v))
            out.append("%s %-6s %-28s by %-11s %-14s [%s %s %s%d x%d · unit 1 · tcp]"
                       % (ts, verb, "%s = %s" % (t["tag"], vtxt), "ptcsw", "%s%s" % (dev, t["addr"]),
                          frame, op, dev, start, count))
        return out or ["%s %s" % (ts, line.split(" ", 1)[1])]

    def view():
        now = time.time()
        st = read_json(state, {})
        words, bits = st.get("words", {}), st.get("bits", {})
        dps = []
        for t in tags():
            row = {"dp": t.get("tag", "%s%s" % (t["dev"], t["addr"])), "unit": 1,
                   "device": t.get("device") or "MC Protocol", "table": t["dev"], "addr": t["addr"], "bit": None,
                   "type": t["type"], "words": nwords(t), "pending": False, "expression": "", "report_ms": None,
                   "write_only": False, "writes": 0}
            try:
                if t["type"] == "BOOL":
                    row["value"] = int(bits.get(t["dev"], {}).get(str(t["addr"]), 0))
                else:
                    m = words.get(t["dev"], {})
                    row["value"] = decode(t, [int(m.get(str(t["addr"] + i), 0)) for i in range(nwords(t))])
                row["iotlink"] = row["value"]
            except Exception as ex:
                row["value"], row["error"] = None, str(ex)
            dps.append(row)
        dev = next((d["device"] for d in dps), "MC Protocol")
        return _ext_view(name, "MC Protocol", port, dev, dps, tail(log, 400) if log else [], now, conv)

    return {"name": name, "proto": "MC Protocol", "view": view,
            "log": lambda n: [x for line in (tail(log, n) if log else []) for x in conv(line)][-n:]}


EIP_LINE = re.compile(r"^(\S+) (\S+) (GET|SET) (0x[0-9a-f]+)/(\d+)/(0x[0-9a-f]+)(?: data=[0-9a-f ]*)? -> status (0x[0-9a-f]+)\s*(.*)$")


def _eip_sim(name, model, log, host, port):
    def num(x):
        return int(x, 0) if isinstance(x, str) else int(x)

    def entries():
        """[(key, dp name, device, type, value, where)] for every scalar attribute and block field."""
        out = []
        for a in read_json(model, {}).get("attributes", []):
            key = (num(a["class"]), num(a["instance"]), num(a["attribute"]))
            where = "%#04x/%d/%#04x" % key
            for f in (a.get("fields") or [a]):
                label = f.get("name") or where
                m = re.match(r"^(\S+)\s*\((.*)\)$", label)
                dp, device = (m.group(1), m.group(2)) if m else (label, "EtherNet/IP")
                at = where + ("+%d" % f["offset"] if "offset" in f else "")
                out.append((key, dp, device, f.get("type"), f.get("value"), at))
        return out

    def conv(line):
        m = EIP_LINE.match(line)
        if not m:
            return ["%s %s" % tuple(line.split(" ", 1))] if " " in line else []
        ts, peer, op, c, i, a, status, desc = m.groups()
        key = (int(c, 16), int(i), int(a, 16))
        hits = [e for e in entries() if e[0] == key]
        if status != "0x00" or not hits:
            return ["%s %s %s %s/%s/%s -> status %s %s" % (ts, peer, op, c, i, a, status, desc)]
        verb = "READ" if op == "GET" else "WRITE"
        vals = dict(re.findall(r"(\S+?)@\d+=(\S+?)[,\]]", desc)) if "block" in desc else {}
        out = []
        for _, dp, _, typ, value, at in hits:
            v = vals.get(dp, desc.split("=", 1)[1] if "=" in desc and "block" not in desc else value)
            out.append("%s %-6s %-28s by %-11s %-14s [%s %s · unit 1 · tcp %s]"
                       % (ts, verb, "%s = %s" % (dp, v), "ptcsw", at, op, "%s %s" % (typ, at), peer))
        return out

    def view():
        now = time.time()
        dps = [{"dp": dp, "unit": 1, "device": device, "table": "", "addr": at, "bit": None, "type": typ,
                "words": 1, "pending": False, "expression": "", "report_ms": None, "write_only": False, "writes": 0,
                "value": value, "iotlink": value} for _, dp, device, typ, value, at in entries()]
        dev = next((d["device"] for d in dps), "EtherNet/IP")
        return _ext_view(name, "EtherNet/IP", port, dev, dps, tail(log, 400) if log else [], now, conv)

    return {"name": name, "proto": "EtherNet/IP", "view": view, "host": host,
            "log": lambda n: [x for line in (tail(log, n) if log else []) for x in conv(line)][-n:]}


class Handler(BaseHTTPRequestHandler):
    server_version = "ModbusSimMonitor"

    def log_message(self, fmt, *args):  # keep the console quiet; errors still print
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = self.path.split("?", 1)
        parts = [p for p in url[0].split("/") if p]
        query = dict(q.split("=", 1) for q in url[1].split("&") if "=" in q) if len(url) > 1 else {}
        try:
            if not parts:
                return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
            if parts == ["api", "overview"]:
                return self.send(200, overview())
            if len(parts) == 3 and parts[:2] == ["api", "log"]:
                n = min(int(query.get("n", 300)), 2000)
                if parts[2] in machine_names():
                    return self.send(200, {"lines": tail(path(parts[2], ".log"), n)})
                other = other_sims().get(parts[2])
                if other:
                    return self.send(200, {"lines": other["log"](n)})
                return self.send(404, {"error": "no simulator %r" % parts[2]})
            self.send(404, {"error": "not found"})
        except Exception as ex:
            self.send(500, {"error": "%s: %s" % (type(ex).__name__, ex)})

    def refuse(self):
        self.send(405, {"error": "the monitor only starts and stops simulators; configure them from the terminal"})

    def do_POST(self):
        """POST /api/sim/<name>/start or /stop: the same as ./run.sh <name> and ./run.sh stop <name>."""
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) != 4 or parts[:2] != ["api", "sim"] or parts[3] not in ("start", "stop"):
            return self.refuse()
        name, action = parts[2], parts[3]
        if name not in machine_names():
            return self.send(404, {"error": "no simulator %r" % name})
        try:
            args = ["./run.sh", name] if action == "start" else ["./run.sh", "stop", name]
            p = subprocess.run(args, cwd=HERE, capture_output=True, text=True, timeout=60)
            out = (p.stdout + p.stderr).strip()
        except Exception as ex:
            return self.send(500, {"error": "%s: %s" % (type(ex).__name__, ex)})
        running = pid_of(name) is not None
        if (action == "start") != running:
            return self.send(500, {"error": out or "%s didn't %s" % (name, action), "running": running})
        return self.send(200, {"output": out, "running": running})

    do_PUT = do_PATCH = do_DELETE = refuse


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PortaCON Simulator</title>
<style>
:root {
  /* soft charcoal, off-white text, muted accents: low contrast between surfaces, enough for text */
  --bg: #1c1e23; --panel: #23262c; --panel-2: #282b32; --line: #2f333b; --line-2: #3a3f48;
  --text: #d9dce2; --muted: #9399a5; --dim: #6d7380;
  --blue: #8db0de; --blue-bg: rgba(141,176,222,.12);
  --amber: #d4b06e; --amber-bg: rgba(212,176,110,.13);
  --violet: #b4a3d9; --violet-bg: rgba(180,163,217,.13);
  --teal: #86c3b9; --teal-bg: rgba(134,195,185,.12);
  --red: #d98c8c; --red-bg: rgba(217,140,140,.13);
  --green: #8fc9a0; --green-bg: rgba(143,201,160,.13);
  --off: #7c828e; --off-bg: rgba(124,130,142,.15);
  --flash: rgba(212,176,110,.16);
  --shadow: 0 1px 2px rgba(0,0,0,.18);
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo", "Noto Sans KR", Roboto, sans-serif;
  color-scheme: dark;
}
@media (prefers-color-scheme: light) {
  :root {
    /* warm paper instead of stark white */
    --bg: #f4f2ee; --panel: #fbfaf8; --panel-2: #f1efea; --line: #e4e0d8; --line-2: #d6d1c7;
    --text: #34373d; --muted: #6e717a; --dim: #9a9ca3;
    --blue: #4f74a8; --blue-bg: rgba(79,116,168,.09);
    --amber: #9c7a35; --amber-bg: rgba(176,140,66,.12);
    --violet: #7a66a8; --violet-bg: rgba(122,102,168,.09);
    --teal: #4c8a80; --teal-bg: rgba(76,138,128,.09);
    --red: #b05f5f; --red-bg: rgba(176,95,95,.09);
    --green: #4f8a5f; --green-bg: rgba(79,138,95,.11);
    --off: #9a9ca3; --off-bg: rgba(154,156,163,.14);
    --flash: rgba(176,140,66,.13);
    --shadow: 0 1px 2px rgba(60,50,30,.06);
    color-scheme: light;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 var(--sans); }
button { font: inherit; color: inherit; }
.wrap { max-width: 1240px; margin: 0 auto; padding: 0 20px 48px; }

/* top bar */
header { position: sticky; top: 0; z-index: 5; background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(10px); border-bottom: 1px solid var(--line); }
.bar { max-width: 1240px; margin: 0 auto; padding: 14px 20px; display: flex; align-items: center; gap: 14px; }
.logo { width: 30px; height: 30px; border-radius: 8px; display: grid; place-items: center;
  background: linear-gradient(135deg, var(--blue), var(--violet)); color: #fff; font: 700 13px var(--mono); }
.brand h1 { margin: 0; font-size: 15px; font-weight: 650; letter-spacing: .1px; }
.brand p { margin: 0; color: var(--muted); font-size: 12px; }
.spacer { flex: 1; }
.live { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
.pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 0 var(--green);
  animation: pulse 2s infinite; }
.live.paused .pulse, .live.down .pulse { animation: none; background: var(--off); }
.live.down .pulse { background: var(--red); }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--green) 55%, transparent); }
  70% { box-shadow: 0 0 0 7px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
.btn { border: 1px solid var(--line-2); background: var(--panel); border-radius: 8px; padding: 6px 11px; cursor: pointer;
  font-size: 12px; color: var(--muted); }
.btn:hover { color: var(--text); border-color: var(--dim); }

/* port tabs */
.tabs { display: flex; gap: 8px; flex-wrap: wrap; margin: 22px 0 18px; }
.tab { display: flex; align-items: center; gap: 10px; padding: 9px 14px; border-radius: 10px; cursor: pointer;
  border: 1px solid var(--line); background: var(--panel); text-align: left; }
.tab:hover { border-color: var(--line-2); }
.tab.on { border-color: var(--blue); background: var(--blue-bg); }
.tab b { font-weight: 650; font-size: 13px; }
.tab small { display: block; color: var(--muted); font-size: 11px; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--off); }
.dot.ok { background: var(--green); } .dot.idle { background: var(--amber); } .dot.bad { background: var(--red); }

/* device picker: one device at a time when a port has several */
.devpick { display: flex; flex-wrap: wrap; gap: 8px; margin: -8px 0 20px; }
.devpick:empty { display: none; }
.dv { display: flex; align-items: center; gap: 10px; padding: 8px 14px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; text-align: left; }
.dv:hover { border-color: var(--line-2); }
.dv.on { border-color: color-mix(in srgb, var(--violet) 55%, transparent); background: var(--violet-bg); }
.dv b { font-weight: 600; font-size: 13px; }
.dv small { color: var(--muted); font-size: 11px; }
.proto { font: 600 11px var(--sans); color: var(--violet); background: var(--violet-bg); padding: 2px 8px; border-radius: 6px; }

/* port header */
.port { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 8px 24px; margin-bottom: 14px; }
.port h2 { margin: 0; font-size: 20px; font-weight: 650; }
.port .sub { color: var(--muted); font-size: 13px; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; }
.mono { font-family: var(--mono); }
.path { font: 12px var(--mono); color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
  padding: 3px 8px; border-radius: 6px; cursor: copy; }
.path:hover { border-color: var(--line-2); }
.ctl { margin-left: auto; }
.act { font: 600 13px var(--sans); border-radius: 9px; padding: 8px 16px; cursor: pointer; border: 1px solid; }
.act.stop { color: var(--red); background: var(--red-bg); border-color: color-mix(in srgb, var(--red) 40%, transparent); }
.act.stop:hover { background: color-mix(in srgb, var(--red) 22%, transparent); }
.act.start { color: #fff; background: var(--green); border-color: var(--green); }
.act.start:hover { filter: brightness(1.08); }
.act:disabled { opacity: .6; cursor: wait; }
.notice { border: 1px solid var(--line); background: var(--panel); border-radius: 10px; padding: 12px 14px;
  margin-bottom: 14px; color: var(--muted); }
.notice.warn { border-color: color-mix(in srgb, var(--amber) 45%, var(--line)); background: var(--amber-bg); color: var(--text); }
.notice.bad { border-color: color-mix(in srgb, var(--red) 45%, var(--line)); background: var(--red-bg); color: var(--text); }
.notice code { font: 12px var(--mono); background: var(--panel-2); padding: 1px 6px; border-radius: 5px; }

/* section titles */
.sec { display: flex; align-items: baseline; gap: 10px; margin: 26px 0 10px; }
.sec h3 { margin: 0; font-size: 12px; text-transform: uppercase; letter-spacing: .9px; color: var(--muted); font-weight: 650; }
.sec span { color: var(--dim); font-size: 12px; }

/* DP cards */
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
.dev + .dev { margin-top: 18px; }
.devh { display: flex; align-items: center; gap: 10px; margin: 0 0 10px; padding: 8px 12px; border-radius: 10px; cursor: pointer;
  border: 1px dashed var(--line-2); }
.devh:hover { border-color: var(--dim); } .devh.on { border-style: solid; border-color: var(--blue); background: var(--blue-bg); }
.devh b { font-size: 14px; font-weight: 650; }
.slave { font: 700 11px var(--mono); color: var(--violet); background: var(--violet-bg); padding: 2px 8px; border-radius: 6px; }
.devm { margin-left: auto; display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
.sl { font: 700 10px var(--mono); color: var(--violet); background: var(--violet-bg); padding: 1px 5px; border-radius: 4px; margin-right: 6px; }
.card { position: relative; background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 14px 16px 12px; cursor: pointer; transition: border-color .15s, transform .15s; }
.card:hover { border-color: var(--line-2); }
.card.focus { border-color: var(--blue); box-shadow: 0 0 0 3px var(--blue-bg); }
.card.flash { animation: flash 1.6s ease-out; }
@keyframes flash { 0% { background: var(--flash); } 100% { background: var(--panel); } }
.card .top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.card .name { font: 650 15px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card .where { font: 12px var(--mono); color: var(--muted); white-space: nowrap; }
.card .val { margin: 10px 0 6px; font: 600 30px/1.1 var(--mono); letter-spacing: -.5px; word-break: break-all; }
.card .val.str { font-size: 20px; }
.card .val.none { color: var(--dim); font-size: 18px; font-weight: 500; }
.bool { display: inline-flex; align-items: center; gap: 10px; }
.bool i { font-style: normal; font: 600 11px var(--sans); text-transform: uppercase; letter-spacing: .8px;
  padding: 3px 8px; border-radius: 999px; }
.bool.on { color: var(--green); } .bool.on i { background: var(--green-bg); }
.bool.off { color: var(--off); } .bool.off i { background: var(--off-bg); }
.card .meta { display: flex; flex-wrap: wrap; gap: 4px 10px; color: var(--muted); font-size: 12px; }
.card .iot { margin-top: 8px; font-size: 12px; color: var(--muted); }
.card .iot b { color: var(--text); font: 600 12px var(--mono); }
.card .foot { display: flex; justify-content: space-between; gap: 8px; margin-top: 10px; padding-top: 10px;
  border-top: 1px solid var(--line); font-size: 12px; color: var(--muted); }
.card .foot span { display: flex; align-items: center; gap: 6px; }
.w-pill { color: var(--amber); background: var(--amber-bg); padding: 1px 8px; border-radius: 999px; font-weight: 600; }

/* log */
.logbox { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
.logbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--line);
  background: var(--panel-2); }
.seg { display: inline-flex; border: 1px solid var(--line-2); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; background: transparent; padding: 5px 11px; font-size: 12px; color: var(--muted); cursor: pointer; }
.seg button.on { background: var(--blue-bg); color: var(--blue); font-weight: 600; }
.chip { display: inline-flex; align-items: center; gap: 6px; font: 600 12px var(--mono); color: var(--blue);
  background: var(--blue-bg); border-radius: 999px; padding: 3px 6px 3px 10px; }
.chip button { border: 0; background: transparent; color: inherit; cursor: pointer; padding: 0 4px; font-size: 14px; line-height: 1; }
.legend { display: flex; gap: 12px; margin-left: auto; font-size: 11px; color: var(--muted); flex-wrap: wrap; }
.legend span { display: flex; align-items: center; gap: 5px; }
.legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.rows { max-height: 560px; overflow: auto; font: 13px/1.35 var(--mono); }
.row { display: grid; grid-template-columns: 96px 66px minmax(110px, 1.1fr) minmax(90px, 1fr) 104px minmax(96px, .8fr) minmax(0, 1.4fr);
  gap: 10px; align-items: center; padding: 7px 14px; border-bottom: 1px solid var(--line); }
.row:last-child { border-bottom: 0; }
.row:hover { background: var(--panel-2); }
.row.new { animation: newrow 1.4s ease-out; }
@keyframes newrow { from { background: var(--blue-bg); } to { background: transparent; } }
.t { color: var(--dim); font-size: 12px; }
.verb { justify-self: start; font: 700 10.5px var(--sans); letter-spacing: .7px; padding: 3px 7px; border-radius: 5px; }
.verb.READ { color: var(--blue); background: var(--blue-bg); }
.verb.WRITE { color: var(--amber); background: var(--amber-bg); }
.verb.SET { color: var(--violet); background: var(--violet-bg); }
.verb.RESET { color: var(--red); background: var(--red-bg); }
.dpn { font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.dpn:hover { color: var(--blue); }
.dpn.nodp { color: var(--red); font-weight: 500; cursor: default; }
.v { font-weight: 650; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.v.on { color: var(--green); } .v.off { color: var(--off); } .v.str { color: var(--teal); }
.v small { color: var(--dim); font-weight: 400; font-size: 12px; }
.by { justify-self: start; font: 600 11px var(--sans); padding: 2px 8px; border-radius: 999px; white-space: nowrap; }
.by.ptcsw, .by.master { color: var(--blue); background: var(--blue-bg); }
.by.schedule { color: var(--violet); background: var(--violet-bg); }
.by.manual { color: var(--teal); background: var(--teal-bg); }
.by.auto-reset { color: var(--red); background: var(--red-bg); }
.addr { color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.note { color: var(--dim); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.note b { color: var(--amber); font-weight: 600; }
.ev { display: grid; grid-template-columns: 96px 1fr; gap: 10px; padding: 7px 14px; border-bottom: 1px solid var(--line);
  color: var(--muted); font-size: 12px; }
.ev.bad { color: var(--red); background: var(--red-bg); }
.ev.warn { color: var(--amber); }
.ev.old { color: var(--dim); }
.empty { padding: 28px; text-align: center; color: var(--muted); font-family: var(--sans); }
.more { position: sticky; top: 0; z-index: 2; display: none; justify-content: center; padding: 6px; }
.more button { border: 0; border-radius: 999px; padding: 4px 12px; background: var(--blue); color: #fff; font-size: 12px; cursor: pointer; }
.more.show { display: flex; }
footer { margin-top: 26px; color: var(--dim); font-size: 12px; text-align: center; }
footer code { font: 12px var(--mono); }
.toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg);
  padding: 7px 14px; border-radius: 8px; font-size: 12px; opacity: 0; transition: opacity .2s; pointer-events: none; }
.toast.show { opacity: 1; }
.path { overflow-wrap: anywhere; max-width: 100%; }
.port > div, .port .sub { min-width: 0; }
@media (max-width: 860px) {
  .row { grid-template-columns: 70px 54px minmax(0, 1fr) minmax(0, 1fr); gap: 8px; padding: 7px 10px; }
  .row .t, .ev .t { font-size: 11px; }
  .row .by, .row .addr, .row .note { display: none; }
  .legend { display: none; }
  .ev { grid-template-columns: 70px 1fr; padding: 7px 10px; }
}
@media (max-width: 520px) {
  .wrap, .bar { padding-left: 16px; padding-right: 16px; }
  .brand p, #liveText { display: none; }
  .cards { grid-template-columns: 1fr; }
  .row .t { font-size: 10.5px; }
}

/* easy on the eyes: lighter weights, more air, quieter badges and borders */
html, body { font-size: 14.5px; line-height: 1.55; -webkit-font-smoothing: antialiased; }
.wrap { padding-bottom: 64px; }
.logo { background: var(--panel-2); color: var(--muted); border: 1px solid var(--line-2); font-weight: 600; }
.brand h1 { font-weight: 600; }
.tabs { margin: 28px 0 22px; gap: 10px; }
.tab { padding: 10px 16px; border-color: transparent; background: var(--panel); box-shadow: var(--shadow); }
.tab.on { border-color: color-mix(in srgb, var(--blue) 45%, transparent); }
.tab b, .port h2, .card .name, .dpn, .devh b { font-weight: 600; }
.sec { margin: 34px 0 12px; }
.sec h3 { font-weight: 600; letter-spacing: .6px; }
.cards { gap: 14px; }
.card { border-color: transparent; box-shadow: var(--shadow); padding: 16px 18px 14px; border-radius: 14px; }
.card:hover { border-color: var(--line-2); }
.card .val { font-weight: 500; font-size: 28px; color: var(--text); }
.card .foot { border-top-color: var(--line); }
.bool i { font-weight: 600; letter-spacing: .5px; }
.devh { border-style: solid; border-color: transparent; background: var(--panel-2); }
.devh:hover { border-color: var(--line-2); }
.logbox { border-color: transparent; box-shadow: var(--shadow); border-radius: 14px; }
.logbar { background: var(--panel); border-bottom-color: var(--line); padding: 12px 14px; }
.seg { border-color: var(--line); }
.rows { font-size: 13px; }
.row { padding: 9px 16px; border-bottom-color: color-mix(in srgb, var(--line) 60%, transparent); }
.verb { font-weight: 600; background: transparent !important; padding: 2px 0; letter-spacing: .6px; }
.verb::before { content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 7px;
  vertical-align: 1px; background: currentColor; }
.v { font-weight: 500; }
.by { font-weight: 500; background: transparent !important; padding: 0; }
.slave, .sl { font-weight: 600; }
.note b { font-weight: 500; }
.act { font-weight: 500; }
.act.start { color: var(--bg); }
.notice { border-color: transparent; box-shadow: var(--shadow); }
.path { background: var(--panel-2); border-color: transparent; }
footer { margin-top: 34px; }
</style>
</head>
<body>
<header><div class="bar">
  <div class="logo">MB</div>
  <div class="brand"><h1>PortaCON Simulator</h1><p>Live monitor · configure from the terminal</p></div>
  <div class="spacer"></div>
  <div class="live" id="live"><span class="pulse"></span><span id="liveText">Connecting…</span></div>
  <button class="btn" id="pause" title="Freeze the screen to read it">Pause</button>
</div></header>

<div class="wrap">
  <nav class="tabs" id="tabs"></nav>
  <div class="devpick" id="devpick"></div>
  <div id="port"></div>

  <div class="sec"><h3>Data points</h3><span id="dpCount"></span></div>
  <div id="cards"></div>

  <div class="sec"><h3>Live log</h3><span>newest first · click a DP to follow it</span></div>
  <div class="logbox">
    <div class="logbar">
      <div class="seg" id="mode">
        <button data-m="all" class="on">Everything</button>
        <button data-m="changes">Changes only</button>
        <button data-m="reads">Reads only</button>
      </div>
      <div class="seg" id="slaveSeg" style="display:none"></div>
      <span id="focusChip"></span>
      <div class="legend">
        <span><i style="background:var(--blue)"></i>READ · ptcsw read it</span>
        <span><i style="background:var(--amber)"></i>WRITE · ptcsw wrote it</span>
        <span><i style="background:var(--violet)"></i>SET · changed in the simulator</span>
        <span><i style="background:var(--red)"></i>RESET · auto-reset to 0</span>
      </div>
    </div>
    <div class="rows" id="rows"><div class="more" id="more"><button>New activity ↑</button></div><div id="list"></div></div>
  </div>

  <footer>Only Start/Stop works here. To change values, delays or schedules, use the terminal:
    <code>./run.sh status</code> · <code>python3 modbus_client.py set com1 dp81=0</code></footer>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
let ports = [], sel = null, focus = null, mode = "all", paused = false, slaveF = null, multi = false;
let devSel = {}, cur = null;  // devSel: chosen device per tab ("<sim>|<unit>"); cur: {p, unit} being shown
let lastVals = {}, seenLines = new Set(), firstLog = true, lastOk = 0;

try { sel = localStorage.getItem("sel"); mode = localStorage.getItem("mode") || "all"; devSel = JSON.parse(localStorage.getItem("devSel") || "{}"); } catch (e) {}
if (location.hash.length > 1) {  // #COM3 opens that port, #TCP/mc-qa a device on it
  const [g, sim] = decodeURIComponent(location.hash.slice(1)).split("/");
  sel = g; if (sim) devSel[g] = sim.includes("|") ? sim : sim + "|1";
}
document.querySelectorAll("#mode button").forEach(b => b.classList.toggle("on", b.dataset.m === mode));

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 1600); }
function ago(s) { if (s == null) return "never"; if (s < 1) return "just now"; if (s < 60) return s.toFixed(s < 10 ? 1 : 0) + " s ago"; if (s < 3600) return Math.round(s / 60) + " min ago"; return Math.round(s / 3600) + " h ago"; }
function where(d) {
  if (d.pending) return "no address";
  if (!["coil", "di", "ir", "hr"].includes(d.table)) return (d.table || "") + d.addr;  // MC "D200", EIP "0x67/1/0x325"
  return d.table + " " + d.addr + (d.bit != null ? " bit " + d.bit : "");
}
function fmt(v, type) {
  if (v === undefined || v === null) return null;
  if (typeof v === "number" && type === "FLOAT32") return String(+v.toPrecision(7));
  return typeof v === "string" ? '"' + v + '"' : String(v);
}
function isBool(type) { return type === "BOOL" || type === "BIT"; }
function portState(p) {
  if (p.error) return ["bad", "Config error"];
  if (!p.running) return ["bad", "Stopped"];
  if (p.silent_left) return ["idle", "Silent " + p.silent_left + " s"];
  if (p.poll && p.poll.ago < 30) return ["ok", "ptcsw reading"];
  return ["idle", p.poll ? "Last read " + ago(p.poll.ago) : "Not read yet"];
}

async function api(u) { const r = await fetch(u, {cache: "no-store"}); if (!r.ok) throw new Error((await r.json()).error || r.statusText); return r.json(); }

function groups() { return [...new Set(ports.map(p => p.group || p.port))]; }
function devicesOf(g) {
  return ports.filter(p => (p.group || p.port) === g).flatMap(p => {
    const units = [...new Set(p.dps.map(d => d.unit))].sort((a, b) => a - b);
    return (units.length ? units : [null]).map(u => {
      const dps = p.dps.filter(d => d.unit === u);
      const seen = dps.map(d => d.read_ago).filter(x => x != null);
      return {key: p.name + "|" + u, p, unit: u, name: dps.length ? dps[0].device : p.title, proto: p.proto || "", dps,
              last: seen.length ? Math.min(...seen) : null};
    });
  }).sort((a, b) => String(a.name).localeCompare(String(b.name), undefined, {numeric: true}));
}
function devState(d) {
  const [cls, txt] = portState(d.p);
  if (cls !== "ok" || d.unit == null) return [cls, txt];
  return d.last != null && d.last < 30 ? ["ok", "ptcsw reading"] : ["idle", d.last != null ? "Last read " + ago(d.last) : "Not read yet"];
}
function renderTabs() {
  $("#tabs").innerHTML = groups().map(g => {
    const devs = devicesOf(g), sts = devs.map(devState);
    const cls = sts.some(x => x[0] === "bad") ? "bad" : sts.every(x => x[0] === "ok") ? "ok" : sts.some(x => x[0] === "ok") ? "ok" : "idle";
    const nDp = devs.reduce((n, d) => n + d.dps.length, 0);
    const txt = devs.length > 1 ? `${devs.length} devices` : (sts[0] || ["", ""])[1];
    return `<button class="tab ${g === sel ? "on" : ""}" data-n="${esc(g)}"><span class="dot ${cls}"></span>
      <span><b>${esc(g)}</b><small>${esc(txt)} · ${nDp} DP${nDp === 1 ? "" : "s"}</small></span></button>`;
  }).join("");
  document.querySelectorAll(".tab").forEach(b => b.onclick = () => select(b.dataset.n));
}
function renderDevPick(devs) {
  const el = $("#devpick");
  if (devs.length < 2) { el.innerHTML = ""; return; }
  el.innerHTML = devs.map(d => {
    const [cls, txt] = devState(d);
    const at = d.p.kind === "pty" ? `slave ${d.unit}` : d.p.tcp ? `:${d.p.tcp}` : "";
    return `<button class="dv ${cur && d.key === cur.key ? "on" : ""}" data-k="${esc(d.key)}" title="${esc(txt)}"><span class="dot ${cls}"></span>
      <span><b>${esc(d.name)}</b> <small>${esc(d.proto)}${at ? " · " + esc(at) : ""} · ${d.dps.length} DP${d.dps.length === 1 ? "" : "s"}</small></span></button>`;
  }).join("");
  el.querySelectorAll(".dv").forEach(b => b.onclick = () => pickDev(b.dataset.k));
}
function pickDev(k) {
  devSel[sel] = k; try { localStorage.setItem("devSel", JSON.stringify(devSel)); } catch (e) {}
  history.replaceState(null, "", "#" + sel + "/" + k.replace(/\|1$/, ""));
  focus = null; $("#focusChip").innerHTML = ""; lastVals = {}; firstLog = true; tick(true);
}

function renderPort(p, d) {
  const [cls, txt] = devState(d);
  const ep = p.kind === "pty" ? `<span class="path" title="Click to copy" data-copy="${esc(p.link)}">${esc(p.link)}</span>`
                              : `<span class="mono">TCP port ${esc(p.tcp)}</span>`;
  let notice = "";
  if (p.error) notice = `<div class="notice bad">This simulator's file has an error: ${esc(p.error)}</div>`;
  else if (!p.running) notice = `<div class="notice bad"><b>${esc(p.name)} is stopped</b>, so ptcsw gets no answers on this port. Use <b>Start</b> above when you want it back.</div>`;
  else if (p.silent_left) notice = `<div class="notice warn">Simulating a dead device: no replies for another ${p.silent_left} s.</div>`;
  else if (!p.poll) notice = `<div class="notice">Waiting for ptcsw. If nothing arrives, check that 포트 경로 for ${esc(p.port)} in Porta Studio is exactly the path above.</div>`;
  $("#port").innerHTML = `<div class="port"><div><h2>${esc(d.name)} <span style="color:var(--muted);font-weight:500;font-size:15px">${esc(p.port)}${d.unit != null && p.kind === "pty" ? " · slave " + d.unit : ""}</span></h2></div>
    <div class="sub"><span><span class="dot ${cls}" style="display:inline-block;margin-right:6px"></span>${esc(txt)}</span>
    ${d.proto ? `<span class="proto">${esc(d.proto)}</span>` : ""}${ep}</div>
    ${p.error || p.controllable === false ? "" : `<div class="ctl">${p.running
      ? `<button class="act stop" id="simBtn" data-a="stop">■ Stop ${esc(p.port)}</button>`
      : `<button class="act start" id="simBtn" data-a="start">▶ Start ${esc(p.port)}</button>`}</div>`}</div>${notice}`;
  const b = $("#simBtn"); if (b) b.onclick = () => simAction(p, b.dataset.a, b);
  document.querySelectorAll("[data-copy]").forEach(e => e.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(e.dataset.copy); toast("Path copied"); });
}

async function simAction(p, action, btn) {
  const others = devicesOf(p.group || p.port).filter(d => d.p.name === p.name).length;
  if (action === "stop" && !confirm(`Stop ${p.port} (${p.name})?\n\nptcsw gets no answers ${others > 1 ? "from all " + others + " devices on this simulator" : "from it"} until you start it again, so its values stop updating in IoTLink. It also stays stopped after a reboot.`)) return;
  btn.disabled = true; btn.textContent = action === "stop" ? "Stopping…" : "Starting…";
  try {
    const r = await fetch(`/api/sim/${encodeURIComponent(p.name)}/${action}`, {method: "POST"});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    toast(`${p.port} ${action === "stop" ? "stopped" : "started"}`);
  } catch (e) { toast(`Couldn't ${action} ${p.port}: ${e.message}`); }
  tick(true);
}

function renderCards(p, d) {
  multi = false;
  const dps = d.dps;
  $("#dpCount").textContent = `${dps.length} on ${d.name}`;
  if (!dps.length) { $("#cards").innerHTML = `<div class="empty">No DPs on this device.</div>`; renderSlaveSeg([]); return; }
  $("#cards").innerHTML = `<div class="cards">${dps.map(cardHtml.bind(null, p)).join("")}</div>`;
  document.querySelectorAll(".card").forEach(c => c.onclick = () => setFocus(focus === c.dataset.dp ? null : c.dataset.dp));
  renderSlaveSeg([]);
}

function renderSlaveSeg(units) {
  const el = $("#slaveSeg");
  if (units.length < 2) { el.innerHTML = ""; el.style.display = "none"; return; }
  el.style.display = "";
  el.innerHTML = `<button data-u="" class="${slaveF == null ? "on" : ""}">All devices</button>` +
    units.map(u => `<button data-u="${u}" class="${slaveF === u ? "on" : ""}">Slave ${u}</button>`).join("");
  el.querySelectorAll("button").forEach(b => b.onclick = () => setSlave(b.dataset.u === "" ? null : +b.dataset.u));
}

function cardHtml(p, d) {
  {
    const key = p.name + "/" + d.unit + "/" + d.dp;
    const val = d.value;
    const changed = key in lastVals && JSON.stringify(lastVals[key]) !== JSON.stringify(val);
    lastVals[key] = val;
    let big;
    if (val === undefined || val === null) big = `<div class="val none">${p.running ? (d.pending ? "no address" : "—") : "stopped"}</div>`;
    else if (isBool(d.type)) big = `<div class="val"><span class="bool ${val ? "on" : "off"}">${val}<i>${val ? "on" : "off"}</i></span></div>`;
    else big = `<div class="val ${typeof val === "string" ? "str" : ""}">${esc(fmt(val, d.type))}</div>`;
    const iot = d.iotlink !== undefined && !d.write_only
      ? `<div class="iot">IoTLink should show <b>${esc(fmt(d.iotlink, d.type))}</b>${d.expression ? ` <span class="mono">(${esc(d.expression)})</span>` : ""}</div>`
      : d.write_only ? `<div class="iot">Write-only: not shown in IoTLink</div>` : "";
    const fresh = d.read_ago != null && d.read_ago < 10;
    return `<div class="card ${focus === d.dp ? "focus" : ""} ${changed ? "flash" : ""}" data-dp="${esc(d.dp)}">
      <div class="top"><span class="name" title="${esc(d.dp)}">${esc(d.dp)}</span><span class="where">${esc(where(d))}</span></div>
      ${big}
      <div class="meta"><span>${esc(d.type)}</span>${d.report_ms ? `<span>report ${d.report_ms / 1000} s</span>` : ""}</div>
      ${iot}
      <div class="foot">${d.write_only && d.read_ago == null ? `<span>write-only · never read</span>` : `<span><span class="dot ${fresh ? "ok" : d.read_ago != null ? "idle" : ""}"></span>Read ${esc(ago(d.read_ago))}</span>`}
        ${d.writes ? `<span class="w-pill" title="Writes from ptcsw">${d.writes}× written</span>` : `<span>not written</span>`}</div>
    </div>`;
  }
}

// 2026-11-10T02:23:04.420 READ   dp82 = 1   by ptcsw   coil 70   [FC01 coil 70 x21 · unit 1 · pty RTU]  +10.004 s since last write
const LINE = /^(\S+) (READ|WRITE|SET|RESET)\s+(.+?) = (.*?)\s+by (\S+)\s+(.*?)\s+\[([^\]]*)\]\s*(.*)$/;
function parse(l) {
  const m = LINE.exec(l);
  if (!m) return {raw: l, time: (l.match(/^\S+T(\S+)/) || [])[1] || "", text: l.replace(/^\S+ /, "")};
  const u = /unit (\d+)/.exec(m[7]);
  return {time: m[1].split("T")[1] || m[1], verb: m[2], dp: m[3], value: m[4], by: m[5], addr: m[6], detail: m[7], note: m[8], unit: u ? +u[1] : null, raw: l};
}
function typeOf(dp) { const d = cur && cur.dps.find(x => x.dp === dp); return d ? d.type : null; }
function valueHtml(e) {
  let v = e.value, extra = "";
  const was = v.match(/^(.*) \(was (.*)\)$/);
  if (was) { v = was[1]; extra = ` <small>was ${esc(was[2])}</small>`; }
  const t = typeOf(e.dp);
  const cls = isBool(t) ? (v === "1" ? "on" : "off") : v.startsWith('"') ? "str" : "";
  return `<span class="v ${cls}">${esc(v)}${extra}</span>`;
}
function rowHtml(e, isNew) {
  if (!e.verb) {
    const cls = / exception |malformed|garbled|error|bad /.test(e.raw) ? "bad" : /silent|SILENT|delay|ignored/.test(e.raw) ? "warn" : / (read|write) /.test(e.raw) ? "old" : "";
    return `<div class="ev ${cls}"><span class="t">${esc(e.time)}</span><span>${esc(e.text)}</span></div>`;
  }
  const nodp = e.dp === "(no DP)";
  const note = e.note ? e.note.replace(/^(\+[\d.]+ s)/, "<b>$1</b>") : "";
  return `<div class="row ${isNew ? "new" : ""}" title="${esc(e.detail)}">
    <span class="t">${esc(e.time)}</span><span class="verb ${e.verb}">${e.verb}</span>
    <span class="dpn ${nodp ? "nodp" : ""}" ${nodp ? 'title="No DP at this address in the project"' : `data-dp="${esc(e.dp)}"`}>${multi && e.unit != null ? `<span class="sl">S${e.unit}</span>` : ""}${esc(e.dp)}</span>
    ${valueHtml(e)}<span class="by ${esc(e.by)}">by ${esc(e.by)}</span><span class="addr">${esc(e.addr)}</span>
    <span class="note">${note || `<span style="color:var(--dim)">${esc(e.detail)}</span>`}</span></div>`;
}
function keep(e) {
  if (!e.verb) return !focus && mode !== "reads" && !/ (connect|connected|disconnect|disconnected|speaks Modbus \w+)$|RegisterSession/.test(e.raw)
    && (slaveF == null || !/unit \d+/.test(e.raw) || new RegExp("unit " + slaveF + "\\b").test(e.raw));
  if (e.by === "manual" && e.verb === "READ") return false;  // the monitor's and modbus_client.py's own reads
  if (focus && e.dp !== focus) return false;
  if (slaveF != null && e.unit !== slaveF) return false;
  if (mode === "changes") return e.verb !== "READ";
  if (mode === "reads") return e.verb === "READ";
  return true;
}
let pending = null;
function renderLog(lines, force) {
  const box = $("#rows");
  if (!force && box.scrollTop > 40) { pending = lines; $("#more").classList.add("show"); return; }
  pending = null; $("#more").classList.remove("show");
  const entries = lines.map(parse).filter(keep).reverse().slice(0, 300);
  const fresh = new Set(lines);
  $("#list").innerHTML = entries.length ? entries.map(e => rowHtml(e, !firstLog && !seenLines.has(e.raw))).join("")
    : `<div class="empty">${focus ? "Nothing for " + esc(focus) + " in the recent log." : slaveF != null ? "Nothing from " + esc(cur ? cur.name : "this device") + " in the recent log." : "No activity yet."}</div>`;
  seenLines = fresh; firstLog = false;
  document.querySelectorAll(".dpn[data-dp]").forEach(e => e.onclick = () => setFocus(e.dataset.dp));
}

function setFocus(dp) {
  focus = dp;
  $("#focusChip").innerHTML = dp ? `<span class="chip">${esc(dp)}<button title="Show all DPs">×</button></span>` : "";
  if (dp) $("#focusChip button").onclick = () => setFocus(null);
  document.querySelectorAll(".card").forEach(c => c.classList.toggle("focus", c.dataset.dp === dp));
  firstLog = true; tick(true);
}
function setSlave(u) {
  slaveF = u; firstLog = true;
  document.querySelectorAll(".devh").forEach(h => h.classList.toggle("on", +h.dataset.u === u));
  document.querySelectorAll("#slaveSeg button").forEach(b => b.classList.toggle("on", b.dataset.u === (u == null ? "" : String(u))));
  tick(true);
}
function select(n) { sel = n; focus = null; $("#focusChip").innerHTML = ""; lastVals = {}; firstLog = true; try { localStorage.setItem("sel", n); } catch (e) {} history.replaceState(null, "", "#" + n); tick(true); }

async function tick(force) {
  if (paused && !force) return;
  try {
    const o = await api("/api/overview");
    ports = o.ports; lastOk = Date.now();
    if (!ports.length) {
      $("#tabs").innerHTML = ""; $("#port").innerHTML = `<div class="notice">No simulated ports yet. Load a Studio project from the terminal: <code>python3 studio_to_init.py &lt;project.json&gt;</code>, then <code>./run.sh all</code>.</div>`;
      $("#cards").innerHTML = ""; $("#list").innerHTML = ""; return;
    }
    const gs = groups();
    if (!gs.includes(sel)) { const old = ports.find(p => p.name === sel); sel = old ? (old.group || old.port) : gs[0]; }
    const devs = devicesOf(sel);
    const d = devs.find(x => x.key === devSel[sel]) || devs[0];
    if (!cur || cur.key !== d.key) { lastVals = {}; firstLog = true; }
    cur = d; slaveF = d.unit;
    const p = d.p;
    renderTabs(); renderDevPick(devs); renderPort(p, d); renderCards(p, d);
    const lg = await api(`/api/log/${encodeURIComponent(p.name)}?n=600`);
    renderLog(lg.lines, force);
    $("#live").className = "live" + (paused ? " paused" : "");
    $("#liveText").textContent = paused ? "Paused" : "Live · " + new Date().toLocaleTimeString();
  } catch (e) {
    $("#live").className = "live down"; $("#liveText").textContent = "Can't reach the simulator";
  }
}

document.querySelectorAll("#mode button").forEach(b => b.onclick = () => {
  mode = b.dataset.m; try { localStorage.setItem("mode", mode); } catch (e) {}
  document.querySelectorAll("#mode button").forEach(x => x.classList.toggle("on", x === b)); firstLog = true; tick(true);
});
$("#pause").onclick = () => { paused = !paused; $("#pause").textContent = paused ? "Resume" : "Pause"; tick(true); };
$("#more button").onclick = () => { $("#rows").scrollTop = 0; if (pending) renderLog(pending, true); };
$("#rows").addEventListener("scroll", () => { if ($("#rows").scrollTop <= 40 && pending) renderLog(pending, true); });
tick(true); setInterval(() => tick(false), 1000);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print("%s web UI on http://%s:%d" % (time.strftime("%Y-%m-%dT%H:%M:%S"), a.bind, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
