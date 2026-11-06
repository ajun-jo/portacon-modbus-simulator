#!/usr/bin/env python3
"""Generate the simulator's machine files from the ADL2012 PLC data sheet in metadata/.

  python3 xlsx_to_init.py                        uses the newest .xlsx in metadata/
  python3 xlsx_to_init.py metadata/other.xlsx

Reads the "ADL2012 PLC 어드레스" (production data), "ADL2012 알람리스트" (alarms) and "ADL2012(IP)" tabs and writes
  machines/<id>.init.json    one simulator per machine: wind1-4 (권선기), sp1-2 (SP접착기), tp1 (6면측정TP기)
  machines/address_map.csv   the Modbus address of every tag, for setting up your server (opens in Excel)

Address mapping. The sheet has each vendor's own addresses, and Modbus has only 0-65535 per table:
  Mitsubishi Q  D/R/W/ZR/SD n -> hr n      M/L/B/X/Y/SM n -> coil n          (same number)
  Yaskawa MP    MW n -> hr n   IW n -> ir n
                MB <word><hex bit> -> coil word*16+bit (MEMOBUS numbering), or, when that coil number is past 65535,
                bit <hex bit> of hr <word> (type BIT): MB00055336 -> hr 5533 bit 6
  Keyence KV    DM/EM/FM/ZF n -> hr n      R/MR/LR/CR n -> coil n            (same number)
A tag whose address still doesn't fit becomes a placeholder with "addr": null: listed, but not served until you
set "addr" in the machine file. Tags with no mapping, or overlapping an earlier tag, are listed under "skipped".

Regenerating keeps what you changed in an existing machines/<id>.init.json: each tag's value, the addr you gave a
placeholder, and port, unit_id and defaults. Change the mapping itself by editing MAPPING below.
"""
import csv
import glob
import json
import os
import random
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modbus_plc_sim import encode_value, word_count  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "machines")
TABS = (("ADL2012 PLC 어드레스", "data"), ("ADL2012 알람리스트", "alarm"))
IP_TAB = "ADL2012(IP)"
KINDS = (("SP접착기", "sp"), ("6면측정TP기", "tp"), ("측정TP기", "tp"), ("권선기", "wind"))
FIXED_PORTS = {"sp1": 5020, "sp2": 5021}  # kept from before the sheet was imported
FIRST_FREE_PORT = 5022
LINK_KEYS = ("port", "unit_id", "defaults")
# Q, MP and KV store 32-bit values low word first. Strings: first character in the low byte of each word
# (confirmed for Mitsubishi; check Keyence/Yaskawa strings against a real PLC).
DEFAULTS = {"word_order": "little", "byte_order": "little", "encoding": "utf-8"}

# vendor -> list of (address regex, table, number base, how to number)
MAPPING = {
    "mitsubishi": [(r"(D|R|ZR|SD)(\d+)", "hr", 10, "same"), (r"(W)([0-9A-F]+)", "hr", 16, "same"),
                   (r"(M|L|SM)(\d+)", "coil", 10, "same"), (r"(B|X|Y)([0-9A-F]+)", "coil", 16, "same")],
    "yaskawa": [(r"(MW)(\d+)", "hr", 10, "same"), (r"(IW)([0-9A-F]+)", "ir", 16, "same"),
                (r"(MB)(\d+[0-9A-F])", "coil", 10, "word_bit"), (r"(IB)([0-9A-F]+)", "di", 16, "same")],
    "keyence": [(r"(DM|EM|FM|ZF)(\d+)", "hr", 10, "same"), (r"(R|MR|LR|CR)(\d+)", "coil", 10, "same")],
}


class NoAddress(ValueError):
    """The PLC address has a mapping but lands past 65535: becomes a placeholder tag."""

    def __init__(self, msg, table):
        super().__init__(msg)
        self.table = table


# ---------------------------------------------------------------- xlsx reading (standard library only)

