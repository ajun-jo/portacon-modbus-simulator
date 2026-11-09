#!/usr/bin/env python3
"""Self-test of a running simulator, as a master on its real endpoint (the pty, or the TCP port).

  python3 check_sim.py com1          then ./run.sh reset com1 (it writes test values, and they're counted)

It won't run while another master read from the simulator in the last 10 s (--force to run anyway): a second
master on a serial port mixes up frames and makes ptcsw log errors.
"""
import argparse
import os
import select
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from modbus_plc_sim import crc16, decode_value, load_config  # noqa: E402


class Rtu:
    def __init__(self, path):
        import termios
        import tty
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = attrs[5] = termios.B9600
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def call(self, unit, pdu, timeout=1.5, reply=True):
        body = bytes([unit]) + pdu
        os.write(self.fd, body + struct.pack("<H", crc16(body)))
        buf, end = b"", time.time() + (timeout if reply else 0.4)
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                buf += os.read(self.fd, 512)
            elif buf:
                break
        if not buf:
            return None
        if crc16(buf):
            raise AssertionError("bad CRC in reply %s" % buf.hex(" "))
        return buf[1:-2]


class Tcp:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.tid = 0

    def call(self, unit, pdu, timeout=1.5, reply=True):
        self.tid += 1
        self.s.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, unit) + pdu)
        head = self.s.recv(7)
        return self.s.recv(struct.unpack(">H", head[4:6])[0] - 1)


def regs(r):
    return list(struct.unpack(">%dH" % (r[1] // 2), r[2:2 + r[1]]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    import webui  # for last_poll
    cfg = load_config(os.path.join(HERE, "machines", a.name + ".init.json"))
    poll = webui.last_poll(a.name)
    if poll and poll["ago"] < 10 and not a.force:
        raise SystemExit("%s was read %.0f s ago by %s: stop that master first (or --force)"
                         % (a.name, poll["ago"], poll["peer"]))
    ep = cfg["endpoint"]
    m = Rtu(ep["path"]) if ep["kind"] == "pty" else Tcp(ep["port"])
    unit = min(cfg["devices"]) if cfg["devices"] else 1
    fails = 0

    def check(name, ok, got=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  got %s" % (got.hex(" ") if isinstance(got, bytes) else got)))
        fails += not ok

    print("== %s: %s, unit %d" % (a.name, ep.get("path") or "tcp %d" % ep.get("port", 0), unit))
    for t in cfg["devices"].get(unit, {"tags": []})["tags"]:
        if t["pending"]:
            continue
        fc = {"coil": 1, "di": 2, "hr": 3, "ir": 4}[t["table"]]
        r = m.call(unit, struct.pack(">BHH", fc, t["addr"], t["words"]))
        if r and r[0] == fc:
            words = list(r[2:2 + r[1]]) if fc in (1, 2) else regs(r)
            v = decode_value(t["type"], [words[0] & 1] if fc in (1, 2) else words, t)
            print("     %s %s %d = %r" % (t["tag"], t["table"], t["addr"], v))
        else:
            check("read %s" % t["tag"], False, r)
    check("fc6 write hr 10 = 1234", m.call(unit, struct.pack(">BHH", 6, 10, 1234)) == struct.pack(">BHH", 6, 10, 1234))
    r = m.call(unit, struct.pack(">BHH", 3, 10, 1))
    check("fc3 reads it back", r is not None and regs(r) == [1234], r)
    check("fc16 write hr 20..22", m.call(unit, struct.pack(">BHHB3H", 16, 20, 3, 6, 1, 2, 65535)) == struct.pack(">BHH", 16, 20, 3))
    r = m.call(unit, struct.pack(">BHH", 3, 20, 3))
    check("fc3 reads [1, 2, 65535]", r is not None and regs(r) == [1, 2, 65535], r)
    check("fc5 write coil 5", m.call(unit, struct.pack(">BHH", 5, 5, 0xFF00)) == struct.pack(">BHH", 5, 5, 0xFF00))
    check("fc15 write coil 100..109", m.call(unit, struct.pack(">BHHBBB", 15, 100, 10, 2, 0x0D, 0x03)) == struct.pack(">BHH", 15, 100, 10))
    r = m.call(unit, struct.pack(">BHH", 1, 100, 10))
    check("fc1 reads them back", r == bytes([1, 2, 0x0D, 0x03]), r)
    r = m.call(unit, struct.pack(">BHH", 6, 9000, 1))
    check("write to refused hr 9000 -> exception", r is not None and r[0] == 0x86, r)
    r = m.call(unit, struct.pack(">BHH", 3, 65535, 2))
    check("read past 65535 -> exception 02", r == bytes([0x83, 2]), r)
    if ep["kind"] == "pty":
        check("broadcast (unit 0) gets no reply", m.call(0, struct.pack(">BHH", 6, 30, 77), reply=False) is None)
        r = m.call(unit, struct.pack(">BHH", 3, 30, 1))
        check("broadcast write applied", r is not None and regs(r) == [77], r)
    print("ALL PASS" if not fails else "%d FAILED" % fails)
    print("It wrote test values and they were counted: ./run.sh reset %s" % a.name)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
