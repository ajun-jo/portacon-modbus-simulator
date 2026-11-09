#!/usr/bin/env python3
"""Client for the simulator: read and change values by tag name or raw address while your server polls.
It talks Modbus TCP to the simulator's port, which also accepts RTU and ASCII framing, so it works whichever
framing your server uses.

By tag name (host, port, unit and word/byte order come from the init file; "sp1" means machines/sp1.init.json):
  modbus_client.py show sp1                                   every tag with its current value
  modbus_client.py get sp1 투입수 "LOT No."
  modbus_client.py set sp1 투입수=5000 가동중=0 "LOT No.=LOT0002"

By raw address (tables: coil, di, ir, hr; addresses are 0-based, so manual "40001" is hr 0):
  modbus_client.py read hr 1104 2 --sim sp1                   raw registers in hex
  modbus_client.py read hr 1104 --type INT32 --sim sp1
  modbus_client.py write hr 1104 --type INT32 123456 --port 5020
  modbus_client.py write hr 20 0x1234 0x5678 --port 5020      raw words
  modbus_client.py write coil 0 1011 --port 5020              bits, lowest address first
  modbus_client.py write ir 10 --type FLOAT32 3.5 --port 5020 di/ir are set through the simulator's FC 65
  modbus_client.py read hr 5533 --bit 6 --sim wind1           one bit of a register (bit 0 = lowest)
  modbus_client.py write hr 5533 1 --bit 6 --sim wind1        sets only that bit (read-modify-write)

Types: BOOL BIT INT16 UINT16 INT32 UINT32 FLOAT32 STRING WORDS. BIT is one bit inside a register ("bit": 0-15).
An exception reply prints e.g. "exception 02 (illegal data address)" and exits with status 1.
A second client is harmless: Modbus TCP is request/response per connection, so your server's polling is unaffected.
"""
import argparse
import os
import socket
import struct
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modbus_plc_sim import (  # noqa: E402
    BIT_TABLES, DEFAULT_PORT, EXCEPTION_NAMES, FC_BACKDOOR, READ_FC, TABLE_CODES,
    decode_value, encode_value, load_config, pack_bits, set_bit, table_name, unpack_bits, word_count,
)

HERE = os.path.dirname(os.path.abspath(__file__))


class ModbusException(Exception):
    def __init__(self, code):
        super().__init__("exception %02X (%s)" % (code, EXCEPTION_NAMES.get(code, "unknown")))
        self.code = code


class Client:
    def __init__(self, host, port, unit, timeout):
        self.where = "%s:%d" % (host, port)
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as ex:
            raise SystemExit("can't connect to %s (%s). Is the simulator running? ./run.sh <name>" % (self.where, ex))
        self.unit, self.tid = unit, 0

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise SystemExit("%s closed the connection" % self.where)
            buf += chunk
        return buf

    def call(self, pdu):
        self.tid = (self.tid + 1) & 0xFFFF
        self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit) + pdu)
        try:
            tid, _, length, _ = struct.unpack(">HHHB", self._recv(7))
            body = self._recv(length - 1)
        except socket.timeout:
            raise SystemExit("no reply from %s (timeout)" % self.where)
        if tid != self.tid:
            raise SystemExit("reply transaction id %d, expected %d" % (tid, self.tid))
        if body[0] & 0x80:
            raise ModbusException(body[1])
        return body

    def read(self, table, addr, n):
        body = self.call(struct.pack(">BHH", READ_FC[table], addr, n))
        data = body[2:]
        return unpack_bits(data, n) if table in BIT_TABLES else list(struct.unpack(">%dH" % n, data[:2 * n]))

    def write(self, table, addr, vals):
        """Uses FC 05/15 for coils, FC 06/16 for holding registers and the FC 65 back door for di/ir."""
        bit = table in BIT_TABLES
        data = pack_bits(vals) if bit else struct.pack(">%dH" % len(vals), *vals)
        if table == "coil" and len(vals) == 1:
            self.call(struct.pack(">BHH", 5, addr, 0xFF00 if vals[0] else 0))
        elif table == "hr" and len(vals) == 1:
            self.call(struct.pack(">BHH", 6, addr, vals[0]))
        elif table in ("coil", "hr"):
            self.call(struct.pack(">BHHB", 15 if bit else 16, addr, len(vals), len(data)) + data)
        else:
            self.call(struct.pack(">BBHHB", FC_BACKDOOR, TABLE_CODES[table], addr, len(vals), len(data)) + data)


