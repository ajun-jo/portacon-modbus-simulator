#!/usr/bin/env python3
"""Modbus PLC simulator over TCP. Python 3 standard library only.

One TCP port (plus UDP on the same port) per simulated machine. The framing is detected per connection
(UDP: per datagram), so the port serves Modbus TCP, RTU-over-TCP and ASCII-over-TCP masters without a restart.
On RTU/ASCII framing the simulator answers only its own unit id (others are another station: no reply) and executes
unit 0 (broadcast) without replying. Modbus TCP answers any unit id unless --tcp-unit-check.

Serves the four Modbus tables:
  coil  (0x)  read/write bits        FC 01 read, FC 05 write single, FC 15 write multiple
  di    (1x)  read-only bits         FC 02 read
  ir    (3x)  read-only registers    FC 04 read
  hr    (4x)  read/write registers   FC 03 read, FC 06 write single, FC 16 write multiple
Addresses are 0-based protocol addresses: "40001" in a device manual is usually hr 0.
Every address 0-65535 exists and starts at 0. Any other function code gets exception 01 and is logged in hex.

FC 65 (0x41, in the spec's user-defined range) is a test back door that writes any table, so modbus_client.py can
set discrete inputs and input registers, which Modbus itself can't write. A real Modbus master never sends it.

Values are seeded by --init, saved to --state after each write, --refuse makes chosen ranges answer writes with
exception 04, and --delay-file holds {"default": ms}, re-read on change, so the reply delay changes without a restart.
"""
import argparse
import asyncio
import json
import os
import re
import struct
import sys
import time

TABLES = ("coil", "di", "ir", "hr")
BIT_TABLES = ("coil", "di")
TABLE_ALIASES = {
    "coil": "coil", "coils": "coil", "0x": "coil",
    "di": "di", "discrete": "di", "discrete_input": "di", "discrete_inputs": "di", "1x": "di",
    "ir": "ir", "input": "ir", "input_register": "ir", "input_registers": "ir", "3x": "ir",
    "hr": "hr", "holding": "hr", "holding_register": "hr", "holding_registers": "hr", "4x": "hr",
}
READ_FC = {"coil": 1, "di": 2, "hr": 3, "ir": 4}
TABLE_CODES = {"coil": 0, "di": 1, "ir": 3, "hr": 4}  # table byte of the FC 65 back door
BY_TABLE_CODE = {v: k for k, v in TABLE_CODES.items()}
FC_BACKDOOR = 0x41

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


def log(msg):
    t = time.time()
    print(time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + ".%03d " % int(t % 1 * 1000) + msg, flush=True)


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


def load_config(path):
    """Init file: {"port": 5020, "unit_id": 1, "defaults": {...}, "tags": [...]} where each tag is like
    {"tag": "투입수", "table": "hr", "addr": 1104, "type": "INT32", "value": 120345}
    {"tag": "LOT No.", "table": "hr", "addr": 771, "type": "STRING", "words": 5, "value": "LOT0001"}
    {"tag": "단선 1-1", "table": "hr", "addr": 22400, "type": "BIT", "bit": 0, "value": 0}   bit 0 of hr 22400
    {"tag": "현재 LOT No.", "table": "hr", "addr": null, ...}   address pending: listed, but not served
    "defaults" (word_order, byte_order, encoding) apply to every tag that doesn't set its own.
    Returns the config with every tag's table/type/addr normalised, its size in "words", and "pending" set
    on tags without an address."""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if isinstance(cfg, list):
        cfg = {"tags": cfg}
    defaults = cfg.get("defaults", {})
    tags = []
    for e in cfg.get("tags", []):
        if "table" not in e:  # comment-only entry
            continue
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
            raise SystemExit("%s: tag %r: %s" % (path, name, ex))
        t.setdefault("tag", name)
        tags.append(t)
    cfg["tags"] = tags
    return cfg


