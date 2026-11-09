#!/usr/bin/env python3
"""Modbus device simulator for testing ptcsw (or any Modbus master). Python 3 standard library only.

One process per endpoint, as Porta Studio has one device list per port:
  "endpoint": "pty:ttyCOM1"   a virtual serial port. A symlink at that path (relative paths are inside this
                              folder) points at a pty; put its absolute path in application.json and ptcsw opens
                              it as its COM port. Baud and parity don't matter on a pty.
  "endpoint": "tcp:5020"      a TCP (and UDP) port, for Modbus TCP masters.
Framing (RTU, ASCII, Modbus TCP) is fixed with "framing" or detected from the first bytes.

Every unit id has its own tables. Units listed under "devices" get their tags' starting values; any other unit id
also answers (reading 0), and the log notes it once. Unit 0 on RTU/ASCII is a broadcast: writes are applied and
nothing is sent back.

Tables, with 0-based protocol addresses (Studio's address is sent as is; manual "40001" is usually hr 0):
  coil  (0x)  read/write bits        FC 01 read, FC 05 write single, FC 15 write multiple
  di    (1x)  read-only bits         FC 02 read
  ir    (3x)  read-only registers    FC 04 read
  hr    (4x)  read/write registers   FC 03 read, FC 06 write single, FC 16 write multiple

Test controls:
  - Values persist across restarts in --state (with per-tag write counts); delete it to go back to the starting values.
  - --delay-file: {"default": ms, "unit": {"2": ms}, "fc": {"4": ms}, "cycle": {"ms": [500, 1000], "every_s": 10}},
    re-read within 0.1 s of a change. A function code entry wins over a unit entry, which wins over the default
    (or the cycle). On a pty, requests that arrive while a delayed reply is pending are dropped, as a busy device
    would; on TCP they queue.
  - Schedules (config "schedules", plus --schedule-file re-read on change) step values over time:
    {"unit": 1, "tag": "dp81", "values": [5, 10], "every_s": 5}
  - --refuse (or "refuse" in the file) makes writes to chosen ranges answer with an exception, by default code 02
    for hr and coil 9000-9099. Reads still succeed.
  - --control-port (127.0.0.1 only) is a Modbus TCP port for people and tools: modbus_client.py and the web UI
    change values there, so they never become a second master on a pty, and their writes aren't counted.
    It also accepts FC 65 (write any table, including di/ir) and FC 66 (reset the write counts).
"""
import argparse
import asyncio
import json
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

TABLES = ("coil", "di", "ir", "hr")
BIT_TABLES = ("coil", "di")
TABLE_ALIASES = {
    "coil": "coil", "coils": "coil", "co": "coil", "0x": "coil",
    "di": "di", "discrete": "di", "discrete_input": "di", "discrete_inputs": "di", "discreteinput": "di", "1x": "di",
    "ir": "ir", "input": "ir", "input_register": "ir", "input_registers": "ir", "inputregister": "ir", "3x": "ir",
    "hr": "hr", "holding": "hr", "holding_register": "hr", "holding_registers": "hr", "holdingregister": "hr",
    "4x": "hr",
}
READ_FC = {"coil": 1, "di": 2, "hr": 3, "ir": 4}
TABLE_CODES = {"coil": 0, "di": 1, "ir": 3, "hr": 4}  # table byte of the FC 65 back door
BY_TABLE_CODE = {v: k for k, v in TABLE_CODES.items()}
FC_BACKDOOR = 0x41      # control port only: write any table
FC_RESET_COUNTS = 0x42  # control port only: zero the write counts

EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_ADDRESS = 0x02
EXC_ILLEGAL_VALUE = 0x03
EXC_DEVICE_FAILURE = 0x04
EXC_GATEWAY_NO_RESPONSE = 0x0B
EXCEPTION_NAMES = {
    EXC_ILLEGAL_FUNCTION: "illegal function", EXC_ILLEGAL_ADDRESS: "illegal data address",
    EXC_ILLEGAL_VALUE: "illegal data value", EXC_DEVICE_FAILURE: "server device failure",
    0x06: "server device busy", EXC_GATEWAY_NO_RESPONSE: "gateway target device failed to respond",
}
MAX_READ_BITS, MAX_READ_REGS = 2000, 125
MAX_WRITE_BITS, MAX_WRITE_REGS = 1968, 123
DEFAULT_PORT = 5020
LOG_MAX_BYTES = 20 * 1024 * 1024  # then <log> moves to <log>.1
DEFAULT_REFUSE = "hr:9000-9099,coil:9000-9099"


class Log:
    """Timestamped lines (ms) to stdout, or to a file that rotates once it passes LOG_MAX_BYTES."""

    def __init__(self):
        self.path, self.f = None, None

    def open(self, path):
        self.path = path
        self.f = open(path, "a", encoding="utf-8")

    def __call__(self, msg):
        t = time.time()
        line = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + ".%03d " % int(t % 1 * 1000) + msg
        if not self.f:
            print(line, flush=True)
            return
        self.f.write(line + "\n")
        self.f.flush()
        if self.f.tell() > LOG_MAX_BYTES:
            self.f.close()
            os.replace(self.path, self.path + ".1")
            self.f = open(self.path, "a", encoding="utf-8")


log = Log()


class ModbusError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def table_name(s):
    t = TABLE_ALIASES.get(str(s).strip().lower())
    if not t:
        raise ValueError("unknown table %r (use coil, di, ir or hr)" % s)
    return t


# ---------------------------------------------------------------- value codec (shared with modbus_client.py)

SIZES = {"INT16": 1, "UINT16": 1, "INT32": 2, "UINT32": 2, "FLOAT32": 2}
RANGES = {"INT16": (-0x8000, 0x7FFF), "UINT16": (0, 0xFFFF),
          "INT32": (-0x80000000, 0x7FFFFFFF), "UINT32": (0, 0xFFFFFFFF)}