# ---------------------------------------------------------------- helpers

def sim_path(name):
    if os.path.exists(name):
        return name
    for p in (os.path.join(HERE, "machines", name + ".init.json"), os.path.join(HERE, name + ".init.json")):
        if os.path.exists(p):
            return p
    names = sorted(f[:-10] for f in os.listdir(os.path.join(HERE, "machines")) if f.endswith(".init.json"))
    raise SystemExit("no machine %r. Machines: %s" % (name, ", ".join(names)))


def connect(a, cfg):
    return Client(a.host or "127.0.0.1", a.port or cfg.get("port") or DEFAULT_PORT,
                  a.unit if a.unit is not None else cfg.get("unit_id", 1), a.timeout)


def find_tag(cfg, name):
    for exact in (True, False):
        for t in cfg["tags"]:
            if (t["tag"] == name) if exact else (t["tag"].replace(" ", "").lower() == name.replace(" ", "").lower()):
                return t
    raise SystemExit("no tag %r. Tags: %s" % (name, ", ".join(t["tag"] for t in cfg["tags"])))


def text_width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def print_table(header, rows):
    widths = [max(text_width(r[i]) for r in [header] + rows) for i in range(len(header))]
    for r in [header] + rows:
        print("  ".join(c + " " * (w - text_width(c)) for c, w in zip(r, widths)).rstrip())


def fmt(v):
    return repr(v) if isinstance(v, str) else str(v)


def fmt_addr(t):
    if t["pending"]:
        return "%s (pending)" % t["table"]
    return "%s %d" % (t["table"], t["addr"]) + (".%d" % t["bit"] if t["type"] == "BIT" else "")


def write_tag(cl, t, words):
    if t["type"] == "BIT":  # only this bit; the register's other bits belong to other tags
        words = [set_bit(cl.read(t["table"], t["addr"], 1)[0], t["bit"], words[0])]
    cl.write(t["table"], t["addr"], words)


def show(cl, tags):
    rows, failed = [], False
    for t in tags:
        if t["pending"]:
            v = "<no Modbus address yet: set \"addr\">"
        else:
            try:
                v = fmt(decode_value(t["type"], cl.read(t["table"], t["addr"], t["words"]), t))
            except ModbusException as ex:
                v, failed = "<%s>" % ex, True
        rows.append((t["tag"], fmt_addr(t), t.get("plc_addr", ""), t["type"], v))
    print_table(("tag", "address", "plc", "type", "value"), rows)
    return 1 if failed else 0


# ---------------------------------------------------------------- commands

def cmd_show(a):
    cfg = load_config(sim_path(a.sim))
    return show(connect(a, cfg), cfg["tags"])


def cmd_get(a):
    cfg = load_config(sim_path(a.sim))
    return show(connect(a, cfg), [find_tag(cfg, n) for n in a.tags])


def cmd_set(a):
    cfg = load_config(sim_path(a.sim))
    todo = []
    for item in a.assignments:  # check every value before writing any
        if "=" not in item:
            raise SystemExit("expected tag=value, got %r" % item)
        name, val = item.split("=", 1)
        t = find_tag(cfg, name.strip())
        if t["pending"]:
            raise SystemExit("%s has no Modbus address yet; set its \"addr\" in the machine file" % t["tag"])
        try:
            todo.append((t, encode_value(t["type"], val, t)))
        except ValueError as ex:
            raise SystemExit("%s: %s" % (t["tag"], ex))
    cl = connect(a, cfg)
    for t, words in todo:
        write_tag(cl, t, words)
    return show(cl, [t for t, _ in todo])


def raw_opts(a):
    cfg = load_config(sim_path(a.sim)) if a.sim else {"tags": []}
    opts = dict(cfg.get("defaults", {}))
    for k in ("word_order", "byte_order", "encoding", "words"):
        if getattr(a, k, None):
            opts[k] = getattr(a, k)
    return cfg, opts


