#!/usr/bin/env python3
"""Web UI for the simulators: open it in a browser to start and stop them, see and change values, set reply
delays and value schedules, count the master's writes, and watch what ptcsw asks for.

  python3 webui.py                      http://<this machine>:8080
  python3 webui.py --port 8081 --bind 127.0.0.1

It runs next to the simulators (on the PortaCON: ./remote.sh <host> ui) and controls them through run.sh.
Values come from each simulator's <name>.state.json, so the UI's refreshes don't show up in the request log.
Changes go through each simulator's control port, never through a serial port, so the UI is never a second
master, and its writes aren't counted. There is no login: anyone who can reach the port can change simulator
values (nothing else).
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
from modbus_client import Client, ModbusException  # noqa: E402
from modbus_plc_sim import (  # noqa: E402
    FC_RESET_COUNTS, decode_value, encode_value, find_tag, load_config, set_bit, table_name, word_count,
)

READ_LINE = re.compile(r"^(\S+) (tcp \S+|udp \S+|pty) (TCP|RTU|ASCII) unit (\d+) fc0[1-4] read ")


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
    """The newest read request from a master in the log: who asked, with which framing, how many seconds ago."""
    for line in reversed(tail(path(name, ".log"), 400)):
        m = READ_LINE.match(line)
        if m:
            try:
                ago = (datetime.now() - datetime.fromisoformat(m.group(1))).total_seconds()
            except ValueError:
                return None
            return {"ago": round(ago, 1), "peer": m.group(2), "framing": m.group(3), "unit": int(m.group(4))}
    return None


def read_json(file, default):
    try:
        with open(file, encoding="utf-8") as f:
            text = f.read().strip()
        return json.loads(text) if text else default
    except (OSError, ValueError):
        return default


def write_json(file, data):
    tmp = file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, file)


class Machine:
    def __init__(self, name):
        self.name = name
        self.file = os.path.join(HERE, "machines", name + ".init.json")
        self.cfg = load_config(self.file)

    def summary(self):
        c, ep = self.cfg, self.cfg["endpoint"]
        pid = pid_of(self.name)
        sched = read_json(path(self.name, ".schedule.json"), [])
        return {"name": self.name, "title": c.get("machine", self.name), "kind": ep["kind"],
                "link": ep.get("path"), "port": ep.get("port"), "framing": c.get("framing"),
                "studio_port": c.get("studio_port"), "application_json": c.get("application_json"),
                "control_port": c["control_port"], "real_plc": c.get("real_plc"),
                "devices": [{"unit": u, "name": d["name"]} for u, d in sorted(c["devices"].items())],
                "running": pid is not None, "pid": pid, "poll": last_poll(self.name) if pid else None,
                "delay": read_json(path(self.name, ".delay"), {}),
                "schedules": (sched if isinstance(sched, list) else sched.get("schedules", [])) + c.get("schedules", []),
                "file_schedules": len(c.get("schedules", []))}

    def state(self):
        st = read_json(path(self.name, ".state.json"), None)
        if not isinstance(st, dict) or "units" not in st:
            return None
        return st

    def detail(self):
        running = pid_of(self.name) is not None
        st = self.state() if running else None
        units = (st or {}).get("units", {})
        writes = (st or {}).get("writes", {})
        devices = []
        for u, d in sorted(self.cfg["devices"].items()):
            tables = units.get(str(u), {})
            counts = writes.get(str(u), {})
            rows = []
            for t in d["tags"]:
                row = {"tag": t["tag"], "group": t.get("group", ""), "plc": t.get("plc_addr", ""),
                       "type": t["type"], "table": t["table"], "addr": t["addr"], "bit": t.get("bit"),
                       "words": t["words"], "pending": t["pending"], "note": t.get("note", ""),
                       "expression": t.get("expression", ""), "start": t.get("value"),
                       "word_order": t.get("word_order", ""), "writes": counts.get(t["tag"], 0),
                       "writable": t["table"] in ("hr", "coil")}
                if st is not None and not t["pending"]:
                    m = tables.get(t["table"], {})
                    words = [m.get(str(t["addr"] + i), 0) for i in range(t["words"])]
                    try:
                        row["value"] = decode_value(t["type"], words, t)
                    except Exception as ex:  # e.g. bytes that aren't valid text
                        row["value"], row["error"] = None, str(ex)
                    row["raw"] = " ".join("%04X" % w for w in words) if t["table"] in ("hr", "ir") else None
                rows.append(row)
            tagged = {t["tag"] for t in d["tags"]}
            other = {k: v for k, v in counts.items() if k not in tagged}
            devices.append({"unit": u, "name": d["name"], "tags": rows, "other_writes": other})
        extra = sorted(int(u) for u in writes if int(u) not in self.cfg["devices"] and writes[u])
        return {"machine": self.summary(), "devices": devices,
                "unknown_unit_writes": {u: writes[str(u)] for u in extra}}

    def client(self, unit):
        if not pid_of(self.name):
            raise ValueError("%s isn't running; start it first" % self.name)
        try:
            return Client("127.0.0.1", self.cfg["control_port"], unit, 3)
        except SystemExit as ex:
            raise ValueError(str(ex))

    def set(self, unit, values):
        todo = []
        for tag, value in values.items():
            t = find_tag(self.cfg, tag, unit)
            if t["pending"]:
                raise ValueError("%s has no Modbus address yet" % tag)
            try:
                todo.append((t, encode_value(t["type"], value, t)))
            except ValueError as ex:
                raise ValueError("%s: %s" % (tag, ex))
        cl = self.client(unit)
        try:
            for t, words in todo:
                if t["type"] == "BIT":  # merge into the register from the state file, so no read is logged
                    reg = ((self.state() or {}).get("units", {}).get(str(unit), {}).get(t["table"], {})
                           .get(str(t["addr"]), 0))
                    words = [set_bit(reg, t["bit"], words[0])]
                cl.write(t["table"], t["addr"], words, unit)
        except ModbusException as ex:
            raise ValueError(str(ex))
        except SystemExit as ex:
            raise ValueError(str(ex))
        finally:
            cl.sock.close()

    def reset_counts(self):
        cl = self.client(min(self.cfg["devices"]) if self.cfg["devices"] else 1)
        try:
            cl.call(bytes([FC_RESET_COUNTS]))
        except SystemExit as ex:
            raise ValueError(str(ex))
        finally:
            cl.sock.close()

    def set_delay(self, body):
        """{"default": ms, "cycle": {"ms": [..], "every_s": s}}; unit/fc entries already in the file are kept."""
        old = read_json(path(self.name, ".delay"), {})
        new = {k: old[k] for k in ("unit", "fc") if old.get(k)}
        default = int(body.get("default") or 0)
        if default < 0:
            raise ValueError("delay must not be negative")
        if default:
            new["default"] = default
        cyc = body.get("cycle")
        if cyc and cyc.get("ms"):
            ms = [int(v) for v in cyc["ms"]]
            every = float(cyc.get("every_s") or 0)
            if every <= 0 or min(ms) < 0:
                raise ValueError("a delay cycle needs ms values ≥ 0 and a period > 0 s")
            new["cycle"] = {"ms": ms, "every_s": every}
        if new:
            write_json(path(self.name, ".delay"), new)
        elif os.path.exists(path(self.name, ".delay")):
            os.remove(path(self.name, ".delay"))
        return new

    def set_schedules(self, items):
        out = []
        for s in items:
            unit = int(s.get("unit") or min(self.cfg["devices"]))
            values = s.get("values")
            if isinstance(values, str):
                values = [v.strip() for v in values.split(",") if v.strip()]
            every = float(s.get("every_s") or 0)
            if not values or every <= 0:
                raise ValueError("a schedule needs values and a period > 0 s")
            if s.get("tag"):
                t = find_tag(self.cfg, s["tag"], unit)
                if t["pending"]:
                    raise ValueError("%s has no Modbus address yet" % t["tag"])
                item = {"unit": unit, "tag": t["tag"]}
            else:
                t = {"table": table_name(s["table"]), "addr": int(s["addr"]), "type": (s.get("type") or "UINT16")}
                t["type"] = t["type"].upper()
                item = {"unit": unit, "table": t["table"], "addr": t["addr"], "type": t["type"]}
                word_count(t["type"], t)
            vals = []
            for v in values:
                try:
                    encode_value(t["type"], v, t)
                except ValueError as ex:
                    raise ValueError("%s: %s" % (item.get("tag") or "%s %d" % (t["table"], t["addr"]), ex))
                vals.append(float(v) if t["type"] == "FLOAT32" else int(str(v), 0) if t["type"] != "STRING" else v)
            item.update({"values": vals, "every_s": every})
            out.append(item)
        if out:
            write_json(path(self.name, ".schedule.json"), out)
        elif os.path.exists(path(self.name, ".schedule.json")):
            os.remove(path(self.name, ".schedule.json"))
        return out


def run_sh(*args):
    p = subprocess.run(["./run.sh"] + list(args), cwd=HERE, capture_output=True, text=True, timeout=120)
    return (p.stdout + p.stderr).strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "ModbusSimUI"

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

    def machine(self, name):
        if name not in machine_names():
            raise LookupError("no machine %r" % name)
        return Machine(name)

    def do_GET(self):
        url = self.path.split("?", 1)
        parts = [p for p in url[0].split("/") if p]
        query = dict(q.split("=", 1) for q in url[1].split("&") if "=" in q) if len(url) > 1 else {}
        try:
            if not parts:
                return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
            if parts == ["api", "machines"]:
                out = []
                for n in machine_names():
                    try:
                        out.append(Machine(n).summary())
                    except SystemExit as ex:  # a broken file shouldn't hide the others
                        out.append({"name": n, "title": n, "error": str(ex), "devices": [], "running": False})
                return self.send(200, out)
            if len(parts) == 3 and parts[:2] == ["api", "m"]:
                return self.send(200, self.machine(parts[2]).detail())
            if len(parts) == 4 and parts[:2] == ["api", "m"] and parts[3] == "log":
                self.machine(parts[2])
                n = min(int(query.get("n", 200)), 2000)
                return self.send(200, {"lines": tail(path(parts[2], ".log"), n)})
            self.send(404, {"error": "not found"})
        except LookupError as ex:
            self.send(404, {"error": str(ex)})
        except SystemExit as ex:
            self.send(500, {"error": str(ex)})
        except Exception as ex:
            self.send(500, {"error": "%s: %s" % (type(ex).__name__, ex)})

    def do_POST(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if parts[:2] != ["api", "m"] or len(parts) != 4:
                return self.send(404, {"error": "not found"})
            name, action = parts[2], parts[3]
            names = machine_names() if name == "all" else [self.machine(name).name]
            if action in ("start", "reset"):  # start = (re)start keeping values; reset = starting values
                cmd = ["reset"] if action == "reset" else []
                return self.send(200, {"output": "\n".join((run_sh(*cmd, m).splitlines() or [""])[-1]
                                                           for m in names)})
            if action == "stop":
                return self.send(200, {"output": "\n".join(run_sh("stop", m) for m in names)})
            m = self.machine(name)
            if action == "set":
                values = body.get("values", body)
                m.set(int(body.get("unit") or min(m.cfg["devices"])), values)
                return self.send(200, {"ok": True})
            if action == "reset-counts":
                m.reset_counts()
                return self.send(200, {"ok": True})
            if action == "delay":
                return self.send(200, {"delay": m.set_delay(body)})
            if action == "schedules":
                return self.send(200, {"schedules": m.set_schedules(body.get("schedules", []))})
            self.send(404, {"error": "not found"})
        except LookupError as ex:
            self.send(404, {"error": str(ex)})
        except (ValueError, TypeError, KeyError) as ex:
            self.send(400, {"error": str(ex)})
        except SystemExit as ex:
            self.send(400, {"error": str(ex)})
        except Exception as ex:
            self.send(500, {"error": "%s: %s" % (type(ex).__name__, ex)})


PAGE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Modbus Simulator</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #1d2330; --muted: #6b7385; --line: #e3e6ec;
  --accent: #2f6fe4; --accent-weak: #e8effd; --ok: #1f9d55; --ok-weak: #e3f5ea; --bad: #d64545;
  --bad-weak: #fbe9e9; --warn: #b7791f; --warn-weak: #fdf3e1; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --flash: #fff4b8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151b; --panel: #1a1e26; --text: #e4e7ee; --muted: #8b93a5; --line: #2a303b;
    --accent: #5b8ff0; --accent-weak: #1e2a42; --ok: #3cc47c; --ok-weak: #15301f; --bad: #f06b6b;
    --bad-weak: #3a1c1c; --warn: #e0a84a; --warn-weak: #382b14; --flash: #4a4220;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
header { display: flex; align-items: center; gap: 12px; padding: 12px 20px; background: var(--panel);
  border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 5; flex-wrap: wrap; }
header h1 { font-size: 16px; margin: 0; font-weight: 650; }
.muted { color: var(--muted); }
.spacer { flex: 1; }
button { font: inherit; border: 1px solid var(--line); background: var(--panel); color: var(--text);
  padding: 6px 12px; border-radius: 7px; cursor: pointer; white-space: nowrap; }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.danger { color: var(--bad); }
button.small { padding: 3px 9px; font-size: 12.5px; }
button:disabled { opacity: .5; cursor: default; }
.layout { display: grid; grid-template-columns: 290px 1fr; gap: 16px; padding: 16px 20px; max-width: 1500px; margin: 0 auto; }
@media (max-width: 860px) { .layout { grid-template-columns: 1fr; padding: 12px 16px; } }
.machines { display: flex; flex-direction: column; gap: 8px; }
.group-label { font-size: 12px; color: var(--muted); margin: 6px 2px 0; text-transform: uppercase; letter-spacing: .04em; }
.mcard { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; cursor: pointer; }
.mcard.sel { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.mcard .row1 { display: flex; align-items: center; gap: 8px; }
.mcard .name { font-weight: 650; }
.mcard .meta { color: var(--muted); font-size: 12px; margin-top: 3px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: none; }
.dot.on { background: var(--ok); }
.pill { font-size: 11.5px; padding: 1px 7px; border-radius: 999px; background: var(--line); color: var(--muted); white-space: nowrap; }
.pill.ok { background: var(--ok-weak); color: var(--ok); }
.pill.warn { background: var(--warn-weak); color: var(--warn); }
.pill.bad { background: var(--bad-weak); color: var(--bad); }
.pill.info { background: var(--accent-weak); color: var(--accent); }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; min-width: 0; }
.detail-head { padding: 14px 16px; border-bottom: 1px solid var(--line); display: flex; gap: 16px; flex-wrap: wrap; align-items: flex-start; }
.detail-head h2 { margin: 0 0 6px; font-size: 18px; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 3px 12px; font-size: 13px; margin: 0; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.mono { font-family: var(--mono); font-size: 12.5px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-left: auto; }
input[type=text], input[type=number], input[type=search], select { font: inherit; padding: 5px 8px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--bg); color: var(--text); min-width: 0; }
input:focus, select:focus { outline: 2px solid var(--accent-weak); border-color: var(--accent); }
.banner { margin: 12px 16px 0; padding: 10px 12px; border-radius: 8px; font-size: 13px; }
.banner.ok { background: var(--ok-weak); color: var(--ok); }
.banner.warn { background: var(--warn-weak); color: var(--warn); }
.toolbar { display: flex; gap: 8px; padding: 12px 16px; align-items: center; flex-wrap: wrap; }
.tabs { display: flex; gap: 2px; background: var(--bg); padding: 3px; border-radius: 8px; flex-wrap: wrap; }
.tabs button { border: 0; background: transparent; padding: 5px 12px; border-radius: 6px; }
.tabs button.sel { background: var(--panel); box-shadow: 0 1px 2px rgba(0,0,0,.12); }
.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 7px 10px; border-top: 1px solid var(--line); vertical-align: middle; }
th { font-size: 12px; color: var(--muted); font-weight: 600; background: var(--panel); }
td.val { min-width: 220px; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.pending td { color: var(--muted); }
tr.changed td.val { animation: flash 1.6s ease-out; }
@keyframes flash { from { background: var(--flash); } to { background: transparent; } }
.edit { display: flex; gap: 6px; align-items: center; }
.edit input { width: 150px; }
.hint { color: var(--muted); font-size: 12px; }
.switch { position: relative; width: 40px; height: 22px; border-radius: 11px; background: var(--line); border: 0; padding: 0; flex: none; }
.switch::after { content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: left .12s; box-shadow: 0 1px 2px rgba(0,0,0,.3); }
.switch.on { background: var(--ok); }
.switch.on.alarm { background: var(--bad); }
.switch.on::after { left: 21px; }
.raw { color: var(--muted); font-family: var(--mono); font-size: 11.5px; }
.section { border-top: 1px solid var(--line); padding: 12px 16px; }
.section h3 { margin: 0 0 8px; font-size: 14px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
.ctl-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 6px 0; font-size: 13px; }
.ctl-row input[type=number] { width: 90px; }
.ctl-row input.wide { width: 150px; }
.sched td, .sched th { padding: 5px 8px; font-size: 13px; }
.log { margin: 0; padding: 10px 12px; font-family: var(--mono); font-size: 12px; line-height: 1.5; max-height: 360px; overflow: auto;
  white-space: pre-wrap; word-break: break-all; background: var(--bg); border-radius: 8px; }
.log .w { color: var(--accent); } .log .e { color: var(--bad); } .log .c { color: var(--muted); } .log .d { color: var(--warn); }
.empty { padding: 40px; text-align: center; color: var(--muted); }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg);
  padding: 8px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none; max-width: 90vw; z-index: 10; }
#toast.show { opacity: 1; }
#toast.err { background: var(--bad); color: #fff; }
</style>
</head>
<body>
<header>
  <h1>Modbus Simulator</h1>
  <span class="muted" id="host"></span>
  <span class="spacer"></span>
  <button class="primary" onclick="act('all','start')">Start all</button>
  <button class="danger" onclick="act('all','stop')">Stop all</button>
</header>
<div class="layout">
  <div class="machines" id="machines"></div>
  <div class="panel" id="detail">
    <div id="head"><div class="empty">Loading…</div></div>
    <div id="body"></div>
    <div class="section" id="controls"></div>
    <div class="section"><h3>Request log
      <label class="muted" style="font-size:13px;font-weight:400"><input type="checkbox" id="quiet" checked onchange="loadLog()"> hide connect/disconnect</label>
      <label class="muted" style="font-size:13px;font-weight:400"><input type="checkbox" id="live" checked> live</label></h3>
      <pre class="log" id="log"></pre></div>
  </div>
</div>
<div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const store = { get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };
let machines = [], sel = store.get("sel", null), tab = store.get("tab", "all"), unitSel = {}, filter = "";
let detail = null, prev = {}, controlsFor = null, draft = null;
$("#host").textContent = location.host;

function toast(msg, err) {
  const t = $("#toast"); t.textContent = msg; t.className = "show" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.className = "", err ? 6000 : 2400);
}
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({error: "bad reply"}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function ago(s) { return s < 60 ? Math.round(s) + "s ago" : s < 3600 ? Math.round(s / 60) + " min ago" : Math.round(s / 3600) + " h ago"; }
function pollPill(m) {
  if (!m.running) return '<span class="pill">stopped</span>';
  if (!m.poll) return '<span class="pill warn">no reads yet</span>';
  return `<span class="pill ${m.poll.ago < 30 ? "ok" : "warn"}" title="last read from ${esc(m.poll.peer)}">read ${ago(m.poll.ago)}</span>`;
}
function delayText(d) {
  if (!d) return "";
  const parts = [];
  if (d.cycle) parts.push(d.cycle.ms.join("↔") + " ms / " + d.cycle.every_s + "s");
  else if (d.default) parts.push(d.default + " ms");
  if (d.unit && Object.keys(d.unit).length) parts.push("per unit");
  if (d.fc && Object.keys(d.fc).length) parts.push("per FC");
  return parts.join(", ");
}
function endpointShort(m) {
  if (m.kind === "pty") return (m.studio_port || "serial") + " · pty";
  return "tcp " + m.port;
}

function renderMachines() {
  const groups = [["Serial (pty)", machines.filter(m => m.kind === "pty")], ["TCP", machines.filter(m => m.kind !== "pty")]];
  $("#machines").innerHTML = groups.filter(g => g[1].length).map(([label, ms]) => `<div class="group-label">${label}</div>` + ms.map(m => `
    <div class="mcard ${m.name === sel ? "sel" : ""}" onclick="pick('${m.name}')">
      <div class="row1"><span class="dot ${m.running ? "on" : ""}"></span><span class="name">${esc(m.name)}</span>
        <span class="muted">${esc(m.title)}</span></div>
      <div class="meta">${m.error ? `<span class="pill bad">${esc(m.error)}</span>` : `${esc(endpointShort(m))} · unit ${m.devices.map(d => d.unit).join(", ")} ${pollPill(m)}
        ${delayText(m.delay) ? `<span class="pill warn">delay ${esc(delayText(m.delay))}</span>` : ""}
        ${m.schedules && m.schedules.length ? `<span class="pill info">${m.schedules.length} schedule${m.schedules.length > 1 ? "s" : ""}</span>` : ""}`}</div>
    </div>`).join("")).join("");
}
function pick(name) { sel = name; store.set("sel", name); prev = {}; controlsFor = null; renderMachines(); refresh(); }

function copy(text) {
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => toast("Copied " + text), () => toast(text));
}

function renderHead(m) {
  const rp = m.real_plc;
  const ep = m.kind === "pty"
    ? `<dt>Serial port</dt><dd><span class="mono">${esc(m.link)}</span><button class="small" onclick="copy('${esc(m.link)}')">Copy</button>
        <span class="hint">→ application.json ${esc(m.application_json || "")} (set it in IoTLink, or a cloud deploy puts /dev/ttyAMA* back)</span></dd>
       <dt>Framing</dt><dd>${esc((m.framing || "auto").toUpperCase())}, any baud/parity (a pty ignores them)</dd>`
    : `<dt>Endpoint</dt><dd class="mono">127.0.0.1:${m.port} (or this PortaCON's IP)</dd>
       <dt>Framing</dt><dd>${esc(m.framing ? m.framing.toUpperCase() : "Modbus TCP, RTU or ASCII, detected per connection")}</dd>`;
  $("#head").innerHTML = `<div class="detail-head">
    <div style="min-width:0">
      <h2>${esc(m.title)} <span class="muted" style="font-weight:400">${esc(m.name)}</span></h2>
      <dl class="kv">${ep}
        <dt>Devices</dt><dd>${m.devices.map(d => `slave ${d.unit}${d.name ? " · " + esc(d.name) : ""}`).join("; ")}</dd>
        ${rp ? `<dt>Real PLC</dt><dd>${esc(rp.make || "")} <span class="mono">${esc(rp.ip || "")}:${esc(rp.port || "")}</span> ${esc(rp.connection || "")}</dd>` : ""}
        ${m.running ? `<dt>Process</dt><dd class="mono">pid ${m.pid}, control port 127.0.0.1:${m.control_port}</dd>` : ""}
      </dl>
    </div>
    <div class="actions">
      ${m.running
        ? `<button onclick="act(sel,'start')" title="Restart the process; values and write counts are kept">Restart</button>
           <button class="danger" onclick="act(sel,'stop')">Stop</button>`
        : `<button class="primary" onclick="act(sel,'start')">Start</button>`}
      <button onclick="act(sel,'reset')" title="Back to the file's starting values, write counts zeroed">Reset values</button>
      <button ${m.running ? "" : "disabled"} onclick="resetCounts()">Reset write counts</button>
    </div>
  </div>`;
}

function addr(t) { return t.pending ? t.table + " —" : t.table + " " + t.addr + (t.type === "BIT" ? "." + t.bit : ""); }
function valueCell(t, m, ui) {
  if (t.pending) return `<span title="${esc(t.note)}">no Modbus address yet — set "addr" in the machine file</span>`;
  if (!m.running) return `<span class="raw">start: ${esc(t.start ?? "")}</span>`;
  if (t.error) return `<span class="pill bad">${esc(t.error)}</span>`;
  if (t.type === "BOOL" || t.type === "BIT") {
    const on = Number(t.value) === 1;
    return `<div class="edit"><button class="switch ${on ? "on" : ""} ${t.group === "alarm" ? "alarm" : ""}"
      onclick="setTag(${ui}, ${on ? 0 : 1})" aria-label="toggle"></button><span>${on ? "1" : "0"}</span></div>`;
  }
  const isStr = t.type === "STRING";
  return `<div class="edit"><input data-ui="${ui}" type="text" value="${esc(t.value)}" ${isStr ? `maxlength="${t.words * 2}"` : 'inputmode="decimal"'}
      onkeydown="if(event.key==='Enter'){setTag(${ui}, this.value);this.blur()} if(event.key==='Escape'){this.value=rows[${ui}].value;this.blur()}">
    ${isStr ? `<span class="hint">max ${t.words * 2}</span>` : ""}</div>`;
}

let rows = [];
function renderBody(d) {
  const m = d.machine;
  const devs = d.devices;
  let unit = unitSel[m.name];
  if (!devs.some(x => x.unit === unit)) unit = devs[0]?.unit;
  unitSel[m.name] = unit;
  const dev = devs.find(x => x.unit === unit) || {tags: [], other_writes: {}};
  rows = dev.tags;
  const groups = [...new Set(rows.map(t => t.group).filter(Boolean))];
  if (tab !== "all" && !groups.includes(tab)) tab = "all";
  const alarmsOn = rows.filter(t => t.group === "alarm" && Number(t.value) === 1).length;
  const shown = rows.map((t, i) => [t, i]).filter(([t]) => (tab === "all" || t.group === tab) &&
    (!filter || (t.tag + " " + t.plc + " " + addr(t)).toLowerCase().includes(filter.toLowerCase())));
  const target = m.kind === "pty" ? `<span class="mono">${esc(m.link)}</span>` : `<span class="mono">127.0.0.1:${m.port}</span>`;
  const banner = !m.running
    ? `<div class="banner warn">${esc(m.name)} is stopped. Start it to serve ${m.kind === "pty" ? "the serial port" : "port " + m.port}.</div>`
    : m.poll ? `<div class="banner ${m.poll.ago < 30 ? "ok" : "warn"}">Last read ${ago(m.poll.ago)} from <span class="mono">${esc(m.poll.peer)}</span> (${esc(m.poll.framing)}, unit ${m.poll.unit}).</div>`
    : `<div class="banner warn">Running, but no master has read from it yet. Point the device at ${target}${m.kind === "pty" ? "" : ", unit " + devs.map(x => x.unit).join("/")}.</div>`;
  const totalWrites = rows.reduce((a, t) => a + (t.writes || 0), 0) + Object.values(dev.other_writes || {}).reduce((a, b) => a + b, 0);
  const other = Object.entries(dev.other_writes || {});
  const unknown = Object.entries(d.unknown_unit_writes || {});
  const focused = document.activeElement;
  const keep = focused && focused.dataset && focused.dataset.ui !== undefined ? {ui: focused.dataset.ui, value: focused.value, s: focused.selectionStart} : null;
  $("#body").innerHTML = `${banner}
    <div class="toolbar">
      ${devs.length > 1 ? `<div class="tabs">${devs.map(x => `<button class="${x.unit === unit ? "sel" : ""}" onclick="unitSel['${m.name}']=${x.unit};prev={};refresh()">slave ${x.unit}${x.name ? " · " + esc(x.name) : ""}</button>`).join("")}</div>` : ""}
      ${groups.length ? `<div class="tabs">${["all", ...groups].map(g => `<button class="${tab === g ? "sel" : ""}" onclick="setTab('${g}')">${
        g === "all" ? "All" : g === "alarm" ? "Alarms" : g === "data" ? "Data" : esc(g)} <span class="pill ${g === "alarm" && alarmsOn ? "bad" : ""}">${
        g === "all" ? rows.length : g === "alarm" && alarmsOn ? alarmsOn + " on" : rows.filter(t => t.group === g).length}</span></button>`).join("")}</div>` : ""}
      <input type="search" id="filter" placeholder="Filter tags or addresses" value="${esc(filter)}" oninput="filter=this.value;renderBody(detail)">
      <span class="spacer"></span>
      <span class="muted" style="font-size:13px">master writes: <b>${totalWrites}</b></span>
      ${groups.includes("alarm") ? `<button ${m.running ? "" : "disabled"} onclick="allAlarms()">All alarms off</button>` : ""}
    </div>
    <div class="tablewrap"><table>
      <thead><tr><th>Tag</th><th>${rows.some(t => t.plc) ? "PLC address" : "Studio"}</th><th>Modbus</th><th>Type</th><th>Value</th><th class="num" title="Writes from the master (ptcsw) since the last reset">Writes</th><th>Registers</th></tr></thead>
      <tbody>${shown.map(([t, i]) => {
        const key = unit + "/" + t.tag, val = JSON.stringify(t.value) + "/" + t.writes;
        const changed = prev[key] !== undefined && prev[key] !== val; prev[key] = val;
        const studio = t.plc || [t.word_order && t.words === 2 ? t.word_order + " word order" : "", t.expression ? "expr " + t.expression : ""].filter(Boolean).join(", ");
        return `<tr class="${t.pending ? "pending" : ""} ${changed ? "changed" : ""}" title="${esc(t.note)}">
          <td>${esc(t.tag)}</td><td class="mono">${esc(studio)}</td><td class="mono">${esc(addr(t))}</td>
          <td class="mono">${esc(t.type)}${t.type === "STRING" ? " ×" + t.words : ""}</td>
          <td class="val">${valueCell(t, m, i)}</td>
          <td class="num">${t.writes ? `<span class="pill ${t.writes > 1 ? "warn" : "info"}">${t.writes}</span>` : '<span class="muted">0</span>'}</td>
          <td class="raw">${esc(t.raw || "")}</td></tr>`;
      }).join("") || `<tr><td colspan="7" class="empty">No tags match.</td></tr>`}</tbody>
    </table></div>
    ${other.length || unknown.length ? `<div class="section"><h3>Writes to addresses without a tag</h3><div class="mono" style="font-size:12.5px">${
      other.map(([k, n]) => `slave ${unit} ${esc(k)}: ${n}`).concat(unknown.flatMap(([u, c]) => Object.entries(c).map(([k, n]) => `slave ${u} (no device) ${esc(k)}: ${n}`))).join("<br>")}</div></div>` : ""}`;
  if (keep) {
    const el = document.querySelector(`[data-ui="${keep.ui}"]`);
    if (el) { el.value = keep.value; el.focus(); try { el.setSelectionRange(keep.s, keep.s); } catch (e) {} }
  }
  if (document.activeElement?.id !== "filter" && focused?.id === "filter") {
    const f = $("#filter"); f.focus(); f.setSelectionRange(f.value.length, f.value.length);
  }
}

function renderControls(d) {
  const m = d.machine, dl = m.delay || {};
  if (!draft) draft = { sched: (m.schedules || []).slice(0, m.schedules.length - m.file_schedules)
    .map(s => ({...s, values: (s.values || []).join(", ")})) };
  const tagOpts = (unit, cur) => (d.devices.find(x => x.unit === Number(unit)) || d.devices[0] || {tags: []}).tags
    .filter(t => !t.pending).map(t => `<option ${t.tag === cur ? "selected" : ""}>${esc(t.tag)}</option>`).join("");
  $("#controls").innerHTML = `<div class="controls">
    <div>
      <h3>Reply delay <span class="hint" style="font-weight:400">re-read within 0.1 s; ${m.kind === "pty" ? "requests during a delay are dropped, like a busy device" : "requests during a delay queue"}</span></h3>
      <div class="ctl-row">Fixed <input type="number" min="0" step="100" id="d-default" value="${dl.cycle ? "" : (dl.default || 0)}"> ms</div>
      <div class="ctl-row">or cycle through <input type="text" class="wide" id="d-cycle" placeholder="500, 1000" value="${dl.cycle ? dl.cycle.ms.join(", ") : ""}"> ms,
        every <input type="number" min="0.1" step="0.5" id="d-every" value="${dl.cycle ? dl.cycle.every_s : 10}"> s</div>
      ${dl.unit && Object.keys(dl.unit).length || dl.fc && Object.keys(dl.fc).length ? `<div class="ctl-row hint">Kept from the file: ${esc(JSON.stringify({unit: dl.unit, fc: dl.fc}))}</div>` : ""}
      <div class="ctl-row"><button class="primary" onclick="saveDelay()">Apply</button><button onclick="clearDelay()">No delay</button>
        <span class="hint">now: ${esc(delayText(dl) || "none")}</span></div>
    </div>
    <div>
      <h3>Value schedules <span class="hint" style="font-weight:400">values step every period, from when they're saved</span></h3>
      <table class="sched"><thead><tr><th>Slave</th><th>Tag</th><th>Values</th><th>Every (s)</th><th></th></tr></thead><tbody>
        ${draft.sched.map((s, i) => `<tr>
          <td><select onchange="draft.sched[${i}].unit=Number(this.value);renderControls(detail)">${d.devices.map(x => `<option value="${x.unit}" ${x.unit === Number(s.unit) ? "selected" : ""}>${x.unit}</option>`).join("")}</select></td>
          <td>${s.tag !== undefined ? `<select onchange="draft.sched[${i}].tag=this.value">${tagOpts(s.unit, s.tag)}</select>`
               : `<span class="mono">${esc(s.table)} ${esc(s.addr)} ${esc(s.type)}</span>`}</td>
          <td><input type="text" style="width:130px" value="${esc(s.values)}" placeholder="5, 10" oninput="draft.sched[${i}].values=this.value"></td>
          <td><input type="number" style="width:70px" min="0.1" step="0.5" value="${esc(s.every_s)}" oninput="draft.sched[${i}].every_s=this.value"></td>
          <td><button class="small danger" onclick="draft.sched.splice(${i},1);renderControls(detail)">Remove</button></td></tr>`).join("")
          || `<tr><td colspan="5" class="hint">None. ${m.file_schedules ? m.file_schedules + " more in the machine file." : ""}</td></tr>`}
      </tbody></table>
      <div class="ctl-row"><button class="small" onclick="addSched()">Add</button>
        <button class="primary" onclick="saveSched()">Save schedules</button>
        ${m.file_schedules ? `<span class="hint">+ ${m.file_schedules} from the machine file</span>` : ""}</div>
    </div></div>`;
}
function addSched() {
  const u = unitSel[sel] || detail.devices[0]?.unit || 1;
  const t = (detail.devices.find(x => x.unit === u) || {tags: []}).tags.find(t => !t.pending);
  draft.sched.push({unit: u, tag: t ? t.tag : "", values: "", every_s: 5});
  renderControls(detail);
}
async function saveSched() {
  try { const r = await api(`/api/m/${sel}/schedules`, {schedules: draft.sched}); toast(r.schedules.length ? r.schedules.length + " schedule(s) saved" : "Schedules cleared"); draft = null; }
  catch (e) { toast(e.message, true); }
  await refresh(true);
}
async function saveDelay() {
  const cyc = $("#d-cycle").value.split(",").map(s => s.trim()).filter(Boolean).map(Number);
  const body = cyc.length ? {cycle: {ms: cyc, every_s: Number($("#d-every").value)}} : {default: Number($("#d-default").value) || 0};
  try { const r = await api(`/api/m/${sel}/delay`, body); toast("Delay: " + (delayText(r.delay) || "none")); }
  catch (e) { toast(e.message, true); }
  await refresh(true);
}
async function clearDelay() {
  try { await api(`/api/m/${sel}/delay`, {}); toast("Delay off"); } catch (e) { toast(e.message, true); }
  await refresh(true);
}

async function loadLog() {
  if (!sel) return;
  let lines = [];
  try { lines = (await api(`/api/m/${sel}/log?n=400`)).lines; } catch (e) { lines = [String(e)]; }
  const el = $("#log");
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
  const quiet = $("#quiet").checked;
  lines = lines.filter(l => !quiet || !/ (connect|disconnect|speaks Modbus \w+)$/.test(l)).slice(-250);
  el.innerHTML = lines.map(l => `<span class="${/ write /.test(l) ? "w" : / exception |bad |garbled|malformed|error/.test(l) ? "e" : /delay|schedule|ignored/.test(l) ? "d" : / (connect|disconnect)$/.test(l) ? "c" : ""}">${esc(l)}</span>`).join("\n") || "(empty)";
  if (atBottom || !el._init) { el.scrollTop = el.scrollHeight; el._init = true; }
}

async function refresh(withControls) {
  try {
    machines = await api("/api/machines");
    if (!sel || !machines.some(m => m.name === sel)) sel = machines[0]?.name;
    renderMachines();
    if (!sel) { $("#head").innerHTML = '<div class="empty">No simulator files in machines/.</div>'; return; }
    detail = await api(`/api/m/${sel}`);
    renderHead(detail.machine);
    renderBody(detail);
    if (withControls || controlsFor !== sel) { controlsFor = sel; draft = null; renderControls(detail); }
    if ($("#live").checked) loadLog();
  } catch (e) { toast("Can't reach the UI server: " + e.message, true); }
}
function setTab(g) { tab = g; store.set("tab", g); renderBody(detail); }

async function setTag(i, value) {
  const t = rows[i];
  try { await api(`/api/m/${sel}/set`, {unit: unitSel[sel], values: {[t.tag]: value}}); toast(`${t.tag} = ${value}`); }
  catch (e) { toast(e.message, true); }
  document.activeElement?.blur(); refresh();
}
async function allAlarms() {
  const values = {};
  rows.filter(t => t.group === "alarm" && !t.pending && Number(t.value) !== 0).forEach(t => values[t.tag] = 0);
  if (!Object.keys(values).length) return toast("Every alarm is already off");
  try { await api(`/api/m/${sel}/set`, {unit: unitSel[sel], values}); toast(Object.keys(values).length + " alarms turned off"); }
  catch (e) { toast(e.message, true); }
  refresh();
}
async function resetCounts() {
  try { await api(`/api/m/${sel}/reset-counts`, {}); toast("Write counts reset"); } catch (e) { toast(e.message, true); }
  refresh();
}
async function act(name, action) {
  const what = name === "all" ? "every simulator" : name;
  if (action === "reset" && !confirm(`Reset ${what} to the file's starting values and zero its write counts? It restarts.`)) return;
  if (action === "stop" && name === "all" && !confirm("Stop every simulator?")) return;
  toast({start: "Starting ", stop: "Stopping ", reset: "Resetting "}[action] + what + "…");
  try { const r = await api(`/api/m/${name}/${action}`, {}); toast(r.output?.split("\n").pop() || "Done"); }
  catch (e) { toast(e.message, true); }
  refresh();
}
refresh(true);
setInterval(() => {
  const a = document.activeElement;
  if (a && (a.dataset?.ui !== undefined || a.closest?.("#controls"))) return;  // don't redraw under someone typing
  refresh();
}, 2000);
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