def pack_bits(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def unpack_bits(data, n):
    return [(data[i // 8] >> (i % 8)) & 1 for i in range(n)]


# ---------------------------------------------------------------- memory

class Memory:
    def __init__(self, state_path=None, refuse=(), tags=()):
        self.state_path = state_path
        self.tables = {t: {} for t in TABLES}
        self.refuse = list(refuse)  # (table, lo, hi)
        self.tags = list(tags)
        self.dirty = False

    @staticmethod
    def _check_range(table, addr, n):
        if addr < 0 or addr + n > 0x10000:
            raise ModbusError(EXC_ILLEGAL_ADDRESS, "%s %d x%d runs past address 65535" % (table, addr, n))

    def read(self, table, addr, n):
        self._check_range(table, addr, n)
        m = self.tables[table]
        return [m.get(addr + i, 0) for i in range(n)]

    def write(self, table, addr, values, enforce_refuse=True):
        self._check_range(table, addr, len(values))
        if enforce_refuse:
            for rt, lo, hi in self.refuse:
                if rt == table and addr <= hi and addr + len(values) - 1 >= lo:
                    raise ModbusError(EXC_DEVICE_FAILURE, "write to refused range %s %d-%d" % (rt, lo, hi))
        mask = 1 if table in BIT_TABLES else 0xFFFF
        m = self.tables[table]
        for i, v in enumerate(values):
            m[addr + i] = v & mask
        self.dirty = True

    def load_tags(self):
        for t in self.tags:
            if "value" not in t or t["pending"]:
                continue
            words = encode_value(t["type"], t["value"], t)
            if t["type"] == "BIT":  # several BIT tags share one register
                words = [set_bit(self.read(t["table"], t["addr"], 1)[0], t["bit"], words[0])]
            self.write(t["table"], t["addr"], words, enforce_refuse=False)
        self.dirty = True

    def describe(self, table, addr, values):
        """'  {투입수=120345, 양품수=120308}' for the tags lying entirely inside this read or write."""
        parts = []
        for t in self.tags:
            if t["pending"] or t["table"] != table:
                continue
            off = t["addr"] - addr
            if 0 <= off and off + t["words"] <= len(values):
                try:
                    parts.append("%s=%r" % (t["tag"], decode_value(t["type"], values[off:off + t["words"]], t)))
                except Exception:
                    pass
        return "  {%s}" % ", ".join(parts) if parts else ""

    def save(self):
        if not self.state_path or not self.dirty:
            return
        data = {t: {str(a): v for a, v in sorted(m.items())} for t, m in self.tables.items()}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.state_path)
        self.dirty = False


def parse_refuse(spec):
    out = []
    for part in filter(None, (spec or "").split(",")):
        m = re.fullmatch(r"([A-Za-z0-9_]+):(\d+)-(\d+)", part.strip())
        try:
            out.append((table_name(m.group(1)), int(m.group(2)), int(m.group(3))))
        except (AttributeError, ValueError):
            raise SystemExit("bad --refuse part %r (expect e.g. hr:9000-9099)" % part)
    return out


class Delay:
    def __init__(self, path):
        self.path, self.mtime, self.ms = path, None, 0

    def get(self):
        if not self.path:
            return 0
        try:
            mt = os.stat(self.path).st_mtime
        except FileNotFoundError:
            if self.ms:
                log("delay file gone, delay 0 ms")
            self.mtime, self.ms = None, 0
            return 0
        if mt != self.mtime:
            self.mtime = mt
            try:
                with open(self.path) as f:
                    self.ms = int(json.load(f).get("default", 0))
                log("delay set to %d ms" % self.ms)
            except Exception as ex:  # keep the old value on a half-written file
                log("delay file unreadable (%s), keeping %d ms" % (ex, self.ms))
        return self.ms


# ---------------------------------------------------------------- PDU handling

def fmt_values(table, vals):
    if table in BIT_TABLES:
        return "".join(map(str, vals))
    hexs = " ".join("%04X" % w for w in vals)
    return "[%s] (%d)" % (hexs, vals[0]) if len(vals) == 1 else "[%s]" % hexs


def _check_qty(qty, hi):
    if not 1 <= qty <= hi:
        raise ModbusError(EXC_ILLEGAL_VALUE, "quantity %d is not in 1-%d" % (qty, hi))


def _write(mem, who, table, addr, vals, reply, enforce_refuse=True):
    mem.write(table, addr, vals, enforce_refuse)
    log("%s write %s %d x%d = %s%s" % (who, table, addr, len(vals), fmt_values(table, vals),
                                       mem.describe(table, addr, vals)))
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


def handle_pdu(pdu, mem, who):
    """Returns the response PDU. Raises ModbusError for exception responses."""
    fc = pdu[0]
    if fc in (1, 2, 3, 4):
        table = {1: "coil", 2: "di", 3: "hr", 4: "ir"}[fc]
        addr, qty = struct.unpack_from(">HH", pdu, 1)
        bit = table in BIT_TABLES
        _check_qty(qty, MAX_READ_BITS if bit else MAX_READ_REGS)
        vals = mem.read(table, addr, qty)
        log("%s read %s %d x%d = %s%s" % (who, table, addr, qty, fmt_values(table, vals),
                                          mem.describe(table, addr, vals)))
        data = pack_bits(vals) if bit else struct.pack(">%dH" % qty, *vals)
        return bytes([fc, len(data)]) + data
    if fc == 5:
        addr, raw = struct.unpack_from(">HH", pdu, 1)
        if raw not in (0xFF00, 0x0000):
            raise ModbusError(EXC_ILLEGAL_VALUE, "coil value 0x%04X (must be FF00 or 0000)" % raw)
        return _write(mem, who, "coil", addr, [1 if raw else 0], pdu[:5])
    if fc == 6:
        addr, v = struct.unpack_from(">HH", pdu, 1)
        return _write(mem, who, "hr", addr, [v], pdu[:5])
    if fc in (15, 16):
        table = "coil" if fc == 15 else "hr"
        addr, qty = struct.unpack_from(">HH", pdu, 1)
        return _write(mem, who, table, addr, _write_data(pdu, 5, table, qty), pdu[:5])
    if fc == FC_BACKDOOR:
        code, addr, qty = struct.unpack_from(">BHH", pdu, 1)
        if code not in BY_TABLE_CODE:
            raise ModbusError(EXC_ILLEGAL_VALUE, "back door table code %d" % code)
        table = BY_TABLE_CODE[code]
        return _write(mem, who + " (FC65)", table, addr, _write_data(pdu, 6, table, qty), pdu[:6],
                      enforce_refuse=False)
    raise ModbusError(EXC_ILLEGAL_FUNCTION, "unsupported function code %d (0x%02X)" % (fc, fc))


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


# ---------------------------------------------------------------- byte streams: TCP connections and UDP datagrams

class Stream:
    """One byte stream with its framer. framing=None detects it from the first bytes."""

    def __init__(self, srv, who, send, framing=None, gap=0.05):
        self.srv, self.who, self.send, self.gap = srv, who, send, gap
        self.framer, self.buf, self.timer = None, b"", None
        if framing:
            self._use(framing)

    def _use(self, framing):
        self.framer = FRAMERS[framing]()
        self.framer.who = "%s %s" % (self.who, self.framer.name)

    def data(self, chunk):
        """Feed received bytes. Returns True when the stream should be closed."""
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
        for ctx, unit, pdu, raw in requests:
            resp = self.srv.serve(self.framer, ctx, unit, pdu, raw, self.who)
            if resp is None:
                continue
            d = self.srv.delay.get()
            if d:
                asyncio.get_running_loop().call_later(d / 1000, self.send, resp)
            else:
                self.send(resp)


class Server:
    def __init__(self, mem, delay, unit, tcp_unit_check):
        self.mem, self.delay, self.unit, self.tcp_unit_check = mem, delay, unit, tcp_unit_check

    def serve(self, framer, ctx, unit, pdu, raw, peer):
        """Answer one request. Returns the framed response, or None when nothing should be sent back."""
        who = "%s %s unit %d" % (peer, framer.name, unit)
        broadcast = False
        try:
            if framer.name == "TCP":
                if self.tcp_unit_check and unit != self.unit:
                    raise ModbusError(EXC_GATEWAY_NO_RESPONSE, "unit %d is not this simulator's unit %d"
                                      % (unit, self.unit))
            elif unit == 0:
                broadcast = True
            elif unit != self.unit:  # RTU/ASCII-style: another station's address, stay silent
                log("%s ignored: this simulator is unit %d" % (who, self.unit))
                return None
            resp = handle_pdu(pdu, self.mem, who)
        except ModbusError as ex:
            log("%s exception %02X (%s): %s; request %s"
                % (who, ex.code, EXCEPTION_NAMES.get(ex.code, "?"), ex, raw.hex(" ") if framer.name != "ASCII"
                   else raw.strip()))
            resp = bytes([(pdu[0] | 0x80) & 0xFF, ex.code])
        except (struct.error, IndexError) as ex:  # body too short for its function code
            log("%s malformed request (%s): %s" % (who, ex, raw.hex(" ")))
            resp = bytes([(pdu[0] | 0x80) & 0xFF, EXC_ILLEGAL_VALUE])
        if broadcast:
            log("%s broadcast, no reply" % who)
            return None
        return framer.wrap(ctx, unit, resp)

    async def tcp_client(self, reader, writer):
        peer = "tcp %s:%d" % writer.get_extra_info("peername")[:2]
        log("%s connect" % peer)
        st = Stream(self, peer, lambda b: writer.is_closing() or writer.write(b))
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk or st.data(chunk):
                    break
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            log("%s disconnect" % peer)
            st.close()
            writer.close()


class Udp(asyncio.DatagramProtocol):
    def __init__(self, srv):
        self.srv = srv

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        st = Stream(self.srv, "udp %s:%d" % addr[:2], lambda b: self.transport.sendto(b, addr))
        st.data(data)
        st.on_gap()  # a datagram is a whole frame
        st.close()


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", help="JSON file of settings, tags and starting values (see load_config)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, help="TCP/UDP port; default: the init file's port, else %d" % DEFAULT_PORT)
    ap.add_argument("--unit", type=int, help="this simulator's unit id; default: the init file's unit_id, else 1")
    ap.add_argument("--state", help="JSON file the current memory is saved to after each write")
    ap.add_argument("--refuse", help="ranges whose writes get exception 04, e.g. hr:9000-9099,coil:9000-9099")
    ap.add_argument("--delay-file", help='JSON {"default": ms} reply delay, re-read on change')
    ap.add_argument("--tcp-unit-check", action="store_true",
                    help="Modbus TCP requests for another unit id get exception 0B (default: any unit id answers)")
    ap.add_argument("--no-udp", action="store_true")
    a = ap.parse_args()

    cfg = load_config(a.init) if a.init else {"tags": []}

    def setting(arg, key, default):
        return arg if arg is not None else cfg.get(key) if cfg.get(key) is not None else default

    port = setting(a.port, "port", DEFAULT_PORT)
    unit = setting(a.unit, "unit_id", 1)
    mem = Memory(a.state, parse_refuse(a.refuse), cfg["tags"])
    mem.load_tags()
    mem.save()
    srv = Server(mem, Delay(a.delay_file), unit, a.tcp_unit_check)
    tcp = await asyncio.start_server(srv.tcp_client, a.bind, port)
    if not a.no_udp:
        await asyncio.get_running_loop().create_datagram_endpoint(lambda: Udp(srv), local_addr=(a.bind, port))
    log("Modbus PLC simulator, unit %d, %d tags from %s, refuse=%s" % (unit, len(cfg["tags"]), a.init, a.refuse))
    for t in cfg["tags"]:
        if t["pending"]:
            log("tag %r has no Modbus address yet (PLC %s); not served until \"addr\" is set"
                % (t["tag"], t.get("plc_addr", "?")))
    log("listening on %s:%d (tcp%s), framing detected per connection: Modbus TCP, RTU or ASCII"
        % (a.bind, port, "" if a.no_udp else "+udp"))
    async with tcp:
        await tcp.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