def read_xlsx(path):
    """{sheet name: [{column letter: text}, ...]} with one dict per row."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    z = zipfile.ZipFile(path)
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            strings.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for s in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", ns):
        target = rels[s.get(rid)].lstrip("/")
        root = ET.fromstring(z.read(target if target.startswith("xl/") else "xl/" + target))
        rows = []
        for row in root.iter("{%s}row" % ns["m"]):
            cells = {}
            for c in row.findall("m:c", ns):
                v, t = c.find("m:v", ns), c.get("t")
                if t == "s" and v is not None:
                    val = strings[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
                else:
                    val = v.text if v is not None else ""
                cells[re.match(r"[A-Z]+", c.get("r")).group()] = val.strip()
            rows.append(cells)
        out[s.get("name")] = rows
    return out


def parse_sections(rows, group):
    """Yield (machine name, plc info, tag rows) for each '■ machine' block of a tab."""
    machine, cols, info, tags = None, None, {}, []
    for r in rows + [{"B": "■ END"}]:
        b = r.get("B", "")
        if b.startswith("■"):
            if machine:
                yield machine, info, tags
            machine, cols, info, tags = b.lstrip("■ ").strip(), None, {}, []
        elif b == "태그 이름":
            cols = {v: k for k, v in r.items()}
        elif machine and cols and b:
            get = lambda h: r.get(cols.get(h, "?"), "")  # noqa: E731
            for key, head in (("plc", "PLC"), ("ip", "PLC IP"), ("plc_port", "PORT")):
                if get(head):
                    info.setdefault(key, get(head))
            if get("주소"):
                tags.append({"tag": b, "group": group, "plc_addr": get("주소").upper(),
                             "mem": get("메모리 타입").upper(), "dtype": get("데이터 타입").upper()})


def parse_ip_tab(rows):
    """{machine name: {"ip": ..., "wifi_bridge": ...}} from the IP tab (설비 / 호기 / PLC / WIFI Bridge columns)."""
    out, cols, kind = {}, None, ""
    for r in rows:
        if "PLC" in r.values() and cols is None:
            cols = {v: k for k, v in r.items()}
            continue
        if not cols:
            continue
        kind = r.get("B") or kind
        unit = r.get("C", "")  # 호기; the column has no header
        ip = r.get(cols["PLC"], "")
        if kind and unit and ip:
            out["%s %s" % (kind, unit)] = {"ip": ip, "wifi_bridge": r.get(cols.get("WIFI Bridge", "?"), "")}
    return out


# ---------------------------------------------------------------- conversion

def machine_id(name):
    m = re.search(r"(\d+)\s*호기", name)
    for prefix, short in KINDS:
        if name.startswith(prefix) and m:
            return short + m.group(1)
    return re.sub(r"\W+", "_", name).strip("_").lower()


def vendor_of(plc):
    p = plc.lower()
    return next((v for v in MAPPING if v in p), None)


def modbus_address(vendor, plc_addr):
    """(table, addr, bit or None). Raises NoAddress when it lands past 65535, ValueError when unmapped."""
    for pattern, table, base, how in MAPPING.get(vendor, []):
        m = re.fullmatch(pattern, plc_addr)
        if not m:
            continue
        if how == "word_bit":  # Yaskawa MB/IB: decimal word number, last digit is the hex bit
            word, bit = int(m.group(2)[:-1]), int(m.group(2)[-1], 16)
            if word * 16 + bit <= 0xFFFF:
                return table, word * 16 + bit, None
            if word <= 0xFFFF:  # the flattened coil number doesn't fit, the word does: bit inside a register
                return "hr" if table == "coil" else "ir", word, bit
            raise NoAddress("%s is word %d, past Modbus's 65535 limit" % (plc_addr, word), "hr")
        n = int(m.group(2), base)
        if n > 0xFFFF:
            raise NoAddress("%s is past Modbus's 65535 limit" % plc_addr, table)
        return table, n, None
    raise ValueError("no Modbus mapping for %s address %s" % (vendor or "unknown PLC", plc_addr))


def tag_type(t):
    words = int(re.match(r"(\d*)", t["mem"]).group(1) or 1) if "WORD" in t["mem"] else 1
    dtype = t["dtype"]
    if t["mem"] == "BIT" or dtype == "BOOL":
        return "BOOL", 1
    if dtype not in ("INT16", "UINT16", "INT32", "UINT32", "FLOAT32", "STRING"):
        raise ValueError("unknown data type %r" % t["dtype"])
    return dtype, words


def starting_value(t, mid, ctx, rng):
    """Plausible made-up value. Counters above 65535 show up word-order mistakes; LOT numbers have an odd length,
    so the last word holds one character and a NUL, which shows up byte-order mistakes."""
    name, typ, words = t["tag"], t["type"], t.get("words", 1)
    if typ in ("BOOL", "BIT"):
        return 1 if name == "가동중" else 0  # alarms off
    if typ == "STRING":
        if "LOT" in name.upper():
            s = ("LOT2609%s%02d" % (mid[:1].upper(), rng.randint(1, 99)))[:words * 2]
            return s if len(s) % 2 else s[:-1]
        return ("ADL2012-%s" % mid.upper())[:words * 2]
    if "투입수" in name:
        v = rng.randint(65536, 199999)
    elif "양품수" in name or "생산수" in name:
        v = ctx.get("투입수", 100000) - rng.randint(10, 60)
    elif "설정수" in name:
        v = rng.choice((50000, 100000, 200000))
    elif any(k in name for k in ("사용수", "연마수", "소모량")):
        v = rng.randint(1000, 90000)
    elif any(k in name for k in ("불량", "미스", "타임아웃")):
        v = rng.randint(0, 40)
    elif "온도" in name:
        v = rng.randint(180, 260)
    elif "하중" in name:
        v = rng.randint(30, 80)
    elif "택타임" in name:
        v = rng.randint(30, 60)
    elif name == "품명":
        v = 2012
    else:
        v = rng.randint(0, 1000)
    lo, hi = {"INT16": (-32768, 32767), "UINT16": (0, 65535)}.get(typ, (-2 ** 31, 2 ** 31 - 1))
    return max(lo, min(hi, v))


def claim(used, table, addr, size, bit, tag):
    """Reserve the address; raises ValueError if an earlier tag already uses it. Keys: (word, bit or None)."""
    m = used.setdefault(table, {})
    if bit is not None:
        clash = m.get((addr, None)) or m.get((addr, bit))
        if clash:
            raise ValueError("%s %d bit %d overlaps tag %r" % (table, addr, bit, clash))
        m[(addr, bit)] = tag
        return
    for a in range(addr, addr + size):
        clash = next((v for (w, _), v in m.items() if w == a), None)
        if clash:
            raise ValueError("%s %d overlaps tag %r" % (table, a, clash))
    for a in range(addr, addr + size):
        m[(a, None)] = tag


def build_machine(name, info, rows, index, used_ports, old):
    mid = machine_id(name)
    vendor = vendor_of(info.get("plc", ""))
    rng = random.Random(mid)
    old_tags = {t["tag"]: t for t in old.get("tags", [])}
    tags, skipped, used, names, ctx = [], [], {}, set(), {}
    for r in rows:
        tag = r["tag"]
        if tag in names:
            tag = "%s (%s)" % (tag, "알람" if r["group"] == "alarm" else "데이터")
        names.add(tag)
        note = None
        try:
            typ, words = tag_type(r)
            try:
                table, addr, bit = modbus_address(vendor, r["plc_addr"])
            except NoAddress as ex:
                table, addr, bit, note = ex.table, None, None, "%s; waiting on the equipment vendor. " \
                    "Set \"addr\" to serve this tag (regenerating keeps it)." % ex
                kept = old_tags.get(tag, {})
                if kept.get("addr") is not None and kept.get("type") == typ:  # an address you filled in
                    table, addr, bit = kept["table"], kept["addr"], kept.get("bit")
            if bit is not None:
                if typ != "BOOL":
                    raise ValueError("%s is a bit address but the sheet says %s" % (r["plc_addr"], typ))
                typ = "BIT"
            elif (typ == "BOOL") != (table in ("coil", "di")):
                raise ValueError("%s is a %s address but the sheet says %s" % (r["plc_addr"], table, typ))
            if addr is not None:
                size = 1 if typ in ("BOOL", "BIT") else words
                if addr + size > 0x10000:
                    raise ValueError("%s %d x%d runs past 65535" % (table, addr, size))
                claim(used, table, addr, size, bit, tag)
        except ValueError as ex:
            skipped.append({"tag": tag, "group": r["group"], "plc_addr": r["plc_addr"], "reason": str(ex)})
            continue
        t = {"tag": tag, "group": r["group"], "table": table, "addr": addr, "type": typ}
        if typ == "BIT":
            t["bit"] = bit
        if typ == "STRING":
            t["words"] = words
        value = old_tags.get(tag, {}).get("value")
        try:
            if value is not None:
                encode_value(typ, value, dict(DEFAULTS, **t))
        except ValueError:
            value = None
        t["value"] = value if value is not None else starting_value(t, mid, ctx, rng)
        if "투입수" in tag:
            ctx["투입수"] = t["value"]
        t["plc_addr"] = r["plc_addr"]
        if note:
            t["note"] = note
        tags.append(t)
    port = old.get("port") or FIXED_PORTS.get(mid)
    if not port:
        port = FIRST_FREE_PORT
        while port in used_ports or port in FIXED_PORTS.values():
            port += 1
    used_ports.add(port)
    bridge = info.get("wifi_bridge", "")
    cfg = {
        "comment": "%s. Generated by xlsx_to_init.py from the sheet; regenerating keeps tag values, placeholder "
                   "addresses, port, unit_id and defaults. addr is the 0-based Modbus address (40001 = hr 0)." % name,
        "source": "%s (%s, %s)" % (os.path.basename(SRC), ", ".join(t for t, _ in TABS), IP_TAB),
        "machine": name,
        "real_plc": {"make": info.get("plc", ""), "ip": info.get("ip", ""), "port": info.get("plc_port", ""),
                     "connection": "WiFi bridge %s" % bridge if re.search(r"\d", bridge) else "wired"},
        "port": port, "unit_id": index, "defaults": dict(DEFAULTS),
    }
    cfg.update({k: old[k] for k in LINK_KEYS if k in old})
    cfg["tags"], cfg["skipped"] = tags, skipped
    return mid, cfg


def write_json(path, cfg):
    head = {k: v for k, v in cfg.items() if k not in ("tags", "skipped")}
    lines = ["{"] + [" %s: %s," % (json.dumps(k), json.dumps(v, ensure_ascii=False)) for k, v in head.items()]
    for key in ("tags", "skipped"):
        items = cfg[key]
        lines.append(' "%s": [' % key)
        lines.append(",\n".join("  " + json.dumps(t, ensure_ascii=False) for t in items))
        lines.append(" ]," if key == "tags" else " ]")
    lines.append("}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(line for line in lines if line != "") + "\n")


def main():
    global SRC
    if len(sys.argv) > 1:
        SRC = sys.argv[1]
    else:
        found = sorted(glob.glob(os.path.join(HERE, "metadata", "*.xlsx")), key=os.path.getmtime)
        if not found:
            raise SystemExit("no .xlsx in metadata/")
        SRC = found[-1]
    sheets = read_xlsx(SRC)
    machines = {}  # name -> (info, rows), in sheet order
    for tab, group in TABS:
        if tab not in sheets:
            raise SystemExit("tab %r not found in %s (tabs: %s)" % (tab, SRC, ", ".join(sheets)))
        for name, info, rows in parse_sections(sheets[tab], group):
            m = machines.setdefault(name, ({}, []))
            for k, v in info.items():
                if not re.search(r"\d", m[0].get(k, "")):  # alarm tab says 부여 예정 where the data tab has a port
                    m[0][k] = v
            m[1].extend(rows)
    ips = parse_ip_tab(sheets.get(IP_TAB, []))
    print("source: %s" % SRC)
    for name, (info, _) in machines.items():
        ip = ips.get(name)
        if not ip:
            print("warning: %s is not in the %s tab" % (name, IP_TAB))
            continue
        if info.get("ip") and info["ip"] != ip["ip"]:
            print("warning: %s PLC IP is %s in the address tab but %s in the %s tab"
                  % (name, info["ip"], ip["ip"], IP_TAB))
        info.setdefault("ip", ip["ip"])
        info["wifi_bridge"] = ip["wifi_bridge"]
    os.makedirs(OUT, exist_ok=True)
    used_ports, csv_rows, last_report = set(), [], None
    for path in glob.glob(os.path.join(OUT, "*.init.json")):
        with open(path, encoding="utf-8") as f:
            used_ports.add(json.load(f).get("port"))
    for index, (name, (info, rows)) in enumerate(machines.items(), 1):
        path = os.path.join(OUT, machine_id(name) + ".init.json")
        old = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            used_ports.discard(old.get("port"))
        mid, cfg = build_machine(name, info, rows, index, used_ports, old)
        write_json(path, cfg)
        tags = cfg["tags"]
        pending = [t for t in tags if t["addr"] is None]
        bits = [t for t in tags if t["type"] == "BIT"]
        print("%-6s %-14s port %d unit %-2d %3d tags (%d bit-in-register, %d pending), %d skipped  -> %s"
              % (mid, name, cfg["port"], cfg["unit_id"], len(tags), len(bits), len(pending), len(cfg["skipped"]),
                 os.path.relpath(path, HERE)))
        report = [("pending", t["tag"], t["plc_addr"], t["note"]) for t in pending] + \
                 [("skipped", s["tag"], s["plc_addr"], s["reason"]) for s in cfg["skipped"]]
        if report and report == last_report:
            print("         (same as above)")
        else:
            for line in report:
                print("         %s %s %s: %s" % line)
        last_report = report
        d = cfg["defaults"]
        for t in tags:
            base = {"coil": 1, "di": 100001, "ir": 300001, "hr": 400001}[t["table"]]
            pend = t["addr"] is None
            csv_rows.append([
                mid, name, cfg["port"], cfg["unit_id"], t["group"], t["tag"], t["plc_addr"], t["table"],
                "" if pend else t["addr"], "" if pend else base + t["addr"], t.get("bit", ""), t["type"],
                "" if t["type"] in ("BOOL", "BIT") else word_count(t["type"], t),
                d.get("word_order") if t["type"] in ("INT32", "UINT32", "FLOAT32") else "",
                d.get("byte_order") if t["type"] == "STRING" else "",
                "pending: " + t["note"] if pend else "ok"])
        for s in cfg["skipped"]:
            csv_rows.append([mid, name, cfg["port"], cfg["unit_id"], s["group"], s["tag"], s["plc_addr"],
                             "", "", "", "", "", "", "", "", "skipped: " + s["reason"]])
    with open(os.path.join(OUT, "address_map.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["machine", "name", "sim_port", "unit_id", "group", "tag", "plc_addr", "table",
                    "modbus_addr (0-based)", "modbus_ref (1-based, 6-digit)", "bit", "type", "words", "word_order",
                    "byte_order", "status"])
        w.writerows(csv_rows)
    print("address map -> machines/address_map.csv")


SRC = None
if __name__ == "__main__":
    main()