def cmd_read(a):
    cfg, opts = raw_opts(a)
    table, typ = table_name(a.table), a.type.upper()
    if table in BIT_TABLES:
        n = a.count or 1
        print("".join(map(str, connect(a, cfg).read(table, a.addr, n))))
        return 0
    if a.bit is not None:
        print(decode_value("BIT", connect(a, cfg).read(table, a.addr, 1), {"bit": a.bit}))
        return 0
    if typ == "STRING" and not a.count:
        raise SystemExit("a STRING read needs a count of registers, e.g. read hr 771 5 --type STRING")
    n = a.count or (1 if typ == "WORDS" else word_count(typ, opts))
    words = connect(a, cfg).read(table, a.addr, n)
    print(" ".join("%04X" % w for w in words) if typ == "WORDS" else fmt(decode_value(typ, words, opts)))
    return 0


def cmd_write(a):
    cfg, opts = raw_opts(a)
    table, typ = table_name(a.table), a.type.upper()
    try:
        if table in BIT_TABLES:
            joined = "".join(a.values)
            vals = [int(c) for c in joined] if set(joined) <= {"0", "1"} else [encode_value("BOOL", joined)[0]]
        elif a.bit is not None:
            value = encode_value("BIT", "".join(a.values), {"bit": a.bit})[0]
            cl = connect(a, cfg)
            cl.write(table, a.addr, [set_bit(cl.read(table, a.addr, 1)[0], a.bit, value)])
            print("ok")
            return 0
        elif typ == "WORDS":
            vals = encode_value("WORDS", a.values)
        else:
            vals = encode_value(typ, " ".join(a.values), opts)
    except ValueError as ex:
        raise SystemExit(str(ex))
    connect(a, cfg).write(table, a.addr, vals)
    print("ok")
    return 0


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", help="default 127.0.0.1")
    common.add_argument("--port", type=int, help="default: the init file's port, else %d" % DEFAULT_PORT)
    common.add_argument("--unit", type=int, help="unit id; default: the init file's unit_id, else 1")
    common.add_argument("--timeout", type=float, default=3)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show", parents=[common], help="every tag with its current value")
    p.add_argument("sim", help="machine name (sp1, wind1, tp1, ...) or init file path")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("get", parents=[common], help="chosen tags with their current values")
    p.add_argument("sim")
    p.add_argument("tags", nargs="+")
    p.set_defaults(fn=cmd_get)
    p = sub.add_parser("set", parents=[common], help="write tag=value pairs, then read them back")
    p.add_argument("sim")
    p.add_argument("assignments", nargs="+", metavar="tag=value")
    p.set_defaults(fn=cmd_set)

    raw = argparse.ArgumentParser(add_help=False, parents=[common])
    raw.add_argument("--sim", help="take host, port, unit and word/byte order from this init file")
    raw.add_argument("--type", default="WORDS", help="BOOL INT16 UINT16 INT32 UINT32 FLOAT32 STRING WORDS")
    raw.add_argument("--word-order", choices=["big", "little"], help="32-bit types; big = high word first")
    raw.add_argument("--byte-order", choices=["big", "little"], help="STRING; big = first character in high byte")
    raw.add_argument("--encoding", help="STRING encoding, default utf-8")
    raw.add_argument("--bit", type=int, choices=range(16), metavar="0-15", help="one bit inside a register")
    p = sub.add_parser("read", parents=[raw], help="read a raw address")
    p.add_argument("table", help="coil, di, ir or hr")
    p.add_argument("addr", type=int)
    p.add_argument("count", type=int, nargs="?", help="registers or bits; default: the size of --type")
    p.set_defaults(fn=cmd_read)
    p = sub.add_parser("write", parents=[raw], help="write a raw address")
    p.add_argument("table")
    p.add_argument("addr", type=int)
    p.add_argument("values", nargs="+")
    p.add_argument("--words", type=int, help="STRING length in registers (NUL-padded)")
    p.set_defaults(fn=cmd_write)

    a = ap.parse_args()
    try:
        return a.fn(a)
    except ModbusException as ex:
        print(ex, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