def to_int(v):
    if isinstance(v, int):
        return v
    s = str(v).strip()
    try:
        return int(s, 10)
    except ValueError:
        return int(s, 0)  # 0x1F, 0b101


def parse_bool(v):
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return 1
    if s in ("0", "false", "off", "no"):
        return 0
    raise ValueError("not a BOOL: %r (use 1/0, true/false, on/off)" % v)


def _order(opts, key):
    v = str(opts.get(key, "big")).lower()
    if v not in ("big", "little"):
        raise ValueError("%s must be big or little, not %r" % (key, v))
    return v


def word_count(typ, opts):
    """Registers (or bits, for BOOL) a value of this type occupies."""
    typ = typ.upper()
    if typ in SIZES:
        return SIZES[typ]
    if typ in ("BOOL", "BIT"):
        return 1
    if opts.get("words"):
        return int(opts["words"])
    v = opts.get("value")
    if typ == "STRING" and v is not None:
        return max(1, (len(str(v).encode(opts.get("encoding", "utf-8"))) + 1) // 2)
    if typ == "WORDS" and isinstance(v, list):
        return len(v)
    if typ in ("STRING", "WORDS"):
        raise ValueError('%s needs "words"' % typ)
    raise ValueError("unknown type %r" % typ)


def encode_value(typ, val, opts=None):
    """Value -> list of register words (or [0/1] for BOOL and BIT: merge a BIT into its register with set_bit).
    opts: word_order (32-bit types), byte_order and encoding (STRING), words (STRING length), bit (BIT).
    "big" word order = high word at the lower address; "big" byte order = first character in the high byte."""
    opts = opts or {}
    typ = typ.upper()
    if typ in ("BOOL", "BIT"):
        return [parse_bool(val)]
    if typ in RANGES or typ == "FLOAT32":
        if typ == "FLOAT32":
            v = struct.unpack(">I", struct.pack(">f", float(val)))[0]
        else:
            v = to_int(val)
            lo, hi = RANGES[typ]
            if not lo <= v <= hi:
                raise ValueError("%d is out of range for %s (%d..%d)" % (v, typ, lo, hi))
        if SIZES[typ] == 1:
            return [v & 0xFFFF]
        v &= 0xFFFFFFFF
        hi_w, lo_w = v >> 16, v & 0xFFFF
        return [hi_w, lo_w] if _order(opts, "word_order") == "big" else [lo_w, hi_w]
    if typ == "WORDS":
        vals = val if isinstance(val, list) else str(val).split()
        return [to_int(x) & 0xFFFF for x in vals]
    if typ == "STRING":
        raw = str(val).encode(opts.get("encoding", "utf-8"))
        n = int(opts.get("words") or max(1, (len(raw) + 1) // 2))
        if len(raw) > n * 2:
            raise ValueError("string %r is %d bytes, longer than %d words" % (val, len(raw), n))
        raw = raw.ljust(n * 2, b"\0")
        big = _order(opts, "byte_order") == "big"
        return [(raw[2 * i] << 8) | raw[2 * i + 1] if big else raw[2 * i] | (raw[2 * i + 1] << 8) for i in range(n)]
    raise ValueError("unknown type %r (use BOOL BIT INT16 UINT16 INT32 UINT32 FLOAT32 STRING WORDS)" % typ)


def bit_number(opts):
    b = to_int(opts.get("bit", -1))
    if not 0 <= b <= 15:
        raise ValueError('BIT needs "bit": 0-15 (the bit inside the register)')
    return b


def set_bit(word, bit, value):
    """The register word with one bit set or cleared; used to write a BIT tag."""
    return (word | (1 << bit)) if value else (word & ~(1 << bit) & 0xFFFF)


def decode_value(typ, words, opts=None):
    """Inverse of encode_value."""
    opts = opts or {}
    typ = typ.upper()
    if typ == "BOOL":
        return words[0] & 1
    if typ == "BIT":
        return (words[0] >> bit_number(opts)) & 1
    if typ == "INT16":
        return words[0] - 0x10000 if words[0] & 0x8000 else words[0]
    if typ == "UINT16":
        return words[0]
    if typ in ("INT32", "UINT32", "FLOAT32"):
        hi_w, lo_w = (words[0], words[1]) if _order(opts, "word_order") == "big" else (words[1], words[0])
        v = (hi_w << 16) | lo_w
        if typ == "FLOAT32":
            return float("%.7g" % struct.unpack(">f", struct.pack(">I", v))[0])
        return v - 0x100000000 if typ == "INT32" and v & 0x80000000 else v
    if typ == "WORDS":
        return list(words)
    if typ == "STRING":
        big = _order(opts, "byte_order") == "big"
        raw = bytearray()
        for w in words:
            raw.extend((w >> 8, w & 0xFF) if big else (w & 0xFF, w >> 8))
        return bytes(raw).split(b"\0", 1)[0].decode(opts.get("encoding", "utf-8"), "replace")
    raise ValueError("unknown type %r" % typ)


# ---------------------------------------------------------------- simulator file

def _norm_tag(e, defaults, path, unit):
    t = dict(defaults)
    t.update(e)
    name = t.get("tag", "%s %s" % (t["table"], t.get("addr")))
    try:
        t["table"] = table_name(t["table"])
        t["pending"] = t.get("addr") is None
        bit = t["table"] in BIT_TABLES
        t["type"] = str(t.get("type", "BOOL" if bit else "UINT16")).upper()
        if bit != (t["type"] == "BOOL"):
            raise ValueError("BOOL goes in coil/di; every other type, including BIT, in hr/ir")
        if t["type"] == "BIT":
            t["bit"] = bit_number(t)
        t["words"] = word_count(t["type"], t)
        if not t["pending"]:
            t["addr"] = to_int(t["addr"])
            if not 0 <= t["addr"] <= 0xFFFF - t["words"] + 1:
                raise ValueError("address %d is outside 0-65535" % t["addr"])
        if "value" in t:
            encode_value(t["type"], t["value"], t)
    except (ValueError, KeyError) as ex:
        raise SystemExit("%s: unit %d tag %r: %s" % (path, unit, name, ex))
    t.setdefault("tag", name)
    t["unit"] = unit
    return t


def parse_endpoint(spec, path=""):
    """'pty:ttyCOM1' -> {"kind": "pty", "path": <absolute>}; 'tcp:5020' or 'tcp:0.0.0.0:5020' -> kind tcp."""
    kind, _, rest = str(spec).partition(":")
    if kind == "pty" and rest:
        return {"kind": "pty", "path": os.path.join(HERE, os.path.expanduser(rest))}
    if kind == "tcp" and rest:
        host, _, port = rest.rpartition(":")
        return {"kind": "tcp", "bind": host or "0.0.0.0", "port": int(port)}
    raise SystemExit("%s: endpoint %r: use pty:<path> or tcp:<port>" % (path, spec))


def load_config(path):
    """A simulator file. The full form:
      {"endpoint": "pty:ttyCOM1", "framing": "rtu", "control_port": 6101, "defaults": {...},
       "devices": [{"unit": 1, "name": "port 1 device", "tags": [...]}, ...],
       "schedules": [{"unit": 1, "tag": "dp81", "values": [5, 10], "every_s": 5}],
       "refuse": "hr:9000-9099", "refuse_code": 2}
    Each tag is like {"tag": "투입수", "table": "hr", "addr": 1104, "type": "INT32", "value": 120345};
    "addr": null marks a tag whose address is still unknown (listed, not served). "defaults" (word_order,
    byte_order, encoding) apply to every tag that doesn't set its own.
    The short form {"port": 5020, "unit_id": 5, "tags": [...]} is one device on a TCP endpoint.
    Returns the file with "devices" as {unit: device}, every tag normalised (table, type, size in "words",
    "pending", "unit"), all tags in "tags", and "endpoint" parsed."""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if isinstance(cfg, list):
        cfg = {"tags": cfg}
    defaults = cfg.get("defaults", {})
    if "devices" in cfg:
        devs = cfg["devices"]
    else:
        devs = [{"unit": cfg.get("unit_id", 1), "name": cfg.get("machine"), "tags": cfg.get("tags", [])}]
    devices, tags = {}, []
    for d in devs:
        unit = int(d.get("unit", 1))
        if not 1 <= unit <= 247:
            raise SystemExit("%s: unit %d is outside 1-247" % (path, unit))
        if unit in devices:
            raise SystemExit("%s: unit %d is listed twice" % (path, unit))
        dd = dict(defaults)
        dd.update(d.get("defaults", {}))
        dev = {k: v for k, v in d.items() if k not in ("tags", "defaults")}
        dev["unit"], dev["name"] = unit, d.get("name") or "unit %d" % unit
        dev["tags"] = [_norm_tag(e, dd, path, unit) for e in d.get("tags", []) if "table" in e]
        devices[unit] = dev
        tags += dev["tags"]
    cfg["devices"], cfg["tags"] = devices, tags
    if "endpoint" not in cfg:
        cfg["endpoint"] = "tcp:%d" % cfg.get("port", DEFAULT_PORT)
    cfg["endpoint_spec"] = cfg["endpoint"]
    cfg["endpoint"] = ep = parse_endpoint(cfg["endpoint"], path)
    cfg["port"] = ep.get("port")
    if cfg.get("framing") not in (None, "rtu", "ascii", "tcp"):
        raise SystemExit("%s: framing must be rtu, ascii or tcp" % path)
    if not cfg.get("control_port"):
        if ep["kind"] != "tcp":
            raise SystemExit("%s: a pty endpoint needs a \"control_port\"" % path)
        cfg["control_port"] = ep["port"] + 1000
    return cfg


def find_tag(cfg, name, unit=None):
    """A tag by name (exact, then ignoring spaces and case), on one unit or on any. Raises ValueError."""
    tags = [t for t in cfg["tags"] if unit is None or t["unit"] == unit]
    for exact in (True, False):
        hits = [t for t in tags if (t["tag"] == name if exact else
                                    t["tag"].replace(" ", "").lower() == name.replace(" ", "").lower())]
        if len({t["unit"] for t in hits}) > 1:
            raise ValueError("tag %r is on units %s; say which unit" % (name, ", ".join(str(t["unit"]) for t in hits)))
        if hits:
            return hits[0]
    raise ValueError("no tag %r%s" % (name, "" if unit is None else " on unit %d" % unit))


# ---------------------------------------------------------------- memory: one set of tables per unit id

class Memory:
    def __init__(self, devices, state_path=None, refuse=(), refuse_code=EXC_ILLEGAL_ADDRESS):
        self.devices = devices
        self.state_path = state_path
        self.units = {}   # unit -> {table: {addr: value}}
        self.writes = {}  # unit -> {tag name or "hr 10": count of master writes}
        self.refuse, self.refuse_code = list(refuse), refuse_code

    def tables(self, unit):
        return self.units.setdefault(unit, {t: {} for t in TABLES})

    @staticmethod
    def _check_range(table, addr, n):
        if addr < 0 or addr + n > 0x10000:
            raise ModbusError(EXC_ILLEGAL_ADDRESS, "%s %d x%d runs past address 65535" % (table, addr, n))

    def read(self, unit, table, addr, n):
        self._check_range(table, addr, n)
        m = self.tables(unit)[table]
        return [m.get(addr + i, 0) for i in range(n)]

    def write(self, unit, table, addr, values, master=True):
        """master=True: a write from the device's master, which --refuse applies to and which is counted."""
        self._check_range(table, addr, len(values))
        if master:
            for rt, lo, hi in self.refuse:
                if rt == table and addr <= hi and addr + len(values) - 1 >= lo:
                    raise ModbusError(self.refuse_code, "write to refused range %s %d-%d" % (rt, lo, hi))
        mask = 1 if table in BIT_TABLES else 0xFFFF
        m = self.tables(unit)[table]
        for i, v in enumerate(values):
            m[addr + i] = v & mask
        if master:
            self._count(unit, table, addr, len(values))

    def _count(self, unit, table, addr, n):
        counts = self.writes.setdefault(unit, {})
        hit = False
        for t in self.tags(unit, table, addr, n, whole=False):
            counts[t["tag"]] = counts.get(t["tag"], 0) + 1
            hit = True
        if not hit:
            key = "%s %d" % (table, addr) + ("" if n == 1 else " x%d" % n)
            counts[key] = counts.get(key, 0) + 1

    def tags(self, unit, table, addr, n, whole=True):
        """Tags of this unit inside (whole=True) or touching (whole=False) table[addr .. addr+n-1]."""
        dev = self.devices.get(unit)
        out = []
        for t in dev["tags"] if dev else ():
            if t["pending"] or t["table"] != table:
                continue
            a, b = t["addr"], t["addr"] + t["words"]
            if (addr <= a and b <= addr + n) if whole else (a < addr + n and addr < b):
                out.append(t)
        return out

    def seed(self):
        """The starting values from the tags."""
        for dev in self.devices.values():
            for t in dev["tags"]:
                if "value" not in t or t["pending"]:
                    continue
                words = encode_value(t["type"], t["value"], t)
                if t["type"] == "BIT":  # several BIT tags share one register
                    words = [set_bit(self.read(t["unit"], t["table"], t["addr"], 1)[0], t["bit"], words[0])]
                self.write(t["unit"], t["table"], t["addr"], words, master=False)

    def load_state(self):
        """Values and write counts saved by an earlier run, over the starting values. Returns True if loaded."""
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            units = data["units"]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        for u, ts in units.items():
            for table, m in ts.items():
                self.tables(int(u))[table_name(table)].update({int(a): int(v) for a, v in m.items()})
        self.writes = {int(u): dict(c) for u, c in data.get("writes", {}).items()}
        return True

    def describe(self, unit, table, addr, values):
        """'  {투입수=120345, 양품수=120308}' for the tags lying entirely inside this read or write."""
        parts = []
        for t in self.tags(unit, table, addr, len(values)):
            off = t["addr"] - addr
            try:
                parts.append("%s=%r" % (t["tag"], decode_value(t["type"], values[off:off + t["words"]], t)))
            except Exception:
                pass
        return "  {%s}" % ", ".join(parts) if parts else ""

    def save(self):
        if not self.state_path:
            return
        data = {"saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "units": {str(u): {t: {str(a): v for a, v in sorted(m.items())} for t, m in ts.items()}
                          for u, ts in sorted(self.units.items())},
                "writes": {str(u): c for u, c in sorted(self.writes.items())}}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)


def parse_refuse(spec):
    out = []
    for part in filter(None, (spec or "").split(",")):
        m = re.fullmatch(r"([A-Za-z0-9_]+):(\d+)(?:-(\d+))?", part.strip())
        try:
            out.append((table_name(m.group(1)), int(m.group(2)), int(m.group(3) or m.group(2))))
        except (AttributeError, ValueError):
            raise SystemExit("bad --refuse part %r (expect e.g. hr:9000-9099)" % part)
    return out


class Watched:
    """A JSON file re-read when it changes. parse(data) returns the new setting or raises ValueError."""

    def __init__(self, path, what):
        self.path, self.what, self.mtime = path, what, None

    def changed(self):
        """None if unchanged, else the parsed contents ({} for a missing or empty file)."""
        if not self.path:
            return None
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            mtime = 0
        if mtime == self.mtime:
            return None
        self.mtime = mtime
        if not mtime:
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                text = f.read().strip()
            return json.loads(text) if text else {}
        except (OSError, ValueError) as ex:
            log("ignoring bad %s file %s, keeping the previous one: %s" % (self.what, self.path, ex))
            return None


class Delay:
    """Reply delay in ms: {"default": ms, "unit": {"1": ms}, "fc": {"4": ms}, "cycle": {"ms": [..], "every_s": s}}."""

    def __init__(self, path):
        self.file = Watched(path, "delay")
        self.default, self.unit, self.fc, self.cycle, self.since = 0, {}, {}, None, 0
        self.last = None

    def refresh(self):
        cfg = self.file.changed()
        if cfg is None:
            return
        try:
            default = int(cfg.get("default", 0))
            unit = {int(k): int(v) for k, v in cfg.get("unit", {}).items()}
            fc = {int(k): int(v) for k, v in cfg.get("fc", {}).items()}
            cycle = cfg.get("cycle")
            if cycle:
                cycle = ([int(v) for v in cycle["ms"]], float(cycle["every_s"]))
                if not cycle[0] or cycle[1] <= 0:
                    raise ValueError("cycle needs ms values and every_s > 0")
            if min([default, *unit.values(), *fc.values(), *(cycle[0] if cycle else [])]) < 0:
                raise ValueError("delays must not be negative")
        except (ValueError, TypeError, AttributeError, KeyError) as ex:
            log("ignoring bad delay file %s, keeping the previous delays: %s" % (self.file.path, ex))
            return
        self.default, self.unit, self.fc, self.cycle, self.since = default, unit, fc, cycle, time.monotonic()
        log("reply delay: default=%dms unit=%s fc=%s%s" % (default, unit, fc, " cycle=%s every %gs" % cycle
                                                            if cycle else ""))
        self.last = None

    def base(self):
        if self.cycle:
            values, every = self.cycle
            return values[int((time.monotonic() - self.since) // every) % len(values)]
        return self.default

    def tick(self):
        self.refresh()
        b = self.base()
        if self.cycle and b != self.last:
            log("reply delay now %d ms (cycle)" % b)
        self.last = b

    def ms(self, unit, fc):
        return self.fc.get(fc, self.unit.get(unit, self.base()))


class Schedules:
    """Values that step over time: {"unit": 1, "tag": "dp81", "values": [5, 10], "every_s": 5}, or with
    "table"/"addr"/"type" instead of "tag". Applied as control writes: not counted, refuse doesn't apply."""

    def __init__(self, mem, cfg, path):
        self.mem, self.cfg = mem, cfg
        self.file = Watched(path, "schedule")
        self.base = self._parse_all(cfg.get("schedules", []), "the simulator file")
        self.extra, self.state = [], {}

    def _parse(self, s):
        unit = int(s.get("unit") or (min(self.cfg["devices"]) if self.cfg["devices"] else 1))
        if "tag" in s:
            t = find_tag(self.cfg, s["tag"], unit)
            if t["pending"]:
                raise ValueError("%s has no address" % t["tag"])
        else:
            t = _norm_tag({k: v for k, v in s.items() if k not in ("values", "every_s", "unit")},
                          self.cfg.get("defaults", {}), "schedule", unit)
        values = list(s["values"])
        words = [encode_value(t["type"], v, t) for v in values]
        every = float(s["every_s"])
        if not values or every <= 0:
            raise ValueError("needs values and every_s > 0")
        return {"t": t, "values": values, "words": words, "every": every, "since": time.monotonic(),
                "label": "unit %d %s" % (unit, t["tag"])}

    def _parse_all(self, items, where):
        out = []
        for s in items:
            try:
                out.append(self._parse(s))
            except (ValueError, KeyError, TypeError) as ex:
                log("ignoring schedule %s in %s: %s" % (json.dumps(s, ensure_ascii=False), where, ex))
        return out

    def tick(self):
        data = self.file.changed()
        if data is not None:
            items = data if isinstance(data, list) else data.get("schedules", [])
            self.extra = self._parse_all(items, self.file.path)
            self.state = {}
            log("schedules: %s" % ("; ".join("%s through %s every %gs" % (s["label"], s["values"], s["every"])
                                              for s in self.base + self.extra) or "none"))
        changed = False
        for i, s in enumerate(self.base + self.extra):
            k = int((time.monotonic() - s["since"]) // s["every"]) % len(s["values"])
            if self.state.get(i) == k:
                continue
            self.state[i] = k
            t, words = s["t"], s["words"][k]
            if t["type"] == "BIT":
                words = [set_bit(self.mem.read(t["unit"], t["table"], t["addr"], 1)[0], t["bit"], words[0])]
            self.mem.write(t["unit"], t["table"], t["addr"], words, master=False)
            log("schedule: %s = %r" % (s["label"], s["values"][k]))
            changed = True
        if changed:
            self.mem.save()


# ---------------------------------------------------------------- PDU handling

def fmt_values(table, vals):
    if table in BIT_TABLES:
        return "".join(map(str, vals))
    hexs = " ".join("%04X" % w for w in vals)
    return "[%s] (%d)" % (hexs, vals[0]) if len(vals) == 1 else "[%s]" % hexs


def _check_qty(qty, hi):
    if not 1 <= qty <= hi:
        raise ModbusError(EXC_ILLEGAL_VALUE, "quantity %d is not in 1-%d" % (qty, hi))


def _write(mem, unit, who, table, addr, vals, reply, master):
    mem.write(unit, table, addr, vals, master)
    log("%s write %s %d x%d = %s%s" % (who, table, addr, len(vals), fmt_values(table, vals),
                                       mem.describe(unit, table, addr, vals)))
    mem.save()
    return reply


def _write_data(pdu, offset, table, qty):
    """Byte count + packed data of a multiple write starting at pdu[offset]; returns the values."""
    bit = table in BIT_TABLES
    _check_qty(qty, MAX_WRITE_BITS if bit else MAX_WRITE_REGS)
    bc = pdu[offset]
    data = pdu[offset + 1:]
    need = (qty + 7) // 8 if bit else 2 * qty
    if bc != need or len(data) < need:
        raise ModbusError(EXC_ILLEGAL_VALUE, "byte count %d with %d data bytes, expected %d" % (bc, len(data), need))
    return unpack_bits(data, qty) if bit else list(struct.unpack_from(">%dH" % qty, data))


def handle_pdu(pdu, mem, unit, who, control=False):
    """Returns the response PDU. Raises ModbusError for exception responses."""
    fc = pdu[0]
    who = "%s fc%02d" % (who, fc)
    if fc in (1, 2, 3, 4):
        table = {1: "coil", 2: "di", 3: "hr", 4: "ir"}[fc]
        addr, qty = struct.unpack_from(">HH", pdu, 1)
        bit = table in BIT_TABLES
        _check_qty(qty, MAX_READ_BITS if bit else MAX_READ_REGS)
        vals = mem.read(unit, table, addr, qty)
        log("%s read %s %d x%d = %s%s" % (who, table, addr, qty, fmt_values(table, vals),
                                          mem.describe(unit, table, addr, vals)))
        data = pack_bits(vals) if bit else struct.pack(">%dH" % qty, *vals)
        return bytes([fc, len(data)]) + data
    if fc == 5:
        addr, raw = struct.unpack_from(">HH", pdu, 1)
        if raw not in (0xFF00, 0x0000):
            raise ModbusError(EXC_ILLEGAL_VALUE, "coil value 0x%04X (must be FF00 or 0000)" % raw)
        return _write(mem, unit, who, "coil", addr, [1 if raw else 0], pdu[:5], not control)
    if fc == 6:
        addr, v = struct.unpack_from(">HH", pdu, 1)
        return _write(mem, unit, who, "hr", addr, [v], pdu[:5], not control)
    if fc in (15, 16):
        table = "coil" if fc == 15 else "hr"
        addr, qty = struct.unpack_from(">HH", pdu, 1)
        return _write(mem, unit, who, table, addr, _write_data(pdu, 5, table, qty), pdu[:5], not control)
    if fc == FC_BACKDOOR and control:
        code, addr, qty = struct.unpack_from(">BHH", pdu, 1)
        if code not in BY_TABLE_CODE:
            raise ModbusError(EXC_ILLEGAL_VALUE, "back door table code %d" % code)
        table = BY_TABLE_CODE[code]
        return _write(mem, unit, who, table, addr, _write_data(pdu, 6, table, qty), pdu[:6], False)
    if fc == FC_RESET_COUNTS and control:
        mem.writes = {}
        mem.save()
        log("%s write counts reset" % who)
        return pdu[:1]
    raise ModbusError(EXC_ILLEGAL_FUNCTION, "unsupported function code %d (0x%02X)" % (fc, fc))


def pack_bits(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def unpack_bits(data, n):
    return [(data[i // 8] >> (i % 8)) & 1 for i in range(n)]


# ---------------------------------------------------------------- framing: Modbus TCP (MBAP), RTU, ASCII

def crc16(data):
    """Modbus RTU CRC. A frame with its CRC appended (low byte first) gives 0."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def lrc(data):
    return (-sum(data)) & 0xFF


def rtu_request_len(buf):
    """Length of the RTU request at the start of buf: None = need more bytes, -1 = unknown function code."""
    if len(buf) < 2:
        return None
    fc = buf[1]
    if fc in (1, 2, 3, 4, 5, 6):
        return 8
    if fc in (15, 16):
        return 9 + buf[6] if len(buf) >= 7 else None
    if fc == FC_BACKDOOR:
        return 10 + buf[7] if len(buf) >= 8 else None
    if fc == FC_RESET_COUNTS:
        return 4
    return -1


class TcpFramer:
    """MBAP header (transaction id, protocol 0, length, unit) + PDU."""
    name = "TCP"

    def __init__(self):
        self.buf, self.fatal, self.who = b"", False, ""

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 7:
            tid, pid, length, uid = struct.unpack_from(">HHHB", self.buf, 0)
            if pid != 0 or not 2 <= length <= 254:
                log("%s not a Modbus TCP frame (protocol id %d, length %d): %s"
                    % (self.who, pid, length, self.buf[:64].hex(" ")))
                self.buf, self.fatal = b"", True
                break
            total = 6 + length
            if len(self.buf) < total:
                break
            raw, self.buf = self.buf[:total], self.buf[total:]
            out.append((tid, uid, raw[7:], raw))
        return out

    def pending(self):
        return False

    def flush(self):
        return []

    def wrap(self, tid, unit, pdu):
        return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


class RtuFramer:
    """unit + PDU + CRC16. Frames are cut by the function code's length; a silent gap flushes anything left."""
    name = "RTU"

    def __init__(self):
        self.buf, self.fatal, self.who = b"", False, ""

    def feed(self, data):
        self.buf += data
        out = []
        while self.buf:
            n = rtu_request_len(self.buf)
            if n is None or n == -1 or len(self.buf) < n:
                break  # wait for more bytes, or for the gap
            frame = self.buf[:n]
            if crc16(frame):
                log("%s bad CRC, discarding %d bytes: %s" % (self.who, len(self.buf), self.buf[:64].hex(" ")))
                self.buf = b""
                break
            self.buf = self.buf[n:]
            out.append((None, frame[0], frame[1:-2], frame))
        return out

    def pending(self):
        return bool(self.buf)

    def flush(self):
        frame, self.buf = self.buf, b""
        if len(frame) >= 4 and crc16(frame) == 0:  # unknown function code: answered with exception 01
            return [(None, frame[0], frame[1:-2], frame)]
        if frame:
            log("%s incomplete or garbled RTU frame, discarding: %s" % (self.who, frame[:64].hex(" ")))
        return []

    def wrap(self, _ctx, unit, pdu):
        body = bytes([unit]) + pdu
        return body + struct.pack("<H", crc16(body))


class AsciiFramer:
    """':' + hex(unit + PDU + LRC) + CR LF."""
    name = "ASCII"

    def __init__(self):
        self.buf, self.fatal, self.who = b"", False, ""

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            start = self.buf.find(b":")
            if start < 0:
                if self.buf.strip():
                    log("%s junk outside an ASCII frame: %r" % (self.who, self.buf[:80]))
                self.buf = b""
                break
            if start:
                log("%s junk before ':': %r" % (self.who, self.buf[:start][:80]))
                self.buf = self.buf[start:]
            end = self.buf.find(b"\n")
            restart = self.buf.find(b":", 1, end if end >= 0 else len(self.buf))
            if restart > 0:  # a new ':' before the line ended restarts the frame
                log("%s unterminated ASCII frame dropped: %r" % (self.who, self.buf[:restart][:80]))
                self.buf = self.buf[restart:]
                continue
            if end < 0:
                if len(self.buf) > 600:
                    log("%s ASCII frame too long, discarding" % self.who)
                    self.buf = b""
                break
            line, self.buf = self.buf[:end + 1], self.buf[end + 1:]
            try:
                raw = bytes.fromhex(line[1:].rstrip(b"\r\n").decode("ascii"))
            except ValueError:
                log("%s bad hex in ASCII frame: %r" % (self.who, line[:80]))
                continue
            if len(raw) < 3 or lrc(raw[:-1]) != raw[-1]:
                log("%s bad LRC in ASCII frame: %r" % (self.who, line[:80]))
                continue
            out.append((None, raw[0], raw[1:-1], line))
        return out

    def pending(self):
        return False

    def flush(self):
        return []

    def wrap(self, _ctx, unit, pdu):
        body = bytes([unit]) + pdu
        return b":" + (body + bytes([lrc(body)])).hex().upper().encode() + b"\r\n"


FRAMERS = {"tcp": TcpFramer, "rtu": RtuFramer, "ascii": AsciiFramer}


def detect_framing(buf):
    """Which framing a TCP connection or UDP datagram speaks, from its first bytes; None = need more."""
    if buf[:1] == b":":
        return "ascii"
    n = rtu_request_len(buf)
    if n is not None and n > 0 and len(buf) >= n and crc16(buf[:n]) == 0:
        return "rtu"
    if n == -1 and len(buf) >= 4 and crc16(buf) == 0:
        return "rtu"
    if len(buf) >= 7 and buf[2:4] == b"\0\0":
        return "tcp"
    if n is None or (n > 0 and len(buf) < n) or len(buf) < 8:
        return None
    return "unknown"


# ---------------------------------------------------------------- byte streams: pty, TCP connections, UDP datagrams

class Stream:
    """One byte stream with its framer. framing=None detects it from the first bytes.
    busy=True (a pty): while a delayed reply is pending, incoming bytes are dropped, as a busy serial device would."""

    def __init__(self, srv, who, send, framing=None, gap=0.05, busy=False, control=False):
        self.srv, self.who, self.send, self.gap, self.busy, self.control = srv, who, send, gap, busy, control
        self.framer, self.buf, self.timer = None, b"", None
        self.waiting, self.ignored = False, 0
        if framing:
            self._use(framing)

    def _use(self, framing):
        self.framer = FRAMERS[framing]()
        self.framer.who = "%s %s" % (self.who, self.framer.name)

    def data(self, chunk):
        """Feed received bytes. Returns True when the stream should be closed (or, for a pty, restarted)."""
        if self.waiting:
            self.ignored += len(chunk)
            return False
        if self.framer is None:
            self.buf += chunk
            kind = detect_framing(self.buf)
            if kind is None:
                if len(self.buf) > 600:
                    log("%s no Modbus framing recognised in %d bytes" % (self.who, len(self.buf)))
                    return True
                return False
            if kind == "unknown":
                log("%s not Modbus TCP, RTU or ASCII: %s" % (self.who, self.buf[:64].hex(" ")))
                return True
            self._use(kind)
            if not self.busy and not self.control:
                log("%s speaks Modbus %s" % (self.who, self.framer.name))
            chunk, self.buf = self.buf, b""
        self._answer(self.framer.feed(chunk))
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.framer.pending():
            self.timer = asyncio.get_running_loop().call_later(self.gap, self.on_gap)
        return self.framer.fatal

    def on_gap(self):
        self.timer = None
        if self.framer:
            self._answer(self.framer.flush())

    def close(self):
        if self.timer:
            self.timer.cancel()

    def _answer(self, requests):
        loop = asyncio.get_running_loop()
        for i, (ctx, unit, pdu, raw) in enumerate(requests):
            resp, ms = self.srv.serve(self.framer, ctx, unit, pdu, raw, self.who, self.control)
            if resp is None:
                continue
            if not ms:
                self.send(resp)
                continue
            tag = "%s %s unit %d fc%02d" % (self.who, self.framer.name, unit, pdu[0])
            if self.busy:  # like a busy serial device: what arrives before the reply goes out is lost
                self.waiting = True
                self.ignored = sum(len(r[3]) for r in requests[i + 1:]) + len(self.framer.buf)
                self.framer.buf = b""
                loop.call_later(ms / 1000, self._send_late, resp, tag, ms)
                return
            loop.call_later(ms / 1000, self.send, resp)
            log("%s reply delayed %d ms" % (tag, ms))

    def _send_late(self, resp, tag, ms):
        self.waiting = False
        log("%s reply delayed %d ms%s" % (tag, ms, ", ignored %d bytes sent meanwhile" % self.ignored
                                          if self.ignored else ""))
        self.ignored = 0
        self.send(resp)


class Server:
    def __init__(self, mem, delay, devices, unknown_units="answer"):
        self.mem, self.delay, self.devices, self.unknown_units = mem, delay, devices, unknown_units
        self.noted = set()

    def serve(self, framer, ctx, unit, pdu, raw, peer, control=False):
        """Answer one request. Returns (framed response or None for no reply, delay in ms)."""
        broadcast = framer.name != "TCP" and unit == 0
        wire_unit = unit
        if framer.name == "TCP" and unit in (0, 255) and len(self.devices) == 1:
            unit = next(iter(self.devices))  # Modbus TCP "this device"
        who = "%s %s unit %d" % (peer, framer.name, wire_unit)
        try:
            if not broadcast and unit not in self.devices and not control:
                if self.unknown_units == "silent":
                    if framer.name == "TCP":
                        raise ModbusError(EXC_GATEWAY_NO_RESPONSE, "no device with unit %d here" % unit)
                    log("%s ignored: no device with that unit id here" % who)
                    return None, 0
                if unit not in self.noted:
                    self.noted.add(unit)
                    log("%s: no device with unit %d is configured here; it answers anyway, unset addresses read 0"
                        % (who, unit))
            if broadcast:
                for u in sorted(self.devices) or [0]:  # a broadcast write reaches every station
                    resp = handle_pdu(pdu, self.mem, u, who, control)
                log("%s broadcast, no reply" % who)
                return None, 0
            resp = handle_pdu(pdu, self.mem, unit, who, control)
        except ModbusError as ex:
            log("%s fc%02d exception %02X (%s): %s; request %s"
                % (who, pdu[0], ex.code, EXCEPTION_NAMES.get(ex.code, "?"), ex,
                   raw.hex(" ") if framer.name != "ASCII" else raw.strip()))
            resp = bytes([(pdu[0] | 0x80) & 0xFF, ex.code])
        except (struct.error, IndexError) as ex:  # body too short for its function code
            log("%s malformed request (%s): %s" % (who, ex, raw.hex(" ")))
            resp = bytes([(pdu[0] | 0x80) & 0xFF, EXC_ILLEGAL_VALUE])
        if broadcast:
            return None, 0
        return framer.wrap(ctx, wire_unit, resp), 0 if control else self.delay.ms(unit, pdu[0])

    async def tcp_client(self, reader, writer, control=False, framing=None):
        peer = ("ctl" if control else "tcp") + " %s:%d" % writer.get_extra_info("peername")[:2]
        if not control:
            log("%s connect" % peer)
        st = Stream(self, peer, lambda b: writer.is_closing() or writer.write(b), framing=framing, control=control)
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk or st.data(chunk):
                    break
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if not control:
                log("%s disconnect" % peer)
            st.close()
            writer.close()


class Udp(asyncio.DatagramProtocol):
    def __init__(self, srv, framing):
        self.srv, self.framing = srv, framing

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        st = Stream(self.srv, "udp %s:%d" % addr[:2], lambda b: self.transport.sendto(b, addr), self.framing)
        st.data(data)
        st.on_gap()  # a datagram is a whole frame
        st.close()


def open_pty(link):
    """A pty in raw mode, with a symlink at link pointing at its slave end, as the master (ptcsw) opens it."""
    import termios
    import tty
    master, slave = os.openpty()
    tty.setraw(slave, termios.TCSANOW)  # so nothing is echoed back before the master sets up its end
    os.set_blocking(master, False)
    path = os.ttyname(slave)
    tmp = link + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(path, tmp)
    os.replace(tmp, link)  # a restart swaps the link in one step; ptcsw reopens the path and finds the new pty
    # Keep the slave end open too, so reads on the master don't fail with EIO while ptcsw has the port closed.
    return master, slave, path


def serve_pty(srv, link, framing):
    master, _slave, path = open_pty(link)
    loop = asyncio.get_running_loop()
    holder = {}

    def send(b):
        try:
            os.write(master, b)
        except BlockingIOError:
            log("pty output buffer full (nobody reading?), reply dropped")

    def new_stream():
        holder["s"] = Stream(srv, "pty", send, framing, gap=0.1, busy=True)

    def on_read():
        try:
            chunk = os.read(master, 4096)
        except BlockingIOError:
            return
        except OSError as ex:
            log("pty read error: %s" % ex)
            return
        if holder["s"].data(chunk):
            holder["s"].close()
            new_stream()

    new_stream()
    loop.add_reader(master, on_read)
    return path


async def ticker(delay, schedules):
    while True:
        delay.tick()
        schedules.tick()
        await asyncio.sleep(0.1)


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", required=True, help="simulator file: endpoint, devices, tags and starting values")
    ap.add_argument("--state", help="JSON file the values and write counts are kept in (loaded on start if present)")
    ap.add_argument("--log", help="log file (rotates to <log>.1 at %d MB); default stdout" % (LOG_MAX_BYTES >> 20))
    ap.add_argument("--refuse", help="ranges whose writes get an exception, e.g. hr:9000-9099,coil:9000-9099")
    ap.add_argument("--refuse-code", type=int, help="exception code for refused writes (default 2)")
    ap.add_argument("--delay-file", help="JSON reply delays, re-read on change (see above)")
    ap.add_argument("--schedule-file", help="JSON list of schedules, re-read on change")
    ap.add_argument("--no-udp", action="store_true", help="TCP endpoints: don't also listen on UDP")
    a = ap.parse_args()
    if a.log:
        log.open(a.log)

    cfg = load_config(a.init)
    ep, framing = cfg["endpoint"], cfg.get("framing")
    refuse = a.refuse if a.refuse is not None else cfg.get("refuse", DEFAULT_REFUSE)
    code = a.refuse_code if a.refuse_code is not None else int(cfg.get("refuse_code", EXC_ILLEGAL_ADDRESS))
    mem = Memory(cfg["devices"], a.state, parse_refuse(refuse), code)
    mem.seed()
    kept = a.state and mem.load_state()
    mem.save()
    delay = Delay(a.delay_file)
    srv = Server(mem, delay, cfg["devices"], cfg.get("unknown_units", "answer"))
    schedules = Schedules(mem, cfg, a.schedule_file)

    log("Modbus simulator %s: units %s, %d tags, %s; refuse=%s code %02d"
        % (os.path.basename(a.init), ", ".join(map(str, sorted(cfg["devices"]))) or "none", len(cfg["tags"]),
           "values kept from the last run" if kept else "starting values", refuse or "none", code))
    for t in cfg["tags"]:
        if t["pending"]:
            log("unit %d tag %r has no Modbus address yet (PLC %s); not served until \"addr\" is set"
                % (t["unit"], t["tag"], t.get("plc_addr", "?")))
    servers = []
    if ep["kind"] == "pty":
        path = serve_pty(srv, ep["path"], framing)
        log("listening on %s (pty %s, %s): the serial port path for application.json"
            % (ep["path"], path, (framing or "rtu/ascii").upper()))
    else:
        servers.append(await asyncio.start_server(
            lambda r, w: srv.tcp_client(r, w, framing=framing), ep["bind"], ep["port"]))
        if not a.no_udp:
            await asyncio.get_running_loop().create_datagram_endpoint(lambda: Udp(srv, framing),
                                                                      local_addr=(ep["bind"], ep["port"]))
        log("listening on %s:%d (tcp%s), framing %s" % (ep["bind"], ep["port"], "" if a.no_udp else "+udp",
                                                        framing.upper() if framing else "detected per connection"))
    servers.append(await asyncio.start_server(lambda r, w: srv.tcp_client(r, w, control=True),
                                              "127.0.0.1", cfg["control_port"]))
    log("control port 127.0.0.1:%d (modbus_client.py and the web UI)" % cfg["control_port"])
    await ticker(delay, schedules)


def run():
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if sys.platform == "darwin":  # macOS kqueue can't watch a pty; select() can
        import selectors
        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    else:
        asyncio.run(main())


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
