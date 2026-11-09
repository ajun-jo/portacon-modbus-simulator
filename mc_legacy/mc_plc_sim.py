#!/usr/bin/env python3
"""Mitsubishi MC protocol (SLMP) PLC simulator. Python 3 standard library only.

Speaks 3E and 4E frames, binary and ASCII, over TCP and UDP on the same port.
The format is detected per request, so one instance serves use_ascii=true/false and use_udp=true/false.

Supported commands:
  0x0401 batch read   sub 0x0000 (word) / 0x0001 (bit)   Q/L-style device spec (3-byte number, 1-byte code)
                      sub 0x0002 (word) / 0x0003 (bit)   iQ-R-style device spec (4-byte number, 2-byte code)
  0x1401 batch write  same subcommands
  0x0101 read CPU model
  0x1001/0x1002/0x1003/0x1005/0x1006 remote run/stop/pause/latch clear/reset (answered OK, no effect)
Anything else gets end code 0xC059 and is logged in hex, so unknown requests are visible.

Memory: every written word or bit is kept and returned on read. Bit devices (M, X, Y, B, L, SM, ...) can also be
read and written as words: one word = 16 consecutive bits, lowest address in bit 0.
Values persist to --state, are seeded by --init, and --refuse makes chosen ranges reply with an error on write.
--delay-file holds {"default": ms}; it is re-read on change, so reply delay can be changed without a restart.
"""
import argparse
import asyncio
import json
import os
import re
import struct
import sys
import time

# device name -> (binary code, is_bit, ascii number base)
DEVICES = {
    "SM": (0x91, True, 10), "SD": (0xA9, False, 10),
    "X": (0x9C, True, 16), "Y": (0x9D, True, 16),
    "M": (0x90, True, 10), "L": (0x92, True, 10), "F": (0x93, True, 10), "V": (0x94, True, 10),
    "B": (0xA0, True, 16), "D": (0xA8, False, 10), "W": (0xB4, False, 16),
    "TS": (0xC1, True, 10), "TC": (0xC0, True, 10), "TN": (0xC2, False, 10),
    "SS": (0xC7, True, 10), "SC": (0xC6, True, 10), "SN": (0xC8, False, 10),
    "CS": (0xC4, True, 10), "CC": (0xC3, True, 10), "CN": (0xC5, False, 10),
    "SB": (0xA1, True, 16), "SW": (0xB5, False, 16),
    "DX": (0xA2, True, 16), "DY": (0xA3, True, 16),
    "Z": (0xCC, False, 10), "R": (0xAF, False, 10), "ZR": (0xB0, False, 10),
}
BY_CODE = {v[0]: k for k, v in DEVICES.items()}

ERR_BAD_CMD = 0xC059
ERR_BAD_DEVICE = 0xC05C
ERR_WRITE_REFUSED = 0xC051  # shown as "write protected / not allowed" in GX tools
ERR_BAD_LENGTH = 0xC061

CPU_NAME = "Q03UDVCPU"
CPU_CODE = 0x0366


def log(msg):
    t = time.time()
    print(time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + ".%03d " % int(t % 1 * 1000) + msg, flush=True)


