#!/usr/bin/env python3
"""Builds simulator files from a Porta Studio project export, so the simulator matches what the build compiles
into ptcsw: one file per Studio port, with every device on it (by slave id) and every enabled datapoint.

  python3 studio_to_init.py "포타콘 미들웨어 크래쉬 버그 테스팅.json"              -> machines/com1.init.json, ...
  python3 studio_to_init.py project.json --prefix crash                          -> machines/crash-com1.init.json, ...

Studio port -> simulator endpoint (a pty for serial ports; put the path printed below into application.json):
  rs232 (COM1) -> pty ttyCOM1      rs485 (COM2) -> pty ttyCOM2      com3 (COM3) -> pty ttyCOM3
  tcp          -> a TCP port per device (5030 and up unless the device names one)
Datapoints: registerType -> table, address as is (0-based), dataType -> type, endianness -> word order.
Disabled datapoints and ones with no address are left out. Expressions aren't applied: the simulator holds raw
values and ptcsw applies them. Starting values make each DP recognisable: dp81 starts at 81.5 (FLOAT32) or 81.
Regenerating keeps the values, control ports and schedules already in the files.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from modbus_plc_sim import encode_value, word_count  # noqa: E402

PORTS = {  # Studio interface key -> (simulator name, Studio port, application.json key, control port)
    "rs232": ("com1", "COM1", "interfaces.rs232.port", 6101),
    "rs485": ("com2", "COM2", "interfaces.rs485.port", 6102),
    "com3": ("com3", "COM3", "interfaces.com3.port", 6103),
}
PROTOCOLS = {"modbusrtu": "rtu", "modbusascii": "ascii", "modbustcp": "tcp"}
TABLES = {"coil": "coil", "coils": "coil", "discreteinput": "di", "inputregister": "ir", "holdingregister": "hr"}
TYPES = {"BOOL", "INT16", "UINT16", "INT32", "UINT32", "FLOAT32"}


def starting_value(name, typ):
    n = int(re.sub(r"\D", "", name) or 1) % 30000
    return {"BOOL": 1, "FLOAT32": n + 0.5}.get(typ, n)


def dp_tag(name, dp, notes):
    table = TABLES.get(str(dp.get("registerType", "")).replace("_", "").lower())
    if not table:
        raise ValueError("register type %r" % dp.get("registerType"))
    typ = str(dp.get("dataType") or "UINT16").upper()
    if typ not in TYPES:
        raise ValueError("data type %r" % typ)
    if table in ("coil", "di") and typ != "BOOL":
        notes.append("%s is %s in a bit table; served as BOOL" % (name, typ))
        typ = "BOOL"
    if table in ("hr", "ir") and typ == "BOOL":
        notes.append("%s is BOOL in a register table; served as UINT16 (0/1)" % name)
        typ = "UINT16"
    t = {"tag": name, "table": table, "addr": int(str(dp["address"]).strip()), "type": typ}
    if word_count(typ, {}) == 2:
        t["word_order"] = (dp.get("endianness") or "big").lower()
        w = dp.get("write") or {}
        if w.get("enabled") and (w.get("endianness") or t["word_order"]).lower() != t["word_order"]:
            t["note"] = "read word order %s, write word order %s: a written value reads back differently" % (
                t["word_order"], w["endianness"])
            notes.append("%s: %s" % (name, t["note"]))
    t["fc"] = dp.get("functionCode", "")
    if (dp.get("write") or {}).get("enabled"):
        t["write"] = {k: v for k, v in dp["write"].items() if k != "enabled" and v not in ("", None)}
    if dp.get("expression"):
        t["expression"] = dp["expression"]
    if dp.get("timeoutMs") is not None:
        t["timeout_ms"] = dp["timeoutMs"]
    return t


def build_device(dev, notes):
    tags, used = [], {}
    for name, dp in (dev.get("datapointConfigs") or {}).items():
        if not dp.get("enabled") or str(dp.get("address", "")).strip() == "":
            continue
        try:
            t = dp_tag(name, dp, notes)
        except (ValueError, KeyError) as ex:
            notes.append("%s %s left out: %s" % (dev.get("name"), name, ex))
            continue
        span = range(t["addr"], t["addr"] + word_count(t["type"], t))
        clash = sorted({used[(t["table"], a)] for a in span if (t["table"], a) in used})
        if clash:
            notes.append("%s overlaps %s on %s %d; it reads whatever they hold" % (name, ", ".join(clash),
                                                                                  t["table"], t["addr"]))
        else:
            t["value"] = starting_value(name, t["type"])
            encode_value(t["type"], t["value"], t)
        for a in span:
            used.setdefault((t["table"], a), name)
        tags.append(t)
    tags.sort(key=lambda t: (t["table"], t["addr"]))
    return {"unit": int(dev.get("slaveId") or 1), "name": dev.get("name", ""), "protocol": dev.get("protocolType"),
            "studio_id": dev.get("id"), "tags": tags}


def merge_old(path, new):
    """Keep values, control port and schedules from an earlier generated file."""
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return
    for k in ("control_port", "schedules", "refuse", "refuse_code"):
        if k in old:
            new[k] = old[k]
    if str(old.get("endpoint", "")).startswith("tcp:") and str(new["endpoint"]).startswith("tcp:"):
        new["endpoint"] = old["endpoint"]
    vals = {(d.get("unit"), t.get("tag")): t["value"] for d in old.get("devices", []) for t in d.get("tags", [])
            if "value" in t}
    for d in new["devices"]:
        for t in d["tags"]:
            if (d["unit"], t["tag"]) in vals and "value" in t:
                t["value"] = vals[(d["unit"], t["tag"])]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="Porta Studio project JSON")
    ap.add_argument("--prefix", default="", help="name prefix for the files, e.g. crash -> crash-com1")
    ap.add_argument("--out", default=os.path.join(HERE, "machines"))
    a = ap.parse_args()
    with open(a.project, encoding="utf-8") as f:
        proj = json.load(f)
    prefix = a.prefix + "-" if a.prefix else ""
    files, notes, tcp_port = [], [], 5030
    for key, iface in (proj.get("interfaces") or {}).items():
        devs = iface.get("devices") or []
        if not devs:
            continue
        groups = []  # (file name, endpoint, Studio port, application.json key, control port, devices)
        if key in PORTS:
            name, port, app_key, ctl = PORTS[key]
            groups.append((name, "pty:tty%s" % port, port, app_key, ctl, devs))
        elif key == "tcp":
            for i, dev in enumerate(devs):
                p = int(dev.get("port") or dev.get("tcpPort") or tcp_port)
                tcp_port = max(tcp_port, p) + 1
                groups.append(("tcp%d" % (i + 1), "tcp:%d" % p, "TCP", "the device's IP and port", p + 1000, [dev]))
        else:
            notes.append("interface %r isn't known; its %d device(s) are left out" % (key, len(devs)))
            continue
        for name, endpoint, port, app_key, ctl, group in groups:
            devices, framing = [], None
            for dev in group:
                proto = PROTOCOLS.get(str(dev.get("protocolType", "")).lower())
                if not proto:
                    notes.append("%s on %s uses %s, which this simulator doesn't speak; left out"
                                 % (dev.get("name"), port, dev.get("protocolType")))
                    continue
                if framing and proto != framing:
                    notes.append("%s mixes %s and %s on one port" % (port, framing, proto))
                framing = framing or proto
                d = build_device(dev, notes)
                if any(x["unit"] == d["unit"] for x in devices):
                    notes.append("%s: two devices with slave id %d; only the first is simulated" % (port, d["unit"]))
                    continue
                devices.append(d)
            if not devices:
                continue
            fname = prefix + name
            path = os.path.join(a.out, fname + ".init.json")
            out = {
                "comment": "Generated by studio_to_init.py from %s. addr is the Studio address, sent as is."
                           % os.path.basename(a.project),
                "machine": "%s (%s)" % (port, ", ".join(d["name"] for d in devices)),
                "studio_port": port,
                "application_json": app_key,
                "endpoint": endpoint,
                "framing": framing,
                "control_port": ctl,
                "devices": devices,
            }
            merge_old(path, out)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
                f.write("\n")
            files.append((fname, out))
    for fname, out in files:
        print("machines/%s.init.json  %s %s, control port %d" % (fname, out["studio_port"], out["endpoint"],
                                                                out["control_port"]))
        for d in out["devices"]:
            print("  slave %d %s: %s" % (d["unit"], d["name"], ", ".join(
                "%s=%s %d %s" % (t["tag"], t["table"], t["addr"], t["type"]) for t in d["tags"]) or "no datapoints"))
    for n in notes:
        print("note: " + n)
    if not files:
        print("no Modbus devices with enabled datapoints found")
        return 1
    print("\nStart them with ./run.sh %s (or ./run.sh all). For serial ports, put the pty path in application.json\n"
          "(through IoTLink, so a cloud deploy doesn't put /dev/ttyAMA* back): ~/modbus-plc-simulator/ttyCOM1 ..."
          % " ".join(f for f, _ in files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
