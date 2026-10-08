"""Display (.gfx) XML model: faithful load/save, object index, geometry, tag refs.

FactoryTalk View SE exports displays to XML through the Graphics Import/Export
wizard. This module edits those XML files. It never touches the binary .gfx.

Faithful round-trip rules (verified against View Studio v12 exports):
* keep the original bytes before ``<gfx`` (XML declaration) and after ``</gfx>``;
* View writes CRLF line endings;
* newlines inside attribute values are written as ``&#xA;`` (lxml emits ``&#10;``).
With these, load+save of an unmodified file is byte-identical (checked on 15
exports, including hand-edited ones with mixed line endings).
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from lxml import etree

from . import schema as _schema

NON_OBJECT_TAGS = {"displaySettings", "vbaProject"}
CONTAINER_TAGS = {"group"}
POINT_ATTRS = ("path", "line", "polygon")  # whitespace-separated "x y x y ..."
COMMAND_ATTRS = ("pressAction", "releaseAction", "repeatAction", "startupCommand",
                 "shutdownCommand", "command", "action")

_PARSER = etree.XMLParser(remove_blank_text=False, huge_tree=True, resolve_entities=False,
                          strip_cdata=False)
_CHARREF_FIX = ((b"&#10;", b"&#xA;"), (b"&#13;", b"&#xD;"), (b"&#9;", b"&#x9;"))
_XSI = "{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation"
_NEW_DECL = b'<?xml version="1.0" encoding="UTF-8"?>\r\n'


class GfxError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


class GfxDoc:
    def __init__(self, raw: bytes, path: Path | None = None):
        self.path = path
        self.raw = raw
        try:
            self.root = etree.fromstring(raw, _PARSER)
        except etree.XMLSyntaxError as e:
            raise GfxError(f"not well-formed XML: {e}") from e
        if self.root.tag != "gfx":
            raise GfxError(f"root element is <{self.root.tag}>, expected <gfx> (display export)")
        start = raw.find(b"<gfx")
        end = raw.rfind(b"</gfx>")
        self._prefix = raw[:start] if start >= 0 else _NEW_DECL
        self._suffix = raw[end + len(b"</gfx>"):] if end >= 0 else b"\r\n"
        self._crlf = b"\r\n" in raw

    # -- construction ------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "GfxDoc":
        p = Path(path)
        return cls(p.read_bytes(), p)

    @classmethod
    def new(cls, width: int = 1920, height: int = 1080, schema: str = "Gfx-SE12.xsd",
            **display_settings: str) -> "GfxDoc":
        ds = {"displayType": "replace", "position": "useCurrentPosition", "securityCode": "*",
              "backColor": "#DDDDDD", "titleBar": "false", "size": "specifySizeInPixels",
              "width": str(width), "height": str(height)}
        ds.update({k: str(v) for k, v in display_settings.items()})
        if ds.get("titleBar", "false").lower() != "true":
            ds.pop("titleBarText", None)  # importer: "'titleBarText' cannot be set while 'titleBar' is 'false'"
        attrs = " ".join(f'{k}="{_xml_attr(v)}"' for k, v in ds.items())
        text = (f'<?xml version="1.0" encoding="UTF-8"?>\r\n'
                f'<gfx xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                f'xsi:noNamespaceSchemaLocation="{schema}">\r\n'
                f'    <displaySettings {attrs}/>\r\n</gfx>\r\n')
        return cls(text.encode("utf-8"))

    # -- serialization -----------------------------------------------------
    def to_bytes(self) -> bytes:
        body = etree.tostring(self.root, encoding="UTF-8", xml_declaration=False)
        for a, b in _CHARREF_FIX:
            body = body.replace(a, b)
        if self._crlf:
            body = body.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        return self._prefix + body + self._suffix

    def sha1(self) -> str:
        return hashlib.sha1(self.to_bytes()).hexdigest()

    # -- schema ------------------------------------------------------------
    @property
    def schema_name(self) -> str:
        loc = self.root.get(_XSI) or _schema.DEFAULT_SCHEMA
        return Path(loc).name

    def validate(self) -> list[str]:
        name = self.schema_name
        if not (_schema.SCHEMA_DIR / name).is_file():
            name = _schema.DEFAULT_SCHEMA
        return _schema.validate(self.root, name)

    # -- objects -----------------------------------------------------------
    @property
    def settings(self) -> etree._Element | None:
        return self.root.find("displaySettings")

    @property
    def size(self) -> tuple[int, int]:
        ds = self.settings
        if ds is None:
            return (0, 0)
        return (int(float(ds.get("width", "0"))), int(float(ds.get("height", "0"))))

    def iter_objects(self, el: etree._Element | None = None, depth: int = 0
                     ) -> Iterator[tuple[etree._Element, int]]:
        el = self.root if el is None else el
        for child in el:
            if is_object(child):
                yield child, depth
                if child.tag in CONTAINER_TAGS:
                    yield from self.iter_objects(child, depth + 1)

    def index(self) -> dict[str, etree._Element]:
        return {o.get("name"): o for o, _ in self.iter_objects()}

    def duplicate_names(self) -> list[str]:
        seen: dict[str, int] = {}
        for o, _ in self.iter_objects():
            seen[o.get("name")] = seen.get(o.get("name"), 0) + 1
        return sorted(n for n, c in seen.items() if c > 1)

    def get(self, name: str) -> etree._Element:
        for o, _ in self.iter_objects():
            if o.get("name") == name:
                return o
        raise GfxError(f"no object named {name!r}")

    def top_level(self, el: etree._Element) -> etree._Element:
        while el.getparent() is not None and el.getparent() is not self.root:
            el = el.getparent()
        return el

    # -- editing -----------------------------------------------------------
    def unique_name(self, wanted: str, taken: set[str]) -> str:
        if wanted not in taken:
            return wanted
        base = re.sub(r"\d+$", "", wanted) or "Object"
        n = 1
        while f"{base}{n}" in taken:
            n += 1
        return f"{base}{n}"

    def insert(self, fragment: str | etree._Element, parent: str | None = None,
               before: str | None = None) -> list[tuple[str, str]]:
        """Insert object element(s). Colliding names are renamed (FTView silently
        skips objects whose name already exists on the display). Returns
        ``[(requested_name, final_name), ...]`` for every object inserted."""
        if isinstance(fragment, str):
            try:
                wrap = etree.fromstring(f"<frag>{fragment}</frag>".encode("utf-8"), _PARSER)
            except etree.XMLSyntaxError as e:
                raise GfxError(f"fragment is not well-formed XML: {e}") from e
            new_els = [c for c in wrap if isinstance(c.tag, str)]
        else:
            new_els = [fragment]
        if not new_els:
            raise GfxError("fragment contains no elements")
        target = self.root if parent is None else self.get(parent)
        if parent is not None and target.tag not in CONTAINER_TAGS:
            raise GfxError(f"{parent!r} is a <{target.tag}>, only groups can contain objects")
        taken = set(self.index())
        renames: list[tuple[str, str]] = []
        for el in new_els:
            if el.tag in NON_OBJECT_TAGS:
                raise GfxError(f"<{el.tag}> cannot be inserted as an object")
            for sub in [el] + [d for d, _ in _iter_objects_in(el)]:
                want = sub.get("name") or sub.tag.capitalize()
                final = self.unique_name(want, taken)
                taken.add(final)
                sub.set("name", final)
                renames.append((want, final))
        anchor = None
        if before is not None:
            anchor = self.get(before)
            if anchor.getparent() is not target:
                raise GfxError(f"{before!r} is not a direct child of the insert target")
        elif target is self.root and self.root.find("vbaProject") is not None:
            anchor = self.root.find("vbaProject")
        depth = _depth(target) + 1
        for el in new_els:
            # importer rule: no 'wallpaper' on objects inside groups
            for d in el.iter():
                if (d is not el or target is not self.root) and d.get("name") is not None:
                    d.attrib.pop("wallpaper", None)
            _place(target, el, anchor, depth)
        return renames

    def delete(self, name: str) -> None:
        el = self.get(name)
        _remove_keep_ws(el)

    def move(self, name: str, dx: float, dy: float) -> None:
        shift(self.get(name), dx, dy)

    def set_attrs(self, name: str, attrs: dict[str, str | None]) -> dict[str, tuple]:
        el = self.get(name)
        changes = {}
        for k, v in attrs.items():
            old = el.get(k)
            if v is None:
                if k in el.attrib:
                    del el.attrib[k]
            else:
                el.set(k, str(v))
            changes[k] = (old, v)
        return changes


def is_object(el) -> bool:
    if not isinstance(el.tag, str) or el.tag in NON_OBJECT_TAGS or el.get("name") is None:
        return False
    parent = el.getparent()
    return parent is not None and (parent.tag == "gfx" or parent.tag in CONTAINER_TAGS)


def _iter_objects_in(el):
    for child in el:
        if isinstance(child.tag, str) and child.get("name") is not None and el.tag in CONTAINER_TAGS \
                and child.tag not in NON_OBJECT_TAGS:
            yield child, 0
            if child.tag in CONTAINER_TAGS:
                yield from _iter_objects_in(child)


def _depth(el) -> int:
    d = 0
    while el.getparent() is not None:
        el = el.getparent()
        d += 1
    return d


def _place(parent, el, anchor, depth: int) -> None:
    ind = "\n" + "    " * depth
    etree.indent(el, space="    ", level=depth)
    if anchor is not None:
        idx = parent.index(anchor)
        prev = anchor.getprevious()
        parent.insert(idx, el)
        el.tail = ind
        if prev is None:
            parent.text = ind
    else:
        children = list(parent)
        if children:
            last = children[-1]
            closing = last.tail
            last.tail = ind
            parent.append(el)
            el.tail = closing if closing is not None else "\n" + "    " * (depth - 1)
        else:
            parent.text = ind
            parent.append(el)
            el.tail = "\n" + "    " * (depth - 1)


def _remove_keep_ws(el) -> None:
    parent = el.getparent()
    prev = el.getprevious()
    if prev is not None:
        prev.tail = el.tail
    else:
        parent.text = el.tail
    parent.remove(el)


def _xml_attr(v: str) -> str:
    return (v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@dataclass
class BBox:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    def union(self, o: "BBox") -> "BBox":
        return BBox(min(self.left, o.left), min(self.top, o.top),
                    max(self.right, o.right), max(self.bottom, o.bottom))

    def intersects(self, o: "BBox") -> bool:
        return not (o.left >= self.right or o.right <= self.left or o.top >= self.bottom
                    or o.bottom <= self.top)

    def as_list(self):
        return [round(self.left), round(self.top), round(self.width), round(self.height)]


def bbox(el) -> BBox | None:
    if el.tag in CONTAINER_TAGS:
        box = None
        for child in el:
            if is_object(child):
                b = bbox(child)
                if b is not None:
                    box = b if box is None else box.union(b)
        return box
    try:
        left = float(el.get("left"))
        top = float(el.get("top"))
        w = float(el.get("width", "0"))
        h = float(el.get("height", "0"))
    except (TypeError, ValueError):
        return None
    return BBox(left, top, left + w, top + h)


def _shift_points(text: str, dx: float, dy: float) -> str:
    vals = text.split()
    out = []
    for i, v in enumerate(vals):
        n = float(v) + (dx if i % 2 == 0 else dy)
        out.append(_num(n))
    trailing = " " if text.endswith(" ") else ""
    return " ".join(out) + trailing


def _num(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def shift(el, dx: float, dy: float) -> None:
    """Move an object (and everything inside it) by dx, dy pixels (rounded: the schema
    requires integer point coordinates)."""
    dx, dy = round(dx), round(dy)
    for e in el.iter():
        if not isinstance(e.tag, str):
            continue
        if e.get("left") is not None and e.get("top") is not None:
            e.set("left", _num(float(e.get("left")) + dx))
            e.set("top", _num(float(e.get("top")) + dy))
        for a in POINT_ATTRS:
            if e.get(a):
                e.set(a, _shift_points(e.get(a), dx, dy))


def scale(el, sx: float, sy: float, ox: float = 0, oy: float = 0) -> None:
    """Scale an object about origin (ox, oy)."""
    def fx(x): return ox + (x - ox) * sx
    def fy(y): return oy + (y - oy) * sy
    for e in el.iter():
        if not isinstance(e.tag, str):
            continue
        if e.get("left") is not None and e.get("top") is not None:
            l, t = float(e.get("left")), float(e.get("top"))
            e.set("left", _num(round(fx(l))))
            e.set("top", _num(round(fy(t))))
            if e.get("width") is not None:
                e.set("width", _num(max(1, round(float(e.get("width")) * sx))))
            if e.get("height") is not None:
                e.set("height", _num(max(1, round(float(e.get("height")) * sy))))
        for a in POINT_ATTRS:
            if e.get(a):
                vals = e.get(a).split()
                pts = [_num(round(fx(float(v)) if i % 2 == 0 else fy(float(v))))
                       for i, v in enumerate(vals)]
                e.set(a, " ".join(pts) + (" " if e.get(a).endswith(" ") else ""))


# ---------------------------------------------------------------------------
# Tag references and commands
# ---------------------------------------------------------------------------

_BRACE = re.compile(r"\{([^{}]*)\}")
_DEVICE = re.compile(r"^(?:(?P<area>[^\[]*?)::)?\[(?P<shortcut>[^\]]+)\](?P<path>.+)$")
_PARAM = re.compile(r"^#\d+$")
_GUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


@dataclass
class TagRef:
    object: str          # top-level object name
    element: str         # element path inside the object, e.g. "numericDisplay/connections/connection[Value]"
    attribute: str
    ref: str             # text inside braces (or bare command argument)
    kind: str            # device | hmi | parameter | literal
    shortcut: str = ""
    tag: str = ""        # tag path without shortcut
    source: str = "expression"   # expression | command

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if v != ""}


def classify(ref: str) -> tuple[str, str, str]:
    r = ref.strip()
    if _PARAM.match(r):
        return "parameter", "", r
    m = _DEVICE.match(r)
    if m:
        return "device", m.group("shortcut"), m.group("path").strip()
    if r.startswith('"') or re.fullmatch(r"-?\d+(\.\d+)?", r):
        return "literal", "", r
    return "hmi", "", r


def _element_label(el, obj) -> str:
    parts = []
    e = el
    while e is not None and e is not obj:
        lab = e.tag
        if e.get("name") is not None and e.tag not in ("connection",):
            lab += f"[{e.get('name')}]"
        elif e.tag == "connection":
            lab += f"[{e.get('name')}]"
        parts.append(lab)
        e = e.getparent()
    return "/".join(reversed(parts))


def tag_refs(doc: GfxDoc) -> list[TagRef]:
    out: list[TagRef] = []
    for el in doc.root.iter():
        if not isinstance(el.tag, str):
            continue
        obj = _owner(el, doc)
        oname = obj.get("name") if obj is not None else "(display)"
        for attr, val in el.attrib.items():
            if not val:
                continue
            label = _element_label(el, obj) if obj is not None else el.tag
            if attr in COMMAND_ATTRS or attr.endswith("Command"):
                for cmd in parse_commands(val):
                    for t in cmd.tags:
                        kind, sc, path = classify(t.strip("{}"))
                        out.append(TagRef(oname, label, attr, t, kind, sc, path, "command"))
                continue
            if attr in ("classId", "typeLibrary"):
                continue
            for m in _BRACE.finditer(val):
                if _GUID.match(m.group(1)):
                    continue
                kind, sc, path = classify(m.group(1))
                out.append(TagRef(oname, label, attr, m.group(1), kind, sc, path))
            if el.tag == "connection" and attr == "expression" and "{" not in val:
                # Some connections hold a bare tag name rather than an expression.
                kind, sc, path = classify(val)
                if kind in ("device", "hmi") and re.fullmatch(r"[\w:\[\]\\/.#]+", val.strip()):
                    out.append(TagRef(oname, label, attr, val, kind, sc, path))
    return out


def _owner(el, doc):
    e = el
    while e is not None and e is not doc.root:
        if is_object(e):
            # report innermost named object
            return e
        e = e.getparent()
    return None


@dataclass
class Command:
    text: str
    verb: str
    args: list[str]
    tags: list[str] = field(default_factory=list)
    display: str | None = None


_TAG_VERBS = {"set": 1, "toggle": 1, "ramp": 1, "momentaryon": 1, "momentaryoff": 1,
              "&set": 1}


def _split_cmds(text: str) -> list[str]:
    parts, buf, q = [], [], False
    for ch in text:
        if ch == '"':
            q = not q
        if ch in ";\n\r" and not q:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _tokens(text: str) -> list[str]:
    return [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', text)]


def parse_commands(text: str) -> list[Command]:
    cmds = []
    for raw in _split_cmds(text):
        toks = _tokens(raw)
        if not toks:
            continue
        verb = toks[0].lstrip("&").lower()
        args = toks[1:]
        c = Command(raw, verb, args)
        if verb in ("display", "abort", "pullforward", "pushback", "printdisplay"):
            names = [a for a in args if not a.startswith("/")]
            c.display = names[0] if names else None
            for a in args:
                if a[:2].upper() == "/T" and len(a) > 2:
                    c.tags.extend(x for x in re.split(r",", a[2:]) if x)
        elif verb in _TAG_VERBS and args:
            c.tags.append(args[0])
        elif verb == "=" and args:
            c.tags.append(args[0])
        for a in args:
            c.tags.extend(m.group(0) for m in _BRACE.finditer(a) if m.group(0) not in c.tags)
        cmds.append(c)
    return cmds


def commands(doc: GfxDoc) -> list[dict]:
    out = []
    for el in doc.root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr, val in el.attrib.items():
            if val and (attr in COMMAND_ATTRS or attr.endswith("Command")):
                obj = _owner(el, doc)
                for c in parse_commands(val):
                    out.append({"object": obj.get("name") if obj is not None else "(display)",
                                "attribute": attr, "command": c.text, "verb": c.verb,
                                "display": c.display, "tags": c.tags})
    return out


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _own_signature(el) -> tuple[dict, str]:
    """Attributes + serialized non-object children (animations, captions, connections)."""
    c = copy.deepcopy(el)
    for child in list(c):
        if isinstance(child.tag, str) and child.get("name") is not None and c.tag in CONTAINER_TAGS \
                and child.tag not in NON_OBJECT_TAGS:
            c.remove(child)
    attrs = dict(c.attrib)
    c.attrib.clear()
    for e in c.iter():
        e.tail = None
        if e.text and not e.text.strip():
            e.text = None
    return attrs, etree.tostring(c, encoding="unicode")


def diff(a: GfxDoc, b: GfxDoc) -> dict:
    ia, ib = a.index(), b.index()
    added = sorted(set(ib) - set(ia))
    removed = sorted(set(ia) - set(ib))
    changed = []
    for name in sorted(set(ia) & set(ib)):
        ea, eb = ia[name], ib[name]
        aa, ca = _own_signature(ea)
        ab, cb = _own_signature(eb)
        if ea.tag != eb.tag:
            changed.append({"name": name, "type": f"{ea.tag} -> {eb.tag}"})
            continue
        attr_changes = {k: [aa.get(k), ab.get(k)] for k in set(aa) | set(ab) if aa.get(k) != ab.get(k)}
        if attr_changes or ca != cb:
            entry = {"name": name, "type": ea.tag}
            if attr_changes:
                entry["attributes"] = attr_changes
            if ca != cb:
                entry["content_changed"] = True
            changed.append(entry)
    sa, sb = a.settings, b.settings
    settings = {}
    if sa is not None and sb is not None:
        settings = {k: [sa.get(k), sb.get(k)] for k in set(sa.attrib) | set(sb.attrib)
                    if sa.get(k) != sb.get(k)}
    return {"added": added, "removed": removed, "changed": changed, "displaySettings": settings}
