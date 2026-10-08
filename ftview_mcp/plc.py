"""Resolve HMI device-tag paths against a Logix L5X export.

Handles controller and program scope (``Program:Name.Tag``), UDT / AOI / built-in
structure members, array indexes with bounds checks, bit access on integers and
alias tags. I/O module tags (``Local:1:I.Data``) can only be checked as far as
the module name, so they come back as ``unverified``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from lxml import etree

INT_BITS = {"SINT": 8, "USINT": 8, "INT": 16, "UINT": 16, "DINT": 32, "UDINT": 32,
            "LINT": 64, "ULINT": 64}
ATOMIC = set(INT_BITS) | {"BOOL", "REAL", "LREAL", "BIT"}

# Members of commonly used built-in structures.
BUILTIN: dict[str, dict[str, tuple[str, int]]] = {
    "TIMER": {n: ("DINT", 0) for n in ("PRE", "ACC")} | {n: ("BOOL", 0) for n in ("EN", "TT", "DN")},
    "COUNTER": {n: ("DINT", 0) for n in ("PRE", "ACC")}
    | {n: ("BOOL", 0) for n in ("CU", "CD", "DN", "OV", "UN")},
    "CONTROL": {n: ("DINT", 0) for n in ("LEN", "POS")}
    | {n: ("BOOL", 0) for n in ("EN", "EU", "DN", "EM", "ER", "UL", "IN", "FD")},
    "STRING": {"LEN": ("DINT", 0), "DATA": ("SINT", 82)},
    "FBD_TIMER": {n: ("DINT", 0) for n in ("PRE", "ACC")}
    | {n: ("BOOL", 0) for n in ("EnableIn", "EnableOut", "TimerEnable", "EN", "TT", "DN")},
    "MESSAGE": {n: ("BOOL", 0) for n in ("EN", "EW", "ER", "DN", "ST", "TO", "EN_CC")}
    | {n: ("INT", 0) for n in ("ERR",)} | {n: ("DINT", 0) for n in ("EXERR", "UnconnectedTimeout",
                                                                    "ConnectionRate")},
}
OPAQUE = {"ALARM_DIGITAL", "ALARM_ANALOG", "PID", "PIDE_AUTOTUNE", "AXIS_CIP_DRIVE",
          "MOTION_GROUP", "COORDINATE_SYSTEM", "CAM", "CAM_PROFILE", "SERIAL_PORT_CONTROL"}


@dataclass
class Member:
    type: str
    dims: tuple[int, ...]


class L5X:
    def __init__(self, path: Path):
        self.path = path
        root = etree.parse(str(path), etree.XMLParser(huge_tree=True)).getroot()
        ctrl = root.find("Controller")
        if ctrl is None:
            raise ValueError(f"{path} has no <Controller> (not a full-project L5X)")
        self.controller = ctrl.get("Name")
        self.types: dict[str, dict[str, Member]] = {}
        for dt in ctrl.iterfind("DataTypes/DataType"):
            self.types[dt.get("Name").upper()] = {
                m.get("Name").upper(): Member(m.get("DataType").upper(), _dims(m.get("Dimension")))
                for m in dt.iterfind("Members/Member")
                if not (m.get("Hidden") == "true")}
        for aoi in ctrl.iterfind("AddOnInstructionDefinitions/AddOnInstructionDefinition"):
            mem = {}
            for p in aoi.iterfind("Parameters/Parameter"):
                mem[p.get("Name").upper()] = Member(p.get("DataType").upper(), _dims(p.get("Dimensions")))
            for t in aoi.iterfind("LocalTags/LocalTag"):
                mem[t.get("Name").upper()] = Member(t.get("DataType").upper(), _dims(t.get("Dimensions")))
            self.types[aoi.get("Name").upper()] = mem
        self.tags = {t.get("Name").upper(): t for t in ctrl.iterfind("Tags/Tag")}
        self.programs = {p.get("Name").upper(): {t.get("Name").upper(): t for t in p.iterfind("Tags/Tag")}
                         for p in ctrl.iterfind("Programs/Program")}
        self.modules = {m.get("Name").upper() for m in ctrl.iterfind("Modules/Module")}
        self.modules.add("LOCAL")

    # ------------------------------------------------------------------
    def resolve(self, path: str) -> dict:
        """Return {'status': ok|missing_tag|missing_member|bad_index|unverified, ...}."""
        p = path.strip()
        try:
            segs = _segments(p)
        except ValueError as e:
            return {"status": "syntax", "detail": str(e)}
        first, idx = segs[0]
        scope = self.tags
        where = "controller"
        if first.upper().startswith("PROGRAM:"):
            prog = first.split(":", 1)[1].upper()
            if prog not in self.programs:
                return {"status": "missing_tag", "detail": f"program {prog!r} not found"}
            if len(segs) < 2:
                return {"status": "syntax", "detail": "program scope without tag"}
            scope, where = self.programs[prog], f"program {prog}"
            segs = segs[1:]
            first, idx = segs[0]
        elif ":" in first:
            mod = first.split(":", 1)[0].upper()
            if mod in self.modules:
                return {"status": "unverified", "detail": f"I/O tag of module {mod}; members not checked"}
            return {"status": "missing_tag", "detail": f"module {mod!r} not in project"}
        tag = scope.get(first.upper())
        if tag is None:
            return {"status": "missing_tag", "detail": f"{first!r} not found in {where} tags"}
        if tag.get("TagType") == "Alias" and tag.get("AliasFor"):
            alias = tag.get("AliasFor")
            rest = p[p.upper().find(first.upper()) + len(first):]
            r = self.resolve(alias + rest)
            r["via_alias"] = alias
            return r
        typ = (tag.get("DataType") or "").upper()
        dims = _dims(tag.get("Dimensions"))
        return self._walk(typ, dims, idx, segs[1:], p)

    def _walk(self, typ, dims, idx, rest, full) -> dict:
        if idx:
            if not dims:
                return {"status": "bad_index", "detail": f"{typ} is not an array"}
            if len(idx) != len(dims):
                return {"status": "bad_index", "detail": f"expected {len(dims)} index(es)"}
            for i, d in zip(idx, dims):
                if i is not None and not (0 <= i < d):
                    return {"status": "bad_index", "detail": f"index {i} out of range 0..{d - 1}"}
            dims = ()
        if not rest:
            return {"status": "ok", "type": typ + ("[" + ",".join(map(str, dims)) + "]" if dims else "")}
        name, nidx = rest[0]
        if dims:
            # whole array then member, e.g. Arr.0 -> BOOL array bit addressing isn't valid in HMI
            return {"status": "bad_index", "detail": f"{typ} array needs an index before .{name}"}
        if typ in INT_BITS and name.isdigit() and not nidx and len(rest) == 1:
            bit = int(name)
            if bit < INT_BITS[typ]:
                return {"status": "ok", "type": "BOOL"}
            return {"status": "bad_index", "detail": f"bit {bit} out of range for {typ}"}
        if typ in OPAQUE:
            return {"status": "unverified", "detail": f"members of built-in {typ} not checked"}
        members = self.types.get(typ) or BUILTIN.get(typ)
        if members is None:
            if typ in ATOMIC:
                return {"status": "missing_member", "detail": f"{typ} has no member {name!r}"}
            return {"status": "unverified", "detail": f"type {typ} definition not in L5X"}
        m = members.get(name.upper())
        if m is None:
            return {"status": "missing_member", "detail": f"{typ} has no member {name!r}"}
        if isinstance(m, tuple):
            m = Member(m[0], (m[1],) if m[1] else ())
        return self._walk(m.type, m.dims, nidx, rest[1:], full)


def _dims(s: str | None) -> tuple[int, ...]:
    if not s:
        return ()
    vals = tuple(int(x) for x in re.split(r"[\s,]+", s.strip()) if x)
    return tuple(v for v in vals if v) if any(vals) else ()


_SEG = re.compile(r"([^.\[\]]+)(?:\[([^\]]+)\])?")


def _segments(path: str) -> list[tuple[str, list[int | None]]]:
    """'Program:P.Tag[1].Mem.3' -> [('Program:P',[]), ('Tag',[1]), ('Mem',[]), ('3',[])]"""
    out = []
    pos = 0
    if path.upper().startswith("PROGRAM:"):
        dot = path.find(".")
        if dot < 0:
            return [(path, [])]
        out.append((path[:dot], []))
        pos = dot + 1
    while pos < len(path):
        m = _SEG.match(path, pos)
        if not m:
            raise ValueError(f"cannot parse tag path at {path[pos:]!r}")
        name, ix = m.group(1), m.group(2)
        idx: list[int | None] = []
        if ix is not None:
            for x in ix.split(","):
                x = x.strip()
                idx.append(int(x) if re.fullmatch(r"\d+", x) else None)  # tag-indexed -> None
        out.append((name, idx))
        pos = m.end()
        if pos < len(path):
            if path[pos] != ".":
                raise ValueError(f"unexpected {path[pos]!r} in {path!r}")
            pos += 1
    return out


@lru_cache(maxsize=8)
def _load_cached(path: str, mtime: float) -> L5X:
    return L5X(Path(path))


def load(path: str | Path) -> L5X:
    p = Path(path).resolve()
    return _load_cached(str(p), p.stat().st_mtime)
