"""XSD validation against the Gfx schemas that ship with FactoryTalk View.

Rockwell's Gfx-*.xsd files use ``\\xC0``-style escapes inside ``xs:pattern``
facets. Those are not legal XSD regex syntax, so libxml2 refuses to compile
the schema as shipped. We load a patched copy (escapes replaced by the literal
characters) and cache the compiled schema per version. The installed files are
never modified.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from lxml import etree

SCHEMA_DIR = Path(
    os.environ.get(
        "FTVIEW_SCHEMA_DIR",
        r"C:\Program Files (x86)\Rockwell Software\RSView Enterprise\Schemas",
    )
)
DEFAULT_SCHEMA = "Gfx-SE12.xsd"

_HEX_ESCAPE = re.compile(r"\\x([0-9A-Fa-f]{2})")


@lru_cache(maxsize=None)
def load_schema(name: str = DEFAULT_SCHEMA) -> etree.XMLSchema:
    """Compile (and cache) a Gfx schema by file name, e.g. ``Gfx-SE12.xsd``."""
    path = SCHEMA_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"schema not found: {path}")
    text = path.read_text(encoding="utf-8")
    text = _HEX_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return etree.XMLSchema(etree.fromstring(text.encode("utf-8")))


XS = "{http://www.w3.org/2001/XMLSchema}"


class SchemaInfo:
    """Introspection of a Gfx schema: which attributes/children an element accepts."""

    def __init__(self, name: str = DEFAULT_SCHEMA):
        text = (SCHEMA_DIR / name).read_text(encoding="utf-8")
        text = _HEX_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
        self.root = etree.fromstring(text.encode("utf-8"))
        g = lambda tag: {e.get("name"): e for e in self.root.findall(XS + tag)}  # noqa: E731
        self.ctypes = g("complexType")
        self.stypes = g("simpleType")
        self.groups = g("group")
        self.agroups = g("attributeGroup")
        self.elements = g("element")
        self.gattrs = g("attribute")

    # -- simple types ------------------------------------------------------
    def simple(self, type_name: str | None, node=None) -> dict:
        st = node if node is not None else self.stypes.get(type_name or "")
        if st is None:
            return {"type": type_name}
        r = st.find(XS + "restriction")
        if r is not None:
            enums = [e.get("value") for e in r.findall(XS + "enumeration")]
            if enums:
                return {"type": type_name or "enum", "enum": enums}
            out = {"type": type_name or r.get("base"), "base": r.get("base")}
            for facet in ("pattern", "minInclusive", "maxInclusive", "minLength", "maxLength"):
                f = r.find(XS + facet)
                if f is not None:
                    out[facet] = f.get("value")
            return out
        u = st.find(XS + "union")
        if u is not None:
            return {"type": type_name, "union": (u.get("memberTypes") or "").split()}
        return {"type": type_name}

    # -- complex types -----------------------------------------------------
    def _attrs(self, node, out: dict):
        for a in node.iterchildren(XS + "attribute", XS + "attributeGroup"):
            if a.tag == XS + "attributeGroup":
                ag = self.agroups.get(a.get("ref"))
                if ag is not None:
                    self._attrs(ag, out)
                continue
            use = a.get("use")
            if a.get("ref"):
                g = self.gattrs.get(a.get("ref"))
                if g is None:
                    continue
                a, use = g, use or g.get("use")
            inline = a.find(XS + "simpleType")
            info = self.simple(a.get("type"), inline) if inline is not None or a.get("type") in self.stypes \
                else {"type": a.get("type")}
            if use == "required":
                info["required"] = True
            if a.get("default") is not None:
                info["default"] = a.get("default")
            out[a.get("name")] = info

    def _children(self, node, out: list, minimum="1", maximum="1"):
        for c in node:
            if c.tag == XS + "element":
                mx = c.get("maxOccurs", "1")
                out.append({"name": c.get("name") or c.get("ref"),
                            "type": c.get("type") or "(inline)",
                            "min": c.get("minOccurs", minimum),
                            "max": "unbounded" if maximum == "unbounded" else mx})
            elif c.tag in (XS + "sequence", XS + "choice", XS + "all"):
                mx = "unbounded" if "unbounded" in (maximum, c.get("maxOccurs")) else "1"
                self._children(c, out, "0" if c.tag == XS + "choice" else c.get("minOccurs", minimum), mx)
            elif c.tag == XS + "group":
                grp = self.groups.get(c.get("ref"))
                if grp is not None:
                    self._children(grp, out, c.get("minOccurs", minimum),
                                   "unbounded" if "unbounded" in (maximum, c.get("maxOccurs")) else maximum)

    def _complex(self, ct) -> dict:
        attrs: dict = {}
        children: list = []
        bases = []
        node = ct
        cc = ct.find(XS + "complexContent")
        if cc is None:
            cc = ct.find(XS + "simpleContent")
        if cc is not None:
            ext = cc.find(XS + "extension")
            if ext is None:
                ext = cc.find(XS + "restriction")
            base = ext.get("base")
            if base in self.ctypes:
                b = self._complex(self.ctypes[base])
                attrs.update(b["attributes"])
                children.extend(b["children"])
                bases = [base] + b["bases"]
            node = ext
        self._attrs(node, attrs)
        self._children(node, children)
        return {"attributes": attrs, "children": children, "bases": bases}

    def _element_type_node(self, el):
        if el.get("type"):
            return self.ctypes.get(el.get("type"))
        return el.find(XS + "complexType")

    def _child_element(self, type_node, name):
        for el in type_node.iter(XS + "element"):
            if el.get("name") == name:
                return el
        # look through extension bases and group refs
        for ref in type_node.iter(XS + "extension", XS + "group"):
            tgt = self.ctypes.get(ref.get("base")) if ref.tag == XS + "extension" else self.groups.get(ref.get("ref"))
            if tgt is not None:
                found = self._child_element(tgt, name)
                if found is not None:
                    return found
        return None

    def describe(self, path: str) -> dict:
        """Describe an element by path from the root, e.g. 'gfx/button/command',
        'gfx/rectangle/animations/animateFill'. A bare type name (e.g. 'buttonType')
        describes that complexType."""
        if path in self.ctypes:
            d = self._complex(self.ctypes[path])
            d["type"] = path
            return d
        parts = [p for p in path.strip("/").split("/") if p]
        if parts and parts[0] != "gfx":
            parts = ["gfx"] + parts
        el = self.elements.get("gfx")
        for p in parts[1:]:
            tn = self._element_type_node(el)
            nxt = self._child_element(tn, p) if tn is not None else None
            if nxt is None:
                raise KeyError(f"<{p}> is not a child of <{el.get('name')}> in the schema")
            el = nxt
        tn = self._element_type_node(el)
        if tn is None:
            return {"element": el.get("name"), "type": el.get("type"), **self.simple(el.get("type"))}
        d = self._complex(tn)
        d["element"] = el.get("name")
        d["type"] = el.get("type") or "(inline)"
        return d


@lru_cache(maxsize=None)
def info(name: str = DEFAULT_SCHEMA) -> SchemaInfo:
    return SchemaInfo(name)


def available_schemas() -> list[str]:
    return sorted(p.name for p in SCHEMA_DIR.glob("Gfx-*.xsd"))


def validate(root: etree._Element, schema_name: str | None = None) -> list[str]:
    """Validate a parsed ``<gfx>`` root. Returns a list of error strings (empty = valid)."""
    schema = load_schema(schema_name or DEFAULT_SCHEMA)
    if schema.validate(root):
        return []
    return [f"line {e.line}: {e.message}" for e in schema.error_log]
