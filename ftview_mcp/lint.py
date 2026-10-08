"""Static checks for a display export."""
from __future__ import annotations

from . import gfx as G
from .plc import L5X

import re

_PAREN_TAG = re.compile(r"^\s*\(+\s*\{[^{}]*\}\s*\)+\s*$")

INPUT_DISPLAY_TAGS ={"numericDisplay", "numericInput", "stringDisplay", "stringInput"}


def lint(doc: G.GfxDoc, displays: list[str] | None = None,
         plcs: dict[str, L5X] | None = None, default_plc: L5X | None = None) -> list[dict]:
    """Return findings ``{severity: error|warning|info, code, object, message}``.

    displays: known display names in the HMI project (enables navigation checks).
    plcs: shortcut -> L5X; default_plc is used for shortcuts not in ``plcs``.
    """
    out: list[dict] = []

    def add(sev, code, obj, msg):
        out.append({"severity": sev, "code": code, "object": obj, "message": msg})

    for e in doc.validate()[:50]:
        add("error", "SCHEMA", "", e)
    for n in doc.duplicate_names():
        add("error", "DUPLICATE_NAME", n,
            "name used more than once; View Studio skips the second object on import")

    # Rules enforced by View Studio's importer but not by the XSD (learned from sandbox imports).
    ds = doc.settings
    if ds is not None and ds.get("titleBar") == "false" and ds.get("titleBarText") is not None:
        add("error", "IMPORT_TITLEBARTEXT", "(display)",
            "titleBarText is set while titleBar is false; the importer rejects it")
    for obj, depth in doc.iter_objects():
        if depth > 0 and obj.get("wallpaper") is not None:
            add("error", "IMPORT_WALLPAPER_IN_GROUP", obj.get("name"),
                "'wallpaper' on an object inside a group; the importer rejects it")
        if obj.tag == "button" and obj.find("confirm[@confirmAction='true']") is not None:
            add("warning", "IMPORT_CONFIRM_DROPPED", obj.get("name"),
                "View v12's importer resets button confirmation to off; set it in View Studio after import")
        if obj.tag == "line" and (obj.get("lineWidth") or "1") not in ("1", "0"):
            add("warning", "IMPORT_LINE_WIDTH", obj.get("name"),
                "View resets <line> lineWidth to 1 on import; use a filled rectangle or a polyline")
        if obj.tag == "polyline" and len((obj.get("path") or "").split()) == 4 \
                and (obj.get("lineWidth") or "1") != "1":
            add("warning", "IMPORT_LINE_WIDTH", obj.get("name"),
                "a 2-point polyline becomes a <line> on import and loses its lineWidth")
    for el in doc.root.iter():
        if not isinstance(el.tag, str):
            continue
        e = el.get("expression")
        if e and _PAREN_TAG.match(e):
            owner = G._owner(el, doc)
            add("error", "IMPORT_PAREN_TAG_EXPR", owner.get("name") if owner is not None else "(display)",
                f"expression {e!r} is a single tag in parentheses; View's importer corrupts it to "
                f"'{{({{{{...}}}})}}' - remove the parentheses")

    w, h = doc.size
    for obj, depth in doc.iter_objects():
        name = obj.get("name")
        b = G.bbox(obj)
        if b is None:
            if obj.tag in G.CONTAINER_TAGS:
                add("warning", "EMPTY_GROUP", name, "group contains no objects")
            continue
        if depth == 0 and w and h and (b.right <= 0 or b.bottom <= 0 or b.left >= w or b.top >= h):
            add("warning", "OFF_SCREEN", name, f"entirely outside the {w}x{h} display at {b.as_list()}")
        elif depth == 0 and w and h and (b.left < 0 or b.top < 0 or b.right > w or b.bottom > h):
            add("info", "PARTLY_OFF_SCREEN", name, f"extends past the {w}x{h} display: {b.as_list()}")
        if obj.tag not in G.CONTAINER_TAGS and obj.tag not in ("line",) and (b.width <= 0 or b.height <= 0):
            add("info", "ZERO_SIZE", name, f"width/height is zero: {b.as_list()}")
        if obj.tag == "button":
            cmd = obj.find("command")
            if cmd is None or not any((cmd.get(a) or "").strip()
                                      for a in ("pressAction", "releaseAction", "repeatAction")):
                add("warning", "BUTTON_NO_ACTION", name, "button has no press/release/repeat action")
        if obj.tag in INPUT_DISPLAY_TAGS:
            conn = obj.find("connections/connection[@name='Value']")
            if conn is None or not (conn.get("expression") or "").strip():
                add("warning", "NO_VALUE_CONNECTION", name, f"{obj.tag} has no Value connection")

    known = {d.lower() for d in displays} if displays else None
    for c in G.commands(doc):
        if c["verb"] == "display":
            if not c["display"]:
                add("warning", "DISPLAY_NO_NAME", c["object"], f"'{c['command']}' has no display name")
            elif known is not None and c["display"].lower() not in known:
                add("error", "UNKNOWN_DISPLAY", c["object"],
                    f"'{c['command']}' opens display {c['display']!r}, which is not in the project")

    refs = G.tag_refs(doc)
    shortcuts = sorted({r.shortcut for r in refs if r.kind == "device"})
    if len(shortcuts) > 1:
        add("info", "MULTIPLE_SHORTCUTS", "", f"device shortcuts used: {shortcuts}")
    if plcs or default_plc:
        seen = set()
        for r in refs:
            if r.kind != "device":
                continue
            plc = (plcs or {}).get(r.shortcut) or (plcs or {}).get(r.shortcut.upper()) or default_plc
            if plc is None:
                continue
            key = (r.object, r.ref)
            if key in seen:
                continue
            seen.add(key)
            res = plc.resolve(r.tag)
            st = res["status"]
            if st == "ok":
                continue
            sev = "info" if st == "unverified" else "error"
            add(sev, f"TAG_{st.upper()}", r.object,
                f"{{{r.ref}}} in {r.element or r.attribute}: {res.get('detail', st)}")
    return out


def summarize(findings: list[dict]) -> dict:
    s = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        s[f["severity"]] += 1
    return s
