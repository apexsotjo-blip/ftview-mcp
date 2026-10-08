"""HMI tag database as View's Tag Import and Export Wizard CSV (format ###002).

The file is UTF-8 with BOM, CRLF line endings, ';' comment lines, a version line
that must not be removed, folders before tags. Columns (0-based):

 0 Tag Type (A analog, D digital, S string, F folder)   1 Tag Name (Folder\\Name)
 2 Description   3 Read Only (T/F)   4 Data Source (M memory, D device)   5 Security Code
 6 Alarmed (T/F)   7 Native Type   8 Value Type   9 Min  10 Max  11 Initial Analog
12 Scale  13 Offset  14 DeadBand  15 Units  16 Off Label  17 On Label  18 Initial Digital
19 Length String  20 Initial String  21 Retentive  22 Address (device: /::[Shortcut]Tag)
23 System Source Name  24 System Source Index  25 RIO Address  26..28 Block columns
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

HEADER = (";Tag Type, Tag Name, Tag Description, Read Only, Data Source, Security Code, Alarmed, Native Type, "
          "Value Type, Min Analog, Max Analog, Initial Analog, Scale, Offset, DeadBand, Units, Off Label Digital, "
          "On Label Digital, Initial Digital, Length String, Initial String, Retentive, Address, System Source Name, "
          "System Source Index, RIO Address, Element Size Block, Number Elements Block, Initial Block")
VERSION = ";###002 - THIS LINE CONTAINS VERSION INFORMATION. DO NOT REMOVE!!!"
COLS = ["type", "name", "description", "read_only", "source", "security", "alarmed", "native_type",
        "value_type", "min", "max", "initial_analog", "scale", "offset", "deadband", "units", "off_label",
        "on_label", "initial_digital", "length", "initial_string", "retentive", "address", "sys_name",
        "sys_index", "rio", "block_size", "block_count", "block_initial"]
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ \-]*(\\[A-Za-z0-9_][A-Za-z0-9_ \-]*)*$")
ADDRESS_RE = re.compile(r"^(/?[^:]*::)?\[[^\]]+\].+$")


class TagError(ValueError):
    pass


@dataclass
class Tag:
    type: str
    name: str
    values: dict = field(default_factory=dict)

    def row(self) -> list:
        v = {"type": self.type, "name": self.name, **self.values}
        return [v.get(c) for c in COLS]

    def folder(self) -> str:
        return self.name.rsplit("\\", 1)[0] if "\\" in self.name else ""


class TagDB:
    def __init__(self):
        self.folders: list[str] = []
        self.tags: dict[str, Tag] = {}

    # -- building -----------------------------------------------------------
    def add_folder(self, path: str):
        parts = path.split("\\")
        for i in range(1, len(parts) + 1):
            f = "\\".join(parts[:i])
            if f not in self.folders:
                self.folders.append(f)

    def _add(self, typ, name, source, description, read_only, security, extra, address=None):
        if not NAME_RE.match(name):
            raise TagError(f"invalid tag name {name!r}")
        if name.lower() in (n.lower() for n in self.tags):
            raise TagError(f"duplicate tag {name!r}")
        if source not in ("memory", "device"):
            raise TagError("source must be 'memory' or 'device'")
        if source == "device":
            if not address:
                raise TagError(f"device tag {name!r} needs an address like '::[SCADA]Tag'")
            address = address.strip().strip("{}")
            if not ADDRESS_RE.match(address):
                raise TagError(f"device address {address!r} should look like '::[Shortcut]Tag' or '[Shortcut]Tag'")
            if not address.startswith(("/", "::")):
                address = "::" + address if address.startswith("[") else address
            if address.startswith("::"):
                address = "/" + address
        t = Tag(typ, name, {"description": description or None, "read_only": "T" if read_only else "F",
                            "source": "D" if source == "device" else "M", "security": security or "*",
                            "alarmed": "F", **extra, "address": address if source == "device" else None})
        if "\\" in name:
            self.add_folder(t.folder())
        self.tags[name] = t
        return t

    def add_analog(self, name, source="memory", description="", min=0, max=100, initial=0, scale=1, offset=0,
                   deadband=0, units="", data_type="F", read_only=False, security="*", address=None,
                   retentive=False):
        # initial is required for device tags too (View rejects an empty one: "Initial value is invalid")
        extra = {"native_type": data_type, "value_type": data_type, "min": min, "max": max,
                 "initial_analog": initial, "scale": scale, "offset": offset, "deadband": deadband,
                 "units": units or None}
        if source == "memory":
            extra.update(retentive=1 if retentive else 0)
        return self._add("A", name, source, description, read_only, security, extra, address)

    def add_digital(self, name, source="memory", description="", off_label="Off", on_label="On",
                    initial=False, read_only=False, security="*", address=None, retentive=False):
        # View requires Initial Digital = On or Off label for device tags too
        # ("initial value must be equal to either the On Label or Off Label")
        extra = {"off_label": off_label, "on_label": on_label,
                 "initial_digital": on_label if initial else off_label}
        if source == "memory":
            extra.update(retentive=1 if retentive else 0)
        return self._add("D", name, source, description, read_only, security, extra, address)

    def add_string(self, name, source="memory", description="", length=82, initial="", read_only=False,
                   security="*", address=None, retentive=False):
        extra = {"length": length}
        if source == "memory":
            extra.update(initial_string=initial or None, retentive=1 if retentive else 0)
        return self._add("S", name, source, description, read_only, security, extra, address)

    # -- I/O ----------------------------------------------------------------
    def to_text(self) -> str:
        buf = io.StringIO()
        buf.write(HEADER + "\r\n" + VERSION + "\r\n\r\n;Folders Section (Must define folders before tags)\r\n")
        for f in self.folders:
            buf.write(_row(["F", f, None, "F"]) + "\r\n")
        buf.write("\r\n;Tag Section\r\n")
        for t in self.tags.values():
            buf.write(_row(t.row()) + "\r\n")
        return buf.getvalue()

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_bytes(b"\xef\xbb\xbf" + self.to_text().encode("utf-8"))
        return p

    @classmethod
    def read(cls, path: str | Path) -> "TagDB":
        raw = Path(path).read_bytes()
        text = raw.decode("utf-8-sig") if not raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-16")
        db = cls()
        if VERSION.split(" -")[0] not in text:
            raise TagError("missing ';###002' version line - not a View tag CSV")
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith(";"):
                continue
            rec = _parse_line(line)
            kind = rec[0].strip()
            if kind == "F":
                db.folders.append(rec[1])
                continue
            vals = {c: (rec[i] if i < len(rec) and rec[i] != "" else None) for i, c in enumerate(COLS)}
            db.tags[rec[1]] = Tag(kind, rec[1], {k: v for k, v in vals.items() if k not in ("type", "name")})
        return db

    # -- checks -------------------------------------------------------------
    def validate(self, plc=None) -> list[dict]:
        out = []
        for t in self.tags.values():
            if t.folder() and t.folder() not in self.folders:
                out.append({"severity": "error", "tag": t.name, "message": f"folder {t.folder()!r} not defined"})
            if t.type == "A" and t.values.get("source") == "M":
                try:
                    lo, hi, ini = (float(t.values.get(k) or 0) for k in ("min", "max", "initial_analog"))
                    if lo > hi:
                        out.append({"severity": "error", "tag": t.name, "message": "min > max"})
                    elif not lo <= ini <= hi:
                        out.append({"severity": "warning", "tag": t.name, "message": "initial outside min..max"})
                except ValueError:
                    out.append({"severity": "error", "tag": t.name, "message": "non-numeric min/max/initial"})
            if t.type == "D":
                labels = {t.values.get("off_label"), t.values.get("on_label")}
                if t.values.get("initial_digital") not in labels:
                    out.append({"severity": "error", "tag": t.name,
                                "message": "initial digital value must equal the On or Off label (View rejects it)"})
            if t.type == "A" and t.values.get("initial_analog") in (None, ""):
                out.append({"severity": "error", "tag": t.name, "message": "analog tag needs an initial value"})
            addr = t.values.get("address")
            if t.values.get("source") == "D" and plc is not None and addr:
                m = re.match(r"^/?(?:[^:\[]*::)?\[([^\]]+)\](.+)$", addr)
                if m:
                    res = plc.resolve(m.group(2))
                    if res["status"] not in ("ok", "unverified"):
                        out.append({"severity": "error", "tag": t.name,
                                    "message": f"address {addr}: {res.get('detail', res['status'])}"})
        return out


def _parse_line(line: str) -> list:
    """Split a View CSV line; quoted fields stay strings, unquoted numbers become numbers."""
    out, i, n = [], 0, len(line)
    while i <= n:
        if i < n and line[i] == '"':
            j, buf = i + 1, []
            while j < n:
                if line[j] == '"' and j + 1 < n and line[j + 1] == '"':
                    buf.append('"')
                    j += 2
                elif line[j] == '"':
                    break
                else:
                    buf.append(line[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
            if i < n and line[i] == ",":
                i += 1
            elif i >= n:
                break
        else:
            j = line.find(",", i)
            tok = line[i:] if j < 0 else line[i:j]
            tok = tok.strip()
            if tok == "":
                out.append("")
            elif re.fullmatch(r"-?\d+", tok):
                out.append(int(tok))
            elif re.fullmatch(r"-?\d*\.\d+([eE][-+]?\d+)?|-?\d+[eE][-+]?\d+", tok):
                out.append(float(tok))
            else:
                out.append(tok)
            if j < 0:
                break
            i = j + 1
            if i == n:
                out.append("")
                break
    return out


def _row(values) -> str:
    out = []
    for v in values:
        if v is None or v == "":
            out.append("")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(str(int(v)) if float(v).is_integer() else repr(float(v)))
        else:
            out.append('"' + str(v).replace('"', '""') + '"')
    # View writes tag rows through the Address column (23 fields) and folder rows as 4 fields
    keep = 4 if values and values[0] == "F" else 23
    while len(out) > keep and out[-1] == "":
        out.pop()
    return ",".join(out)


def _n(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f
    except ValueError:
        return str(v).strip().lower() if isinstance(v, str) else v


def compare(sent: TagDB, got: TagDB) -> dict:
    missing = [n for n in sent.tags if n not in got.tags]
    missing_folders = [f for f in sent.folders if f not in got.folders]
    diffs = []
    for n, t in sent.tags.items():
        g = got.tags.get(n)
        if g is None:
            continue
        if g.type != t.type:
            diffs.append({"tag": n, "field": "type", "sent": t.type, "exported": g.type})
        for k, v in t.values.items():
            if v is None:
                continue
            if _n(v) != _n(g.values.get(k)):
                diffs.append({"tag": n, "field": k, "sent": v, "exported": g.values.get(k)})
    return {"ok": not missing and not missing_folders and not diffs, "missing": missing,
            "missing_folders": missing_folders, "differences": diffs, "sent": len(sent.tags),
            "exported": len(got.tags)}
