"""Parameter files (.par) and macros (.mcr): UTF-16 LE with BOM, CRLF, '!' comments.

Parameter file:  first line "@!=1" (as View writes it), then "#n = tag" lines; used with
                 Display "X" /P<file>. Macro: one FactoryTalk View command per line.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gfx as G

_PARAM = re.compile(r"^\s*(#\d+)\s*=\s*(.*?)\s*$")
KNOWN_VERBS = {
    "display", "abort", "set", "toggle", "ramp", "momentaryon", "momentaryoff", "=", "pullforward", "pushback",
    "printdisplay", "datalogon", "datalogoff", "datalogswitch", "datalogsnapshot", "datalogmergetoprimary",
    "alarmon", "alarmoff", "eventon", "eventoff", "acknowledge", "acknowledgeall", "silence", "silenceall",
    "derivedon", "derivedoff", "invoke", "login", "logout", "loginpopup", "pause", "remark", "displayclient",
    "appactivate", "appabort", "appstart", "close", "closeall", "position", "parameter", "play", "noaction",
    "menu", "next", "prev", "recipe", "refresh", "replace", "screenprint", "securitycode", "setfocus",
    "sendkeys", "upload", "uploadall", "download", "downloadall", "help", "language", "logoff", "execute",
    "abortall", "ftaecommand", "logixunlock", "logixlock", "displayheader", "historicaltrend"}


def _read_text(path: Path) -> str:
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _write_text(path: Path, lines: list[str]) -> Path:
    p = Path(path)
    p.write_bytes(b"\xff\xfe" + ("\r\n".join(lines) + "\r\n").encode("utf-16-le"))
    return p


@dataclass
class ParameterFile:
    params: dict[str, str] = field(default_factory=dict)
    comments: dict[str, str] = field(default_factory=dict)

    @classmethod
    def read(cls, path) -> "ParameterFile":
        pf, pending = cls(), []
        for line in _read_text(path).splitlines():
            s = line.strip()
            if s.startswith("@!="):
                continue
            if s.startswith("!"):
                pending.append(s[1:].strip())
                continue
            m = _PARAM.match(line)
            if m:
                pf.params[m.group(1)] = m.group(2)
                if pending and not pending[-1].startswith("="):
                    pf.comments[m.group(1)] = pending[-1]
                pending = []
        return pf

    def write(self, path, title: str = "") -> Path:
        lines = ["@!=1", f"!===== Parameter File created {dt.date.today():%m/%d/%Y} by ftview-mcp " + "=" * 20,
                 "! Passed to a display with: Display \"<name>\" /P" + Path(path).stem]
        if title:
            lines.append(f"! {title}")
        lines.append("!" + "=" * 60)
        for k in sorted(self.params, key=lambda x: int(x[1:])):
            lines.append("")
            if k in self.comments:
                lines.append(f"! {self.comments[k]}")
            lines.append(f"{k} = {self.params[k]}")
        return _write_text(path, lines)

    def validate(self, plc=None) -> list[dict]:
        out = []
        for k, v in self.params.items():
            if not v:
                out.append({"severity": "error", "param": k, "message": "empty replacement"})
                continue
            kind, sc, tag = G.classify(v.strip("{}"))
            if plc is not None and kind == "device":
                r = plc.resolve(tag)
                if r["status"] not in ("ok", "unverified"):
                    out.append({"severity": "error", "param": k, "message": f"{v}: {r.get('detail', r['status'])}"})
        nums = sorted(int(k[1:]) for k in self.params)
        if nums and nums != list(range(1, nums[-1] + 1)):
            out.append({"severity": "info", "param": "", "message": f"gaps in parameter numbering: {nums}"})
        return out


@dataclass
class Macro:
    commands: list[str] = field(default_factory=list)

    @classmethod
    def read(cls, path) -> "Macro":
        return cls([ln.strip() for ln in _read_text(path).splitlines() if ln.strip() and not ln.strip().startswith("!")])

    def write(self, path, title: str = "") -> Path:
        lines = [f"!===== Macro File created  {dt.date.today():%m/%d/%Y} by ftview-mcp " + "=" * 20,
                 "! Macros are lists of commands, with one command per line"]
        if title:
            lines.append(f"! {title}")
        lines.append("!" + "=" * 60)
        lines += self.commands
        return _write_text(path, lines)

    def validate(self, displays: list[str] | None = None, plc=None) -> list[dict]:
        out = []
        known = {d.lower() for d in displays} if displays else None
        for i, c in enumerate(self.commands, 1):
            for cmd in G.parse_commands(c):
                if cmd.verb not in KNOWN_VERBS:
                    out.append({"severity": "warning", "line": i, "message": f"unknown command '{cmd.verb}'"})
                if cmd.verb == "display" and known is not None and cmd.display and cmd.display.lower() not in known:
                    out.append({"severity": "error", "line": i, "message": f"display {cmd.display!r} not in project"})
                if plc is not None:
                    for t in cmd.tags:
                        kind, sc, tag = G.classify(t.strip("{}"))
                        if kind == "device":
                            r = plc.resolve(tag)
                            if r["status"] not in ("ok", "unverified"):
                                out.append({"severity": "error", "line": i,
                                            "message": f"{t}: {r.get('detail', r['status'])}"})
        return out
