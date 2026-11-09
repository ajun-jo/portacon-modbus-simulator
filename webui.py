#!/usr/bin/env python3
"""Web UI for the simulators: open it in a browser to start and stop machines, see and change tag values,
and watch what your server asks for.

  python3 webui.py                      http://<this machine>:8080
  python3 webui.py --port 8081 --bind 127.0.0.1

It runs next to the simulators (on the portacon: ./remote.sh <host> ui) and controls them through run.sh.
Values come from each simulator's <name>.state.json, so the UI's refreshes don't show up in the request log;
changes are sent as ordinary Modbus writes, so they do. There is no login: anyone who can reach the port can
change simulator values (nothing else).
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
from modbus_plc_sim import decode_value, encode_value, load_config, set_bit  # noqa: E402

READ_LINE = re.compile(r"^(\S+) (tcp|udp) (\S+) (\S+) unit (\d+) read ")


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
    """The newest read request in the log: who asked, with which framing, and how many seconds ago."""
    for line in reversed(tail(path(name, ".log"), 400)):
        m = READ_LINE.match(line)
        if m:
            try:
                ago = (datetime.now() - datetime.fromisoformat(m.group(1))).total_seconds()
            except ValueError:
                return None
            return {"ago": round(ago, 1), "peer": m.group(3), "framing": m.group(4), "unit": int(m.group(5))}
    return None


def get_delay(name):
    try:
        with open(path(name, ".delay")) as f:
            return int(json.load(f).get("default", 0))
    except (OSError, ValueError, AttributeError):
        return 0


class Machine:
    def __init__(self, name):
        self.name = name
        self.file = os.path.join(HERE, "machines", name + ".init.json")
        self.cfg = load_config(self.file)

    def summary(self):
        c = self.cfg
        pid = pid_of(self.name)
        return {"name": self.name, "title": c.get("machine", self.name), "port": c.get("port"),
                "unit": c.get("unit_id", 1), "real_plc": c.get("real_plc"), "running": pid is not None,
                "pid": pid, "poll": last_poll(self.name) if pid else None, "delay": get_delay(self.name)}

    def state(self):
        try:
            with open(path(self.name, ".state.json")) as f:
                return {t: {int(a): v for a, v in m.items()} for t, m in json.load(f).items()}
        except (OSError, ValueError):
            return None

    def tags(self):
        st = self.state() if pid_of(self.name) else None
        out = []
        for t in self.cfg["tags"]:
            row = {"tag": t["tag"], "group": t.get("group", ""), "plc": t.get("plc_addr", ""), "type": t["type"],
                   "table": t["table"], "addr": t["addr"], "bit": t.get("bit"), "words": t["words"],
                   "pending": t["pending"], "note": t.get("note", ""), "start": t.get("value")}
            if st is not None and not t["pending"]:
                m = st.get(t["table"], {})
                words = [m.get(t["addr"] + i, 0) for i in range(t["words"])]
                try:
                    row["value"] = decode_value(t["type"], words, t)
                except Exception as ex:  # e.g. bytes that aren't valid text
                    row["value"], row["error"] = None, str(ex)
                row["raw"] = " ".join("%04X" % w for w in words) if t["table"] in ("hr", "ir") else None
            out.append(row)
        return out

    def set(self, tag, value):
        t = next((t for t in self.cfg["tags"] if t["tag"] == tag), None)
        if t is None:
            raise ValueError("no tag %r" % tag)
        if t["pending"]:
            raise ValueError("%s has no Modbus address yet" % tag)
        if not pid_of(self.name):
            raise ValueError("%s isn't running; start it first" % self.name)
        try:
            words = encode_value(t["type"], value, t)
        except ValueError as ex:
            raise ValueError("%s: %s" % (tag, ex))
        if t["type"] == "BIT":  # merge into the register from the state file, so no read shows up as a poll
            st = self.state() or {}
            words = [set_bit(st.get(t["table"], {}).get(t["addr"], 0), t["bit"], words[0])]
        try:
            cl = Client("127.0.0.1", self.cfg.get("port"), self.cfg.get("unit_id", 1), 3)
        except SystemExit as ex:
            raise ValueError(str(ex))
        try:
            cl.write(t["table"], t["addr"], words)
        except ModbusException as ex:
            raise ValueError("%s: %s" % (tag, ex))
        except SystemExit as ex:
            raise ValueError(str(ex))
        finally:
            cl.sock.close()


def run_sh(*args):
    p = subprocess.run(["./run.sh"] + list(args), cwd=HERE, capture_output=True, text=True, timeout=60)
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
                return self.send(200, [Machine(n).summary() for n in machine_names()])
            if len(parts) == 3 and parts[:2] == ["api", "m"]:
                m = self.machine(parts[2])
                return self.send(200, {"machine": m.summary(), "tags": m.tags()})
            if len(parts) == 4 and parts[:2] == ["api", "m"] and parts[3] == "log":
                self.machine(parts[2])
                n = min(int(query.get("n", 200)), 2000)
                return self.send(200, {"lines": tail(path(parts[2], ".log"), n)})
            self.send(404, {"error": "not found"})
        except LookupError as ex:
            self.send(404, {"error": str(ex)})
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
            if action == "start":  # also a restart: values go back to the machine file's
                return self.send(200, {"output": "\n".join((run_sh(m).splitlines() or [""])[-1] for m in names)})
            if action == "stop":
                return self.send(200, {"output": "\n".join(run_sh("stop", m) for m in names)})
            if action == "delay":
                ms = max(0, int(body.get("ms", 0)))
                for m in names:
                    if ms:
                        with open(path(m, ".delay"), "w") as f:
                            json.dump({"default": ms}, f)
                    elif os.path.exists(path(m, ".delay")):
                        os.remove(path(m, ".delay"))
                return self.send(200, {"delay": ms})
            if action == "set":
                m = self.machine(name)
                for tag, value in body.items():
                    m.set(tag, value)
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except LookupError as ex:
            self.send(404, {"error": str(ex)})
        except (ValueError, TypeError) as ex:
            self.send(400, {"error": str(ex)})
        except Exception as ex:
            self.send(500, {"error": "%s: %s" % (type(ex).__name__, ex)})


PAGE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Modbus PLC Simulator</title>
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
header .host { color: var(--muted); font-size: 13px; }
header .spacer { flex: 1; }
button { font: inherit; border: 1px solid var(--line); background: var(--panel); color: var(--text);
  padding: 6px 12px; border-radius: 7px; cursor: pointer; white-space: nowrap; }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.danger { color: var(--bad); }
button:disabled { opacity: .5; cursor: default; }
.layout { display: grid; grid-template-columns: 280px 1fr; gap: 16px; padding: 16px 20px; max-width: 1500px; margin: 0 auto; }
@media (max-width: 860px) { .layout { grid-template-columns: 1fr; padding: 12px 16px; } }
.machines { display: flex; flex-direction: column; gap: 8px; }
.mcard { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; cursor: pointer; }
.mcard.sel { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.mcard .row1 { display: flex; align-items: center; gap: 8px; }
.mcard .name { font-weight: 650; }
.mcard .title { color: var(--muted); }
.mcard .meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: none; }
.dot.on { background: var(--ok); }
.pill { font-size: 11.5px; padding: 1px 7px; border-radius: 999px; background: var(--line); color: var(--muted); white-space: nowrap; }
.pill.ok { background: var(--ok-weak); color: var(--ok); }
.pill.warn { background: var(--warn-weak); color: var(--warn); }
.pill.bad { background: var(--bad-weak); color: var(--bad); }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
.detail-head { padding: 14px 16px; border-bottom: 1px solid var(--line); display: flex; gap: 16px; flex-wrap: wrap; align-items: flex-start; }
.detail-head h2 { margin: 0 0 4px; font-size: 18px; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; font-size: 13px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; }
.mono { font-family: var(--mono); font-size: 12.5px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-left: auto; }
.actions label { color: var(--muted); font-size: 13px; display: flex; gap: 6px; align-items: center; }
.actions input { width: 70px; }
input[type=text], input[type=number], input[type=search] { font: inherit; padding: 5px 8px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--bg); color: var(--text); min-width: 0; }
input:focus { outline: 2px solid var(--accent-weak); border-color: var(--accent); }
.banner { margin: 12px 16px 0; padding: 10px 12px; border-radius: 8px; font-size: 13px; }
.banner.ok { background: var(--ok-weak); color: var(--ok); }
.banner.warn { background: var(--warn-weak); color: var(--warn); }
.toolbar { display: flex; gap: 8px; padding: 12px 16px; align-items: center; flex-wrap: wrap; }
.tabs { display: flex; gap: 2px; background: var(--bg); padding: 3px; border-radius: 8px; }
.tabs button { border: 0; background: transparent; padding: 5px 12px; border-radius: 6px; }
.tabs button.sel { background: var(--panel); box-shadow: 0 1px 2px rgba(0,0,0,.12); }
.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 7px 10px; border-top: 1px solid var(--line); vertical-align: middle; }
th { font-size: 12px; color: var(--muted); font-weight: 600; background: var(--panel); position: sticky; top: 0; }
td.val { min-width: 230px; }
tr.pending td { color: var(--muted); }
tr.changed td.val { animation: flash 1.6s ease-out; }
@keyframes flash { from { background: var(--flash); } to { background: transparent; } }
.edit { display: flex; gap: 6px; align-items: center; }
.edit input { width: 160px; }
.edit .hint { color: var(--muted); font-size: 12px; }
.switch { position: relative; width: 40px; height: 22px; border-radius: 11px; background: var(--line); border: 0; padding: 0; flex: none; }
.switch::after { content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: left .12s; box-shadow: 0 1px 2px rgba(0,0,0,.3); }
.switch.on { background: var(--ok); }
.switch.on.alarm { background: var(--bad); }
.switch.on::after { left: 21px; }
.raw { color: var(--muted); font-family: var(--mono); font-size: 11.5px; }
.log { margin: 0; padding: 10px 12px; font-family: var(--mono); font-size: 12px; line-height: 1.5; max-height: 340px; overflow: auto;
  white-space: pre-wrap; word-break: break-all; background: var(--bg); border-radius: 0 0 10px 10px; }
.log .w { color: var(--accent); } .log .e { color: var(--bad); } .log .c { color: var(--muted); }
.section-title { display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-top: 1px solid var(--line); }
.section-title h3 { margin: 0; font-size: 14px; }
.empty { padding: 40px; text-align: center; color: var(--muted); }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg);
  padding: 8px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none; max-width: 90vw; }
#toast.show { opacity: 1; }
#toast.err { background: var(--bad); color: #fff; }
</style>
</head>
<body>
<header>
  <h1>Modbus PLC Simulator</h1>
  <span class="host" id="host"></span>
  <span class="spacer"></span>
  <button class="primary" onclick="act('all','start','Starting every machine… (values reset)')">Start all</button>
  <button class="danger" onclick="act('all','stop','Stopping every machine…')">Stop all</button>
</header>
<div class="layout">
  <div class="machines" id="machines"></div>
  <div class="panel" id="detail"><div class="empty">Loading…</div></div>
</div>
<div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let machines = [], sel = localStorage.getItem("sel"), tab = localStorage.getItem("tab") || "data";
let filter = "", tags = [], prev = {}, editing = null, logOn = true, logQuiet = true;
$("#host").textContent = location.host;

function toast(msg, err) {
  const t = $("#toast"); t.textContent = msg; t.className = "show" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.className = "", err ? 5000 : 2200);
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
  const cls = m.poll.ago < 30 ? "ok" : "warn";
  return `<span class="pill ${cls}" title="last read from ${esc(m.poll.peer)} (${esc(m.poll.framing)})">read ${ago(m.poll.ago)}</span>`;
}

function renderMachines() {
  $("#machines").innerHTML = machines.map(m => `
    <div class="mcard ${m.name === sel ? "sel" : ""}" onclick="pick('${m.name}')">
      <div class="row1"><span class="dot ${m.running ? "on" : ""}"></span><span class="name">${esc(m.name)}</span>
        <span class="title">${esc(m.title)}</span></div>
      <div class="meta">port ${m.port} · unit ${m.unit} &nbsp; ${pollPill(m)}${m.delay ? ` <span class="pill warn">+${m.delay} ms</span>` : ""}</div>
    </div>`).join("");
}
function pick(name) { sel = name; localStorage.setItem("sel", name); prev = {}; editing = null; renderMachines(); refresh(true); }

function valueCell(t, m) {
  if (t.pending) return `<span title="${esc(t.note)}">no Modbus address yet — set "addr" in the machine file</span>`;
  if (!m.running) return `<span class="raw">start: ${esc(fmtVal(t, t.start))}</span>`;
  if (t.error) return `<span class="pill bad">${esc(t.error)}</span>`;
  if (t.type === "BOOL" || t.type === "BIT") {
    const on = Number(t.value) === 1;
    return `<div class="edit"><button class="switch ${on ? "on" : ""} ${t.group === "alarm" ? "alarm" : ""}"
      onclick="setTag(${idx(t)}, ${on ? 0 : 1})" aria-label="toggle"></button><span>${on ? "1" : "0"}</span></div>`;
  }
  const i = idx(t);
  const isStr = t.type === "STRING";
  const v = editing === i ? $("#in" + i)?.value ?? t.value : t.value;
  return `<div class="edit"><input id="in${i}" type="text" value="${esc(v)}" ${isStr ? `maxlength="${t.words * 2}"` : 'inputmode="decimal"'}
      onfocus="editing=${i}" onblur="if(editing===${i})editing=null"
      onkeydown="if(event.key==='Enter'){setTag(${i}, this.value);this.blur()} if(event.key==='Escape'){this.value=tags[${i}].value;this.blur()}">
    <span class="hint">${isStr ? "max " + t.words * 2 + " chars" : ""}</span></div>`;
}
function fmtVal(t, v) { return v === undefined || v === null ? "" : v; }
function idx(t) { return tags.indexOf(t); }
function addr(t) {
  if (t.pending) return t.table + " —";
  return t.table + " " + t.addr + (t.type === "BIT" ? "." + t.bit : "");
}

function renderDetail(data) {
  const m = data.machine, rp = m.real_plc || {};
  tags = data.tags;
  const counts = {data: 0, alarm: 0}, active = {alarm: 0};
  tags.forEach(t => { counts[t.group] = (counts[t.group] || 0) + 1; if (t.group === "alarm" && Number(t.value) === 1) active.alarm++; });
  const shown = tags.filter(t => (tab === "all" || t.group === tab) &&
    (!filter || (t.tag + " " + t.plc + " " + addr(t)).toLowerCase().includes(filter.toLowerCase())));
  const banner = !m.running
    ? `<div class="banner warn">${esc(m.name)} is stopped. Start it to serve values on port ${m.port}.</div>`
    : m.poll ? `<div class="banner ${m.poll.ago < 30 ? "ok" : "warn"}">Last read ${ago(m.poll.ago)} from <span class="mono">${esc(m.poll.peer)}</span> (${esc(m.poll.framing)}, unit ${m.poll.unit}).</div>`
    : `<div class="banner warn">Running, but nothing has read from it yet. Point the device at <span class="mono">127.0.0.1:${m.port}</span>, unit ${m.unit}.</div>`;
  const keepFocus = editing !== null ? editing : null;
  $("#detail").innerHTML = `
    <div class="detail-head">
      <div>
        <h2>${esc(m.title)} <span class="title" style="color:var(--muted);font-weight:400">${esc(m.name)}</span></h2>
        <dl class="kv">
          <dt>Simulator</dt><dd class="mono">127.0.0.1:${m.port} · unit ${m.unit}${m.running ? " · pid " + m.pid : ""}</dd>
          <dt>Real PLC</dt><dd>${esc(rp.make || "")} <span class="mono">${esc(rp.ip || "")}:${esc(rp.port || "")}</span> ${esc(rp.connection || "")}</dd>
        </dl>
      </div>
      <div class="actions">
        <label title="Delay every reply, to test timeouts">Reply delay <input type="number" min="0" step="100" id="delay" value="${m.delay}"
          onkeydown="if(event.key==='Enter')setDelay(this.value)" onchange="setDelay(this.value)"> ms</label>
        <button class="primary" onclick="act(sel,'start','${m.running ? "Restarting (values reset)…" : "Starting…"}')">${m.running ? "Restart / reset values" : "Start"}</button>
        <button class="danger" ${m.running ? "" : "disabled"} onclick="act(sel,'stop','Stopping…')">Stop</button>
      </div>
    </div>
    ${banner}
    <div class="toolbar">
      <div class="tabs">
        ${["data", "alarm", "all"].map(g => `<button class="${tab === g ? "sel" : ""}" onclick="setTab('${g}')">${
          g === "data" ? "Data" : g === "alarm" ? "Alarms" : "All"} <span class="pill ${g === "alarm" && active.alarm ? "bad" : ""}">${
          g === "all" ? tags.length : g === "alarm" && active.alarm ? active.alarm + " on" : counts[g] || 0}</span></button>`).join("")}
      </div>
      <input type="search" id="filter" placeholder="Filter tags or addresses" value="${esc(filter)}" oninput="filter=this.value;refresh()">
      <span class="spacer" style="flex:1"></span>
      ${tab !== "data" ? `<button ${m.running ? "" : "disabled"} onclick="allAlarms(0)">All alarms off</button>` : ""}
    </div>
    <div class="tablewrap"><table>
      <thead><tr><th>Tag</th><th>PLC address</th><th>Modbus</th><th>Type</th><th>Value</th><th>Registers</th></tr></thead>
      <tbody>${shown.map(t => {
        const i = idx(t), key = t.tag, val = JSON.stringify(t.value);
        const changed = prev[key] !== undefined && prev[key] !== val; prev[key] = val;
        return `<tr class="${t.pending ? "pending" : ""} ${changed ? "changed" : ""}">
          <td>${esc(t.tag)}</td><td class="mono">${esc(t.plc)}</td><td class="mono">${esc(addr(t))}</td>
          <td class="mono">${esc(t.type)}${t.type === "STRING" ? " ×" + t.words : ""}</td>
          <td class="val">${valueCell(t, m)}</td><td class="raw">${esc(t.raw || "")}</td></tr>`;
      }).join("") || `<tr><td colspan="6" class="empty">No tags match.</td></tr>`}</tbody>
    </table></div>
    <div class="section-title"><h3>Request log</h3>
      <label class="title" style="font-size:13px;color:var(--muted)"><input type="checkbox" ${logQuiet ? "checked" : ""} onchange="logQuiet=this.checked;loadLog()"> hide connect/disconnect</label>
      <label class="title" style="font-size:13px;color:var(--muted)"><input type="checkbox" ${logOn ? "checked" : ""} onchange="logOn=this.checked"> live</label>
    </div>
    <pre class="log" id="log"></pre>`;
  if (keepFocus !== null) { const el = $("#in" + keepFocus); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
  if (document.activeElement?.id !== "filter" && filter) {}
  loadLog();
}

let logCache = "";
async function loadLog() {
  if (!sel) return;
  if (logOn) {
    try { logCache = (await api(`/api/m/${sel}/log?n=300`)).lines; } catch (e) { logCache = [String(e)]; }
  }
  const el = $("#log"); if (!el) return;
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
  const lines = (logCache || []).filter(l => !logQuiet || !/ (connect|disconnect|speaks Modbus \w+)$/.test(l)).slice(-200);
  el.innerHTML = lines.map(l => `<span class="${/ write /.test(l) ? "w" : / exception |bad |garbled|malformed/.test(l) ? "e" : / (connect|disconnect)$/.test(l) ? "c" : ""}">${esc(l)}</span>`).join("\n") || "(empty)";
  if (atBottom || !el._init) { el.scrollTop = el.scrollHeight; el._init = true; }
}

async function refresh(full) {
  try {
    machines = await api("/api/machines");
    if (!sel || !machines.some(m => m.name === sel)) sel = machines[0]?.name;
    renderMachines();
    if (!sel) { $("#detail").innerHTML = '<div class="empty">No machines in machines/.</div>'; return; }
    const d = await api(`/api/m/${sel}`);
    const filterFocused = document.activeElement?.id === "filter";
    const logEl = $("#log"), logScroll = logEl && logEl.scrollTop;
    renderDetail(d);
    if (filterFocused) { const f = $("#filter"); f.focus(); f.setSelectionRange(f.value.length, f.value.length); }
  } catch (e) { toast("UI can't reach the server: " + e.message, true); }
}
function setTab(g) { tab = g; localStorage.setItem("tab", g); refresh(); }

async function setTag(i, value) {
  const t = tags[i]; editing = null;
  try { await api(`/api/m/${sel}/set`, {[t.tag]: value}); toast(`${t.tag} = ${value}`); }
  catch (e) { toast(e.message, true); }
  refresh();
}
async function allAlarms(v) {
  const body = {};
  tags.filter(t => t.group === "alarm" && !t.pending && Number(t.value) !== v).forEach(t => body[t.tag] = v);
  if (!Object.keys(body).length) return toast("Every alarm is already off");
  try { await api(`/api/m/${sel}/set`, body); toast(Object.keys(body).length + " alarms turned off"); }
  catch (e) { toast(e.message, true); }
  refresh();
}
async function setDelay(ms) {
  try { const r = await api(`/api/m/${sel}/delay`, {ms: Number(ms) || 0}); toast(r.delay ? `Replies delayed by ${r.delay} ms` : "Delay off"); }
  catch (e) { toast(e.message, true); }
  refresh();
}
async function act(name, action, msg) {
  if (action === "start" && !confirm(name === "all" ? "Start (or restart) every machine? Values go back to the machine files." : `${msg}\nValues go back to the machine file's. Continue?`)) return;
  toast(msg);
  try { const r = await api(`/api/m/${name}/${action}`, {}); toast(r.output?.split("\n").pop() || "Done"); }
  catch (e) { toast(e.message, true); }
  refresh();
}
refresh();
setInterval(() => { if (editing === null) refresh(); }, 2000);
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
