#!/usr/bin/env python3
"""Small MC protocol (SLMP 3E) client for checking the simulator and changing its values while ptcsw polls.

Examples:
  mc_client.py --port 4000 read D1104 2                 raw words
  mc_client.py --port 4000 read D1104 --type INT32
  mc_client.py --port 4000 read R771 5 --type STRING    LH byte order, stops at the first NUL
  mc_client.py --port 4000 read M5 1 --bit
  mc_client.py --port 4000 write D1104 --type INT32 123456
  mc_client.py --port 4000 write R771 --type STRING --words 5 "LOT-0002"
  mc_client.py --port 4000 write M5 --bit 0
Options --ascii, --udp and --iqr switch the frame format, so each ptcsw setting can be exercised.
A second client on the simulator is harmless: MC over TCP/UDP is request/response per connection.
"""
import argparse
import re
import socket
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mc_plc_sim import DEVICES, encode_value  # noqa: E402


def parse_dev(s):
    m = re.fullmatch(r"([A-Za-z]+)([0-9A-Fa-f]+)", s)
    if not m or m.group(1).upper() not in DEVICES:
        raise SystemExit("bad device %r" % s)
    dev = m.group(1).upper()
    return dev, int(m.group(2), DEVICES[dev][2])


def request(a, cmd, sub, dev, addr, count, data):
    is_iqr = sub in (2, 3)
    code, _, base = DEVICES[dev]
    if a.ascii:
        if is_iqr:
            spec = dev.ljust(4, "*") + (("%08X" if base == 16 else "%08d") % addr)
        else:
            spec = dev.ljust(2, "*") + (("%06X" if base == 16 else "%06d") % addr)
        body = "%04X%04X%04X" % (0x10, cmd, sub) + spec + "%04X" % count + data
        frame = ("5000" + "00FF03FF00" + "%04X" % len(body) + body).encode()
    else:
        if is_iqr:
            spec = struct.pack("<IH", addr, code)
        else:
            spec = struct.pack("<I", addr)[:3] + bytes([code])
        body = struct.pack("<HHH", 0x10, cmd, sub) + spec + struct.pack("<H", count) + data
        frame = b"\x50\x00" + bytes([0x00, 0xFF, 0xFF, 0x03, 0x00]) + struct.pack("<H", len(body)) + body
    if a.udp:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(a.timeout)
        s.sendto(frame, (a.host, a.port))
        resp = s.recv(65536)
    else:
        s = socket.create_connection((a.host, a.port), timeout=a.timeout)
        s.sendall(frame)
        resp = b""
        need = 22 if a.ascii else 11
        while len(resp) < need:
            resp += s.recv(65536)
        if a.ascii:
            need = 18 + int(resp[14:18], 16)
        else:
            need = 9 + struct.unpack_from("<H", resp, 7)[0]
        while len(resp) < need:
            resp += s.recv(65536)
    s.close()
    if a.ascii:
        end = int(resp[18:22], 16)
        payload = resp[22:].decode()
    else:
        end = struct.unpack_from("<H", resp, 9)[0]
        payload = resp[11:]
    if end:
        raise SystemExit("PLC end code 0x%04X" % end)
    return payload


def decode_words(a, payload, n):
    if a.ascii:
        return [int(payload[i * 4:i * 4 + 4], 16) for i in range(n)]
    return list(struct.unpack_from("<%dH" % n, payload, 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--ascii", action="store_true")
    ap.add_argument("--udp", action="store_true")
    ap.add_argument("--iqr", action="store_true", help="iQ-R device spec (subcommand 0002/0003)")
    ap.add_argument("--timeout", type=float, default=3)
    ap.add_argument("op", choices=["read", "write"])
    ap.add_argument("device")
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--bit", action="store_true")
    ap.add_argument("--type", default="WORDS", help="WORDS INT16 UINT16 INT32 UINT32 FLOAT32 STRING")
    ap.add_argument("--words", type=int, help="string length in words for STRING writes")
    ap.add_argument("--byte-order", default="LH")
    a = ap.parse_intermixed_args()
    dev, addr = parse_dev(a.device)
    typ = a.type.upper()
    sub = (1 if a.bit else 0) + (2 if a.iqr else 0)

    if a.op == "read":
        sizes = {"INT16": 1, "UINT16": 1, "INT32": 2, "UINT32": 2, "FLOAT32": 2}
        n = int(a.rest[0]) if a.rest else sizes.get(typ, 1)
        payload = request(a, 0x0401, sub, dev, addr, n, "" if a.ascii else b"")
        if a.bit:
            bits = [int(c) for c in payload[:n]] if a.ascii else \
                [(payload[i // 2] >> (4 if i % 2 == 0 else 0)) & 1 for i in range(n)]
            print("".join(map(str, bits)))
            return
        w = decode_words(a, payload, n)
        if typ == "WORDS":
            print(" ".join("%04X" % x for x in w))
        elif typ in ("INT16", "UINT16"):
            print(struct.unpack("<h" if typ == "INT16" else "<H", struct.pack("<H", w[0]))[0])
        elif typ in ("INT32", "UINT32", "FLOAT32"):
            raw = struct.pack("<HH", w[0], w[1])
            print(struct.unpack({"INT32": "<i", "UINT32": "<I", "FLOAT32": "<f"}[typ], raw)[0])
        elif typ == "STRING":
            bs = bytearray()
            for x in w:
                pair = (x & 0xFF, x >> 8) if a.byte_order.upper() == "LH" else (x >> 8, x & 0xFF)
                bs.extend(pair)
            print(repr(bytes(bs).split(b"\0", 1)[0].decode("latin-1")))
        return

    if not a.rest:
        raise SystemExit("write needs a value")
    if a.bit:
        bits = [int(c) for c in "".join(a.rest)]
        data = "".join(map(str, bits)) if a.ascii else bytes(
            ((bits[i] & 1) << 4) | ((bits[i + 1] & 1) if i + 1 < len(bits) else 0) for i in range(0, len(bits), 2))
        request(a, 0x1401, sub, dev, addr, len(bits), data)
        print("ok")
        return
    if typ == "WORDS":
        words = [int(x, 16) for x in a.rest]
    else:
        words = encode_value(typ, a.rest[0], {"words": a.words, "byte_order": a.byte_order})
    data = "".join("%04X" % x for x in words) if a.ascii else struct.pack("<%dH" % len(words), *words)
    request(a, 0x1401, sub, dev, addr, len(words), data)
    print("ok")


if __name__ == "__main__":
    main()