class McError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class Memory:
    def __init__(self, state_path, refuse):
        self.state_path = state_path
        self.words = {}  # device -> {addr: uint16}
        self.bits = {}   # device -> {addr: 0/1}
        self.refuse = refuse  # list of (device, lo, hi)
        self.dirty = False

    def _bit_dev(self, dev):
        return DEVICES[dev][1]

    def read_words(self, dev, addr, n):
        if self._bit_dev(dev):
            out = []
            for i in range(n):
                w = 0
                for b in range(16):
                    w |= (self.bits.get(dev, {}).get(addr + i * 16 + b, 0) & 1) << b
                out.append(w)
            return out
        m = self.words.get(dev, {})
        return [m.get(addr + i, 0) for i in range(n)]

    def read_bits(self, dev, addr, n):
        if not self._bit_dev(dev):
            raise McError(ERR_BAD_DEVICE, "bit read of word device %s" % dev)
        m = self.bits.get(dev, {})
        return [m.get(addr + i, 0) for i in range(n)]

    def _check_refuse(self, dev, addr, n):
        for rdev, lo, hi in self.refuse:
            if rdev == dev and addr <= hi and addr + n - 1 >= lo:
                raise McError(ERR_WRITE_REFUSED, "write to refused range %s%d-%d" % (dev, lo, hi))

    def write_words(self, dev, addr, values):
        if self._bit_dev(dev):
            self._check_refuse(dev, addr, len(values) * 16)
            m = self.bits.setdefault(dev, {})
            for i, w in enumerate(values):
                for b in range(16):
                    m[addr + i * 16 + b] = (w >> b) & 1
        else:
            self._check_refuse(dev, addr, len(values))
            m = self.words.setdefault(dev, {})
            for i, w in enumerate(values):
                m[addr + i] = w & 0xFFFF
        self.dirty = True

    def write_bits(self, dev, addr, values):
        if not self._bit_dev(dev):
            raise McError(ERR_BAD_DEVICE, "bit write to word device %s" % dev)
        self._check_refuse(dev, addr, len(values))
        m = self.bits.setdefault(dev, {})
        for i, v in enumerate(values):
            m[addr + i] = 1 if v else 0
        self.dirty = True

    def save(self):
        if not self.state_path or not self.dirty:
            return
        data = {"words": {d: {str(a): v for a, v in sorted(m.items())} for d, m in self.words.items()},
                "bits": {d: {str(a): v for a, v in sorted(m.items())} for d, m in self.bits.items()}}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.state_path)
        self.dirty = False

    def load_init(self, path):
        """Init file: a JSON list of entries such as
        {"dev": "M", "addr": 5, "type": "BOOL", "value": 1}
        {"dev": "D", "addr": 1104, "type": "INT32", "value": 12345}           low word at the lower address
        {"dev": "D", "addr": 1000, "type": "UINT16", "value": 42}
        {"dev": "R", "addr": 771, "type": "STRING", "words": 5, "value": "LOT0001", "byte_order": "LH"}
        Types: BOOL, INT16, UINT16, INT32, UINT32, FLOAT32, STRING, WORDS (value = list of raw uint16)."""
        with open(path) as f:
            entries = json.load(f)
        for e in entries:
            if e.get("comment") and "dev" not in e:
                continue
            dev, addr, typ, val = e["dev"].upper(), int(e["addr"]), e["type"].upper(), e["value"]
            if dev not in DEVICES:
                raise ValueError("unknown device %s in init" % dev)
            if typ == "BOOL":
                self.write_bits(dev, addr, [int(val)])
            else:
                self.write_words(dev, addr, encode_value(typ, val, e))
        self.dirty = True


def encode_value(typ, val, e=None):
    e = e or {}
    if typ in ("INT16", "UINT16"):
        return [int(val) & 0xFFFF]
    if typ in ("INT32", "UINT32"):
        v = int(val) & 0xFFFFFFFF
        return [v & 0xFFFF, v >> 16]
    if typ == "FLOAT32":
        v = struct.unpack("<I", struct.pack("<f", float(val)))[0]
        return [v & 0xFFFF, v >> 16]
    if typ == "WORDS":
        return [int(x) & 0xFFFF for x in val]
    if typ == "STRING":
        words = int(e.get("words", (len(val) + 1) // 2))
        raw = val.encode("latin-1") if isinstance(val, str) else bytes(val)
        raw = raw[: words * 2].ljust(words * 2, b"\0")
        out = []
        for i in range(words):
            a, b = raw[2 * i], raw[2 * i + 1]
            out.append(a | (b << 8) if e.get("byte_order", "LH").upper() == "LH" else (a << 8) | b)
        return out
    raise ValueError("unknown type " + typ)


def parse_refuse(spec):
    out = []
    for part in filter(None, (spec or "").split(",")):
        m = re.fullmatch(r"([A-Za-z]+):(\d+)-(\d+)", part.strip())
        if not m or m.group(1).upper() not in DEVICES:
            raise SystemExit("bad --refuse part %r (expect e.g. D:9000-9099)" % part)
        out.append((m.group(1).upper(), int(m.group(2)), int(m.group(3))))
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


# ---------------------------------------------------------------- frame handling

class Request:
    """Decoded request: header fields to echo back plus the command body."""
    pass


def parse_binary(buf):
    """Return (Request, consumed) or (None, 0) if incomplete. Raises ValueError on garbage."""
    if len(buf) < 2:
        return None, 0
    r = Request()
    r.ascii = False
    if buf[0] == 0x50 and buf[1] == 0x00:
        r.frame, hdr = "3E", 2
        r.serial = None
    elif buf[0] == 0x54 and buf[1] == 0x00:
        r.frame, hdr = "4E", 6
        if len(buf) < 6:
            return None, 0
        r.serial = buf[2:4]
    else:
        raise ValueError("unknown binary subheader %02X %02X" % (buf[0], buf[1]))
    if len(buf) < hdr + 7:
        return None, 0
    r.route = buf[hdr:hdr + 5]  # network, pc, io(2), station
    length = struct.unpack_from("<H", buf, hdr + 5)[0]
    total = hdr + 7 + length
    if len(buf) < total:
        return None, 0
    body = buf[hdr + 7:total]
    if len(body) < 6:
        raise ValueError("binary body too short")
    r.timer, r.cmd, r.sub = struct.unpack_from("<HHH", body, 0)
    r.data = body[6:]
    return r, total


def parse_ascii(buf):
    if len(buf) < 4:
        return None, 0
    r = Request()
    r.ascii = True
    head = buf[:4].decode("ascii", "replace")
    if head == "5000":
        r.frame, hdr = "3E", 4
        r.serial = None
    elif head == "5400":
        r.frame, hdr = "4E", 12
        if len(buf) < 12:
            return None, 0
        r.serial = buf[4:8]
    else:
        raise ValueError("unknown ascii subheader %r" % head)
    if len(buf) < hdr + 14:
        return None, 0
    r.route = buf[hdr:hdr + 10]  # NN PP IIII SS as text
    length = int(buf[hdr + 10:hdr + 14], 16)
    total = hdr + 14 + length
    if len(buf) < total:
        return None, 0
    body = buf[hdr + 14:total].decode("ascii")
    r.timer, r.cmd, r.sub = int(body[0:4], 16), int(body[4:8], 16), int(body[8:12], 16)
    r.data = body[12:]
    return r, total


def build_response(r, end_code, data=b""):
    if r.ascii:
        sub = "D000" if r.frame == "3E" else "D400"
        serial = r.serial.decode() + "0000" if r.frame == "4E" else ""
        if end_code:
            # error info: route of the request + command + subcommand
            data = r.route.decode() + "%04X%04X" % (r.cmd, r.sub)
        payload = ("%04X" % end_code) + (data if isinstance(data, str) else data.decode())
        return (sub + serial + r.route.decode() + "%04X" % len(payload) + payload).encode()
    sub = b"\xD0\x00" if r.frame == "3E" else b"\xD4\x00" + r.serial + b"\x00\x00"
    if end_code:
        data = bytes(r.route) + struct.pack("<HH", r.cmd, r.sub)
    payload = struct.pack("<H", end_code) + data
    return sub + bytes(r.route) + struct.pack("<H", len(payload)) + payload


def decode_device_binary(r):
    """Returns (dev, addr, count, rest)."""
    if r.sub in (0x0002, 0x0003):  # iQ-R: 4-byte number, 2-byte code
        addr = struct.unpack_from("<I", r.data, 0)[0]
        code = struct.unpack_from("<H", r.data, 4)[0]
        count = struct.unpack_from("<H", r.data, 6)[0]
        rest = r.data[8:]
    else:
        addr = r.data[0] | (r.data[1] << 8) | (r.data[2] << 16)
        code = r.data[3]
        count = struct.unpack_from("<H", r.data, 4)[0]
        rest = r.data[6:]
    if code not in BY_CODE:
        raise McError(ERR_BAD_DEVICE, "unknown device code 0x%02X" % code)
    return BY_CODE[code], addr, count, rest


def decode_device_ascii(r):
    if r.sub in (0x0002, 0x0003):  # iQ-R: 4-char code, 8-digit number
        code, num, count, rest = r.data[0:4], r.data[4:12], r.data[12:16], r.data[16:]
    else:
        code, num, count, rest = r.data[0:2], r.data[2:8], r.data[8:12], r.data[12:]
    dev = code.rstrip("*").upper()
    if dev not in DEVICES:
        raise McError(ERR_BAD_DEVICE, "unknown device %r" % code)
    return dev, int(num, DEVICES[dev][2]), int(count, 16), rest


def pack_bits_binary(bits):
    out = bytearray()
    for i in range(0, len(bits), 2):
        hi = bits[i] & 1
        lo = bits[i + 1] & 1 if i + 1 < len(bits) else 0
        out.append((hi << 4) | lo)
    return bytes(out)


def unpack_bits_binary(data, n):
    bits = []
    for i in range(n):
        byte = data[i // 2]
        bits.append((byte >> 4) & 1 if i % 2 == 0 else byte & 1)
    return bits


def fmt_dev(dev, addr):
    return "%s%X" % (dev, addr) if DEVICES[dev][2] == 16 else "%s%d" % (dev, addr)


def words_text(words):
    """Show words as hex plus their LH-ordered printable text, which is what strings look like."""
    hexs = " ".join("%04X" % w for w in words)
    if len(words) > 1:
        txt = "".join(chr(c) if 32 <= c < 127 else "." for w in words for c in (w & 0xFF, w >> 8))
        return "[%s] text(LH)=%r" % (hexs, txt)
    return "[%s] (%d)" % (hexs, words[0]) if words else "[]"


def handle(r, mem):
    """Returns (end_code, payload) where payload is bytes (binary) or str (ascii)."""
    kind = ("ASCII" if r.ascii else "BIN") + "/" + r.frame
    if r.cmd in (0x0401, 0x1401):
        is_bit = r.sub in (0x0001, 0x0003)
        if r.sub not in (0, 1, 2, 3):
            raise McError(ERR_BAD_CMD, "unsupported subcommand 0x%04X" % r.sub)
        dev, addr, count, rest = (decode_device_ascii if r.ascii else decode_device_binary)(r)
        if count == 0 or count > (7168 if is_bit else 960):
            raise McError(ERR_BAD_LENGTH, "bad count %d" % count)
        where = "%s %s x%d" % (fmt_dev(dev, addr), "bit" if is_bit else "word", count)
        if r.cmd == 0x0401:
            if is_bit:
                bits = mem.read_bits(dev, addr, count)
                log("%s read %s = %s" % (kind, where, "".join(map(str, bits))))
                return 0, ("".join(map(str, bits)) if r.ascii else pack_bits_binary(bits))
            words = mem.read_words(dev, addr, count)
            log("%s read %s = %s" % (kind, where, words_text(words)))
            return 0, ("".join("%04X" % w for w in words) if r.ascii else struct.pack("<%dH" % count, *words))
        # write
        if is_bit:
            bits = [int(c) for c in rest[:count]] if r.ascii else unpack_bits_binary(rest, count)
            if len(bits) != count:
                raise McError(ERR_BAD_LENGTH, "bit write data short")
            mem.write_bits(dev, addr, bits)
            log("%s write %s = %s" % (kind, where, "".join(map(str, bits))))
        else:
            if r.ascii:
                words = [int(rest[i * 4:i * 4 + 4], 16) for i in range(count)]
            else:
                if len(rest) < 2 * count:
                    raise McError(ERR_BAD_LENGTH, "word write data short")
                words = list(struct.unpack_from("<%dH" % count, rest, 0))
            mem.write_words(dev, addr, words)
            log("%s write %s = %s" % (kind, where, words_text(words)))
        mem.save()
        return 0, ("" if r.ascii else b"")
    if r.cmd == 0x0101:
        log("%s read CPU model -> %s" % (kind, CPU_NAME))
        name = CPU_NAME.ljust(16)
        return 0, (name + "%04X" % CPU_CODE if r.ascii else name.encode() + struct.pack("<H", CPU_CODE))
    if r.cmd in (0x1001, 0x1002, 0x1003, 0x1005, 0x1006):
        log("%s remote control 0x%04X (ignored, answered OK)" % (kind, r.cmd))
        return 0, ("" if r.ascii else b"")
    raise McError(ERR_BAD_CMD, "unsupported command 0x%04X sub 0x%04X" % (r.cmd, r.sub))


def process(buf, mem, peer):
    """Consume as many whole requests from buf as possible. Returns (responses, remaining_buf, fatal)."""
    out = []
    while buf:
        try:
            if buf[:1] in (b"5",):
                r, used = parse_ascii(buf)
            else:
                r, used = parse_binary(buf)
        except ValueError as ex:
            log("%s unparseable request (%s): %s" % (peer, ex, buf[:64].hex(" ")))
            return out, b"", True
        if r is None:
            break
        raw = buf[:used]
        buf = buf[used:]
        try:
            code, payload = handle(r, mem)
        except McError as ex:
            log("%s error 0x%04X: %s; request %s" % (peer, ex.code, ex, raw[:64].hex(" ") if not r.ascii else raw[:80]))
            code, payload = ex.code, b""
        except Exception as ex:  # malformed body
            log("%s malformed request (%s): %s" % (peer, ex, raw[:64].hex(" ")))
            code, payload = ERR_BAD_LENGTH, b""
        out.append(build_response(r, code, payload))
    return out, buf, False


class Server:
    def __init__(self, mem, delay):
        self.mem, self.delay = mem, delay

    async def tcp_client(self, reader, writer):
        peer = "%s:%d" % writer.get_extra_info("peername")[:2]
        log("tcp connect %s" % peer)
        buf = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                resps, buf, fatal = process(buf, self.mem, peer)
                for resp in resps:
                    d = self.delay.get()
                    if d:
                        await asyncio.sleep(d / 1000)
                    writer.write(resp)
                await writer.drain()
                if fatal:
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            log("tcp disconnect %s" % peer)
            writer.close()


class Udp(asyncio.DatagramProtocol):
    def __init__(self, srv):
        self.srv = srv

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        resps, _, _ = process(data, self.srv.mem, "udp %s:%d" % addr[:2])
        d = self.srv.delay.get()
        loop = asyncio.get_running_loop()
        for resp in resps:
            if d:
                loop.call_later(d / 1000, self.transport.sendto, resp, addr)
            else:
                self.transport.sendto(resp, addr)


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--state", help="JSON file the current memory is saved to after each write")
    ap.add_argument("--init", help="JSON file of starting values (see Memory.load_init)")
    ap.add_argument("--refuse", help="ranges whose writes get end code 0xC051, e.g. D:9000-9099,M:9000-9099")
    ap.add_argument("--delay-file", help='JSON {"default": ms} reply delay, re-read on change')
    ap.add_argument("--no-udp", action="store_true")
    a = ap.parse_args()

    mem = Memory(a.state, parse_refuse(a.refuse))
    if a.init:
        mem.load_init(a.init)
        mem.save()
    srv = Server(mem, Delay(a.delay_file))
    tcp = await asyncio.start_server(srv.tcp_client, a.bind, a.port)
    if not a.no_udp:
        await asyncio.get_running_loop().create_datagram_endpoint(lambda: Udp(srv), local_addr=(a.bind, a.port))
    log("MC protocol PLC simulator on %s:%d (tcp%s), 3E/4E binary+ascii, cpu %s, init=%s, refuse=%s"
        % (a.bind, a.port, "" if a.no_udp else "+udp", CPU_NAME, a.init, a.refuse))
    async with tcp:
        await tcp.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
