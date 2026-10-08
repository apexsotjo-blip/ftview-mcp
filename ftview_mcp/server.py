"""FactoryTalk View SE MCP server (stdio).

Works on display XML exported by View Studio (Graphics Import/Export wizard).
Edits never touch the live HMI project: every edit writes a new XML that is
schema-validated against Rockwell's Gfx-SE*.xsd before it is saved, and the
user imports it with View Studio. See README.md for the workflow.
"""
from __future__ import annotations

import copy
import functools
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image
from lxml import etree

from . import gfx as G
from . import lint as L
from . import plc as P
from . import project as PR
from . import render as R

mcp = FastMCP("ftview-mcp")
_LOG = logging.getLogger("ftview_mcp")


def _tool(fn):
    """Register a tool and turn expected failures into readable ToolErrors."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except ToolError:
            raise
        except G.GfxError as e:
            raise ToolError(f"[GFX] {e}") from e
        except FileExistsError as e:
            raise ToolError(f"[EXISTS] {e}") from e
        except FileNotFoundError as e:
            raise ToolError(f"[NOT FOUND] {e}") from e
        except PermissionError as e:
            raise ToolError(f"[REFUSED] {e}") from e
        except (ValueError, KeyError) as e:
            raise ToolError(f"[INVALID] {e}") from e
    return mcp.tool(wrapper)


def _load(path: str) -> G.GfxDoc:
    p = Path(path).expanduser()
    if p.suffix.lower() == ".gfx":
        raise ToolError("[REFUSED] .gfx is View's binary format. Export the display to XML in View Studio "
                        "(Displays > right-click > Import and Export..., or Tools > Graphics Import Export "
                        "Wizard) and pass the .xml.")
    return G.GfxDoc.load(p)


def _save(doc: G.GfxDoc, src: G.GfxDoc | None, out_path: str, overwrite: bool) -> dict:
    res = PR.safe_write(doc, out_path, overwrite, baseline=src)
    if src is not None:
        d = G.diff(src, doc)
        res["diff"] = {"added": d["added"][:50], "removed": d["removed"][:50],
                       "changed": [c["name"] for c in d["changed"]][:100],
                       "counts": {k: len(d[k]) for k in ("added", "removed", "changed")}}
    return res


def _obj_summary(el, depth) -> dict:
    b = G.bbox(el)
    d: dict[str, Any] = {"name": el.get("name"), "type": el.tag, "depth": depth}
    if b:
        d["box"] = b.as_list()
    if el.tag == "group":
        d["children"] = sum(1 for c in el if G.is_object(c))
    cap = el.get("caption") or (el.find("up/caption").get("caption") if el.find("up/caption") is not None else None)
    if cap:
        d["caption"] = cap[:60]
    if el.find("animations") is not None:
        d["animations"] = [a.tag for a in el.find("animations")]
    return d


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------

@_tool
def ftv_list_projects() -> list[dict]:
    """List FactoryTalk View SE HMI projects on this PC (read-only)."""
    return PR.list_projects()


@_tool
def ftv_project_contents(project: str) -> dict:
    """List displays, global objects, images, parameter files and macros in an SE HMI project.
    `project` is a project name (e.g. MyPlant) or an absolute project folder. Read-only."""
    return PR.project_contents(project)


# ---------------------------------------------------------------------------
# Reading displays
# ---------------------------------------------------------------------------

@_tool
def ftv_display_info(xml_path: str) -> dict:
    """Summary of a display XML: size, settings, object counts by type, tag shortcuts, schema validity."""
    doc = _load(xml_path)
    counts: dict[str, int] = {}
    for o, _ in doc.iter_objects():
        counts[o.tag] = counts.get(o.tag, 0) + 1
    refs = G.tag_refs(doc)
    errs = doc.validate()
    return {
        "path": str(doc.path), "schema": doc.schema_name, "size": list(doc.size),
        "settings": dict(doc.settings.attrib) if doc.settings is not None else {},
        "objects_total": sum(counts.values()),
        "top_level": sum(1 for o in doc.root if G.is_object(o)),
        "by_type": dict(sorted(counts.items(), key=lambda x: -x[1])),
        "tag_refs": len(refs), "unique_tags": len({r.ref for r in refs}),
        "shortcuts": sorted({r.shortcut for r in refs if r.kind == "device"}),
        "has_vba": doc.root.find("vbaProject") is not None,
        "schema_valid": not errs, "schema_errors": errs[:10],
        "duplicate_names": doc.duplicate_names(),
    }


@_tool
def ftv_list_objects(xml_path: str, type: str | None = None, name_regex: str | None = None,
                     region: list[int] | None = None, top_level_only: bool = False,
                     tag_regex: str | None = None, limit: int = 300) -> dict:
    """List objects with name, type, box [left, top, width, height], caption, animations.
    Filters: type (e.g. 'button'), name_regex, region [left, top, width, height] (objects
    intersecting it), tag_regex (objects referencing a matching tag), top_level_only."""
    doc = _load(xml_path)
    rx = re.compile(name_regex, re.I) if name_regex else None
    trx = re.compile(tag_regex, re.I) if tag_regex else None
    tag_objs = None
    if trx:
        tag_objs = {r.object for r in G.tag_refs(doc) if trx.search(r.ref)}
    area = G.BBox(region[0], region[1], region[0] + region[2], region[1] + region[3]) if region else None
    out = []
    for o, depth in doc.iter_objects():
        if top_level_only and depth:
            continue
        if type and o.tag.lower() != type.lower():
            continue
        if rx and not rx.search(o.get("name")):
            continue
        if tag_objs is not None and o.get("name") not in tag_objs:
            continue
        if area:
            b = G.bbox(o)
            if b is None or not area.intersects(b):
                continue
        out.append(_obj_summary(o, depth))
    return {"count": len(out), "objects": out[:limit], "truncated": len(out) > limit}


@_tool
def ftv_get_object(xml_path: str, name: str, max_chars: int = 20000) -> str:
    """Return the raw XML of one object (including children)."""
    doc = _load(xml_path)
    el = doc.get(name)
    c = copy.deepcopy(el)
    c.tail = None
    etree.cleanup_namespaces(c)
    s = etree.tostring(c, encoding="unicode").replace(
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"', "")
    return s if len(s) <= max_chars else s[:max_chars] + f"\n... [truncated, {len(s)} chars]"


@_tool
def ftv_find_tags(xml_path: str, pattern: str | None = None, kind: str | None = None,
                  group_by_tag: bool = True) -> dict:
    """All tag references on a display (expressions, connections, animations, button commands).
    kind: device | hmi | parameter. pattern: regex on the reference text.
    group_by_tag=True returns {tag: [object/attribute uses]}."""
    doc = _load(xml_path)
    rx = re.compile(pattern, re.I) if pattern else None
    refs = [r for r in G.tag_refs(doc) if (not kind or r.kind == kind) and (not rx or rx.search(r.ref))]
    if not group_by_tag:
        return {"count": len(refs), "refs": [r.as_dict() for r in refs[:1000]]}
    grouped: dict[str, list[str]] = {}
    for r in refs:
        grouped.setdefault(r.ref, []).append(f"{r.object}:{r.element or ''}@{r.attribute}")
    return {"unique": len(grouped), "total": len(refs), "tags": dict(sorted(grouped.items()))}


@_tool
def ftv_find_commands(xml_path: str, verb: str | None = None) -> list[dict]:
    """Button/display commands (Display, Set, Toggle, Abort, ...) with target displays and tags."""
    doc = _load(xml_path)
    return [c for c in G.commands(doc) if not verb or c["verb"] == verb.lower()]


@_tool
def ftv_validate(xml_path: str) -> dict:
    """Validate a display XML against the installed Gfx-SE schema (named in the file)."""
    doc = _load(xml_path)
    errs = doc.validate()
    return {"valid": not errs, "schema": doc.schema_name, "errors": errs[:50],
            "duplicate_names": doc.duplicate_names(),
            "roundtrip_identical": doc.to_bytes() == doc.raw}


@_tool
def ftv_lint(xml_path: str, project: str | None = None, l5x: str | None = None,
             shortcut_l5x: dict[str, str] | None = None) -> dict:
    """Check a display: schema, duplicate names, off-screen/zero-size objects, buttons without
    actions, empty value connections, Display commands to displays not in `project`, and
    (with l5x / shortcut_l5x={'SCADA': 'path.L5X'}) every PLC tag reference against the
    Logix program, including UDT/AOI members, array bounds and bit numbers."""
    doc = _load(xml_path)
    displays = PR.display_names(project) if project else None
    plcs = {k: P.load(v) for k, v in (shortcut_l5x or {}).items()}
    default = P.load(l5x) if l5x else None
    findings = L.lint(doc, displays, plcs, default)
    return {"summary": L.summarize(findings), "findings": findings[:400],
            "truncated": len(findings) > 400}


@_tool
def ftv_crosscheck_plc(xml_paths: list[str], l5x: str, shortcut: str | None = None) -> dict:
    """Check the device-tag references of several displays against one Logix L5X.
    Returns per-tag status (ok / missing_tag / missing_member / bad_index / unverified)."""
    plc = P.load(l5x)
    result: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for x in xml_paths:
        doc = _load(x)
        for r in G.tag_refs(doc):
            if r.kind != "device" or (shortcut and r.shortcut.lower() != shortcut.lower()):
                continue
            e = result.get(r.ref)
            if e is None:
                e = result[r.ref] = plc.resolve(r.tag) | {"used_in": []}
                counts[e["status"]] = counts.get(e["status"], 0) + 1
            loc = f"{Path(x).stem}:{r.object}"
            if loc not in e["used_in"] and len(e["used_in"]) < 10:
                e["used_in"].append(loc)
    problems = {k: v for k, v in result.items() if v["status"] != "ok"}
    return {"controller": plc.controller, "counts": counts, "problems": problems}


@_tool
def ftv_render(xml_path: str, out_png: str | None = None, highlight: list[str] | None = None,
               show_tags: bool = False, region: list[int] | None = None, simulate: dict | None = None):
    """Render a preview PNG of a display (or a region [left, top, width, height]).
    Default is the design view (all objects, like View Studio). simulate={"values": {"P101_RUN": 1,
    "LIT101_OUT": 62.5}, "default": 0} evaluates visibility/colour/fill/position animations for
    that tag state (keys are bare tag names or '[SHORTCUT]tag'). highlight: names to outline.
    show_tags: print tag names in numeric displays. ActiveX/images are placeholders."""
    doc = _load(xml_path)
    if out_png is None:
        out = Path(tempfile.gettempdir()) / "ftview_mcp" / (Path(xml_path).stem + ".png")
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        out = Path(out_png)
    R.render(doc, out, highlight, show_tags, tuple(region) if region else None, simulate)
    return [f"preview: {out}", Image(path=out)]


@_tool
def ftv_diff(xml_a: str, xml_b: str) -> dict:
    """Object-level diff of two display XMLs (added / removed / changed attributes)."""
    a, b = _load(xml_a), _load(xml_b)
    d = G.diff(a, b)
    d["changed"] = d["changed"][:300]
    return d


@_tool
def ftv_check_import_log(log_path: str) -> dict:
    """Parse View Studio's DisplaysImport.txt and say whether each display import really applied.
    Flags objects that were skipped because they 'already exist' (merge instead of replace)."""
    return PR.parse_import_log(log_path)


# ---------------------------------------------------------------------------
# Editing (always writes a new, validated XML)
# ---------------------------------------------------------------------------

@_tool
def ftv_set_attributes(xml_path: str, changes: list[dict], out_path: str, overwrite: bool = False) -> dict:
    """Set/remove attributes on objects. changes: [{"name": "Button1", "attrs": {"left": "100",
    "backColor": "#C0C0C0", "toolTipText": null}}]. For nested elements use "path": an XPath
    relative to the object, e.g. {"name": "Button1", "path": "up/caption", "attrs": {"caption": "START"}}.
    Writes out_path (validated; overwrite=true backs up an existing file first)."""
    src = _load(xml_path)
    doc = G.GfxDoc(src.raw, src.path)
    applied = []
    for ch in changes:
        el = doc.get(ch["name"])
        if ch.get("path"):
            found = el.xpath(ch["path"])
            if not found:
                raise G.GfxError(f"{ch['name']}: path {ch['path']!r} matched nothing")
            targets = found
        else:
            targets = [el]
        for t in targets:
            for k, v in ch["attrs"].items():
                if v is None:
                    t.attrib.pop(k, None)
                else:
                    t.set(k, str(v))
        applied.append({"name": ch["name"], "path": ch.get("path"), "elements": len(targets)})
    res = _save(doc, src, out_path, overwrite)
    res["applied"] = applied
    return res


_SKIP_ATTRS = {"classId", "typeLibrary", "data", "path", "line", "polygon"}
_CAPTION_ATTRS = {"caption", "toolTipText", "titleBarText", "description"}


@_tool
def ftv_replace(xml_path: str, find: str, replace: str, out_path: str | None = None,
                scope: str = "tags", regex: bool = False, objects: list[str] | None = None,
                dry_run: bool = True, overwrite: bool = False) -> dict:
    """Find/replace in attribute values. scope: 'tags' (inside {...} references and command
    arguments only - use for tag renames / shortcut changes), 'captions' (caption, toolTipText,
    titleBarText), or 'all' (every attribute except ActiveX blobs and geometry).
    objects: limit to these objects (and their children). dry_run=True only reports matches."""
    if scope not in ("tags", "captions", "all"):
        raise ValueError("scope must be tags | captions | all")
    src = _load(xml_path)
    doc = G.GfxDoc(src.raw, src.path)
    rx = re.compile(find if regex else re.escape(find))
    limit = None
    if objects:
        limit = set()  # holds the proxies, so lxml hands back the same objects while iterating
        for n in objects:
            limit.update(doc.get(n).iter())
    hits = []
    for el in doc.root.iter():
        if not isinstance(el.tag, str) or (limit is not None and el not in limit):
            continue
        for attr, val in list(el.attrib.items()):
            if not val or attr in _SKIP_ATTRS:
                continue
            if scope == "captions" and attr not in _CAPTION_ATTRS:
                continue
            if scope == "tags":
                if attr in G.COMMAND_ATTRS or attr.endswith("Command") or \
                        (el.tag == "connection" and "{" not in val):
                    new = rx.sub(replace, val)
                else:
                    new = G._BRACE.sub(lambda m: "{" + rx.sub(replace, m.group(1)) + "}", val)
            else:
                new = rx.sub(replace, val)
            if new != val:
                owner = G._owner(el, doc)
                hits.append({"object": owner.get("name") if owner is not None else "(display)",
                             "element": el.tag, "attribute": attr, "before": val[:200], "after": new[:200]})
                el.set(attr, new)
    res: dict[str, Any] = {"matches": len(hits), "changes": hits[:200]}
    if dry_run or not hits:
        res["written"] = False
        return res
    if not out_path:
        raise ValueError("out_path is required when dry_run=false")
    res.update(_save(doc, src, out_path, overwrite))
    res["written"] = True
    return res


@_tool
def ftv_move_objects(xml_path: str, names: list[str], dx: float, dy: float, out_path: str,
                     overwrite: bool = False) -> dict:
    """Move objects (with all children and polygon/line points) by dx, dy pixels."""
    src = _load(xml_path)
    doc = G.GfxDoc(src.raw, src.path)
    for n in names:
        doc.move(n, dx, dy)
    return _save(doc, src, out_path, overwrite)


@_tool
def ftv_scale_objects(xml_path: str, names: list[str], sx: float, sy: float, out_path: str,
                      origin: list[float] | None = None, overwrite: bool = False) -> dict:
    """Scale objects by sx, sy about origin [x, y] (default: each object's top-left corner).
    Font sizes are not changed."""
    src = _load(xml_path)
    doc = G.GfxDoc(src.raw, src.path)
    for n in names:
        el = doc.get(n)
        b = G.bbox(el)
        ox, oy = origin if origin else (b.left, b.top)
        G.scale(el, sx, sy, ox, oy)
    return _save(doc, src, out_path, overwrite)


@_tool
def ftv_delete_objects(xml_path: str, names: list[str], out_path: str, overwrite: bool = False) -> dict:
    """Delete objects (and their children)."""
    src = _load(xml_path)
    doc = G.GfxDoc(src.raw, src.path)
    for n in names:
        doc.delete(n)
    return _save(doc, src, out_path, overwrite)


@_tool
def ftv_insert_objects(xml_path: str, xml_fragment: str, out_path: str, parent_group: str | None = None,
                       before: str | None = None, overwrite: bool = False) -> dict:
    """Insert object XML (one or more elements in Gfx-SE schema form, e.g. copied from
    ftv_get_object and modified). Names that already exist are renamed automatically (View
    Studio would otherwise silently skip them). Rejected if the result fails schema validation."""
    src = _load(xml_path)
    doc = G.GfxDoc(src.raw, src.path)
    renames = doc.insert(xml_fragment, parent_group, before)
    res = _save(doc, src, out_path, overwrite)
    res["inserted"] = [{"requested": a, "name": b} for a, b in renames]
    return res


@_tool
def ftv_copy_objects(src_xml: str, names: list[str], dst_xml: str, out_path: str, dx: float = 0,
                     dy: float = 0, overwrite: bool = False) -> dict:
    """Copy objects from one display to another (or the same one), offset by dx, dy.
    Colliding names are renamed."""
    s = _load(src_xml)
    base = _load(dst_xml)
    doc = G.GfxDoc(base.raw, base.path)
    renames = []
    for n in names:
        el = copy.deepcopy(s.get(n))
        el.tail = None
        if dx or dy:
            G.shift(el, dx, dy)
        renames += doc.insert(el)
    res = _save(doc, base, out_path, overwrite)
    res["inserted"] = [{"requested": a, "name": b} for a, b in renames]
    return res


@_tool
def ftv_new_display(out_path: str, width: int = 1920, height: int = 1080, back_color: str = "#DDDDDD",
                    title: str = "", schema: str = "Gfx-SE12.xsd", overwrite: bool = False) -> dict:
    """Create an empty display XML (replace-type, no title bar by default) to build on."""
    doc = G.GfxDoc.new(width, height, schema, backColor=back_color,
                       titleBar="true" if title else "false", titleBarText=title)
    return _save(doc, None, out_path, overwrite)


@_tool
def ftv_make_batch_import(xml_paths: list[str], out_path: str) -> dict:
    """Write a View Studio batch import file listing these display XMLs (keep them in the same
    folder as the batch file). Import it with the Graphics Import Export Wizard, choosing to
    replace existing displays/objects; then check DisplaysImport.txt with ftv_check_import_log."""
    p = PR.write_batch_import(xml_paths, out_path)
    return {"batch_file": str(p), "displays": [Path(x).stem for x in xml_paths]}


# ---------------------------------------------------------------------------
# Phase 2: schema help, ISA-101 templates, generation, style
# ---------------------------------------------------------------------------

@_tool
def ftv_schema(path: str, schema: str = "Gfx-SE12.xsd") -> dict:
    """Describe what the Gfx schema allows for an element: attributes (type, enum values,
    required) and child elements in order. path like 'button', 'button/command',
    'rectangle/animations/animateFill', or a complexType name. Use before hand-writing XML."""
    from . import schema as S
    return S.info(schema).describe(path)


@_tool
def ftv_template_catalog() -> dict:
    """ISA-101 object templates (pump, blower, mixer, motor, valve, tank, analog, setpoint, status,
    button, alarm, mode, label, panel, pipe, header, nav) with parameters, plus the default style."""
    from . import templates as T
    return {"templates": T.catalog(), "style": T.HP_STYLE,
            "tag_syntax": "bare name -> [shortcut]name; '[X]name' kept; '=expression' used verbatim; "
                          "'NOT name' allowed in mode/status conditions"}


def _spec_arg(spec: dict | str) -> dict:
    import json
    if isinstance(spec, str):
        p = Path(spec)
        if p.suffix.lower() == ".json" and p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
        return json.loads(spec)
    return spec


def _preview(doc, out_path, simulate=None):
    png = Path(out_path).with_suffix(".png")
    R.render(doc, png, simulate=simulate)
    return png


@_tool
def ftv_generate_display(spec: dict | str, out_path: str, l5x: str | None = None, project: str | None = None,
                         preview: bool = True, simulate: dict | None = None, overwrite: bool = False,
                         strict: bool = True):
    """Generate a complete ISA-101 display XML from a spec (dict, JSON text, or .json path).
    See ftview-mcp/examples/equalization_hp.json and ftv_template_catalog (tag_roles per template)
    for the format. Validates against the schema, lints (PLC tags with l5x, navigation with
    project) and renders a PNG preview next to out_path (simulate={"values": {...}}).
    strict=True (default): any lint error (e.g. a PLC tag that does not exist) is a tool error and
    nothing is written; strict=False writes anyway and returns the findings."""
    from . import generate as GEN
    doc, report = GEN.generate(_spec_arg(spec))
    findings = L.lint(doc, PR.display_names(project) if project else None, None, P.load(l5x) if l5x else None)
    errors = [f for f in findings if f["severity"] == "error"]
    if strict and errors:
        raise ToolError("[INVALID] not written (strict=true); lint errors: " + json.dumps(errors)[:3000])
    res = PR.safe_write(doc, out_path, overwrite)
    res.update({"summary": L.summarize(findings), "findings": findings[:100], "objects": report["objects"]})
    if preview:
        png = _preview(doc, out_path, simulate)
        return [res, Image(path=png)]
    return res


@_tool
def ftv_generate_faceplate(out_path: str, title: str = "Motor", auto: bool = True, reset: bool = True,
                           overwrite: bool = False):
    """Generate an on-top motor/pump faceplate display XML driven by /T tag parameters.
    Returns the parameter order to use in Display commands (e.g. run, fault, auto, cmd, reset)."""
    from . import generate as GEN
    doc, order = GEN.motor_faceplate(title, auto=auto, reset=reset)
    res = PR.safe_write(doc, out_path, overwrite)
    name = Path(out_path).stem
    res["parameters"] = {f"#{i + 1}": k for i, k in enumerate(order)}
    res["open_with"] = f'Display "{name}" /T' + ",".join(f"<{k} tag>" for k in order)
    png = _preview(doc, out_path)
    return [res, Image(path=png)]


@_tool
def ftv_add_template(xml_path: str, type: str, params: dict, out_path: str, shortcut: str = "SCADA",
                     style: dict | None = None, overwrite: bool = False) -> dict:
    """Add one ISA-101 template object (e.g. type='pump', params={'id': 'P301', 'x': 400, 'y': 300,
    'label': 'P-301', 'tags': {'run': 'P301_RUN', 'fault': 'P301_TRIP'}}) to an existing display."""
    from . import templates as T
    if type not in T.TEMPLATES:
        raise ValueError(f"unknown template {type!r}; known: {sorted(T.TEMPLATES)}")
    src = _load(xml_path)
    doc = G.GfxDoc(src.raw, src.path)
    ctx = T.Ctx(shortcut, style, taken=set(doc.index()))
    p = dict(params)
    oid = p.pop("id", type)
    T.check_params(type, p)
    built = T.TEMPLATES[type](ctx, oid, **p)
    renames = doc.insert(built.element)
    res = _save(doc, src, out_path, overwrite)
    res["inserted"] = renames[0][1] if renames else None
    res["ports"] = {k: [round(v[0]), round(v[1]), v[2]] for k, v in built.ports.items()}
    return res


@_tool
def ftv_search_plc_tags(l5x: str, pattern: str, data_type: str | None = None, limit: int = 200) -> dict:
    """Search Logix tags (controller and program scope) by regex on name or description.
    Use to find the right tags when building or rebinding screens."""
    plc = P.load(l5x)
    rx = re.compile(pattern, re.I)
    out = []
    scopes = [("", plc.tags)] + [(f"Program:{n}.", t) for n, t in plc.programs.items()]
    for prefix, tags in scopes:
        for t in tags.values():
            desc = (t.findtext("Description") or "").strip()
            if not (rx.search(t.get("Name")) or rx.search(desc)):
                continue
            if data_type and (t.get("DataType") or "").upper() != data_type.upper():
                continue
            out.append({"tag": prefix + t.get("Name"), "type": t.get("DataType") or t.get("AliasFor"),
                        "description": desc[:120]})
    return {"count": len(out), "tags": out[:limit]}


@_tool
def ftv_style_audit(xml_path: str) -> dict:
    """ISA-101 / high-performance audit of a display: saturated colours on static graphics,
    gradients, background, non-alarm blinking, with a rough 0-100 score and colour usage."""
    from . import style as ST
    doc = _load(xml_path)
    a = ST.isa101_audit(doc)
    a["colors"] = ST.color_report(doc)
    return a


@_tool
def ftv_recolor(xml_path: str, mapping: dict[str, str], out_path: str | None = None,
                include_animations: bool = False, objects: list[str] | None = None, dry_run: bool = True,
                overwrite: bool = False) -> dict:
    """Replace colours, e.g. {"#0000FC": "#808080", "teal": "#C8C8C8"}. Static graphics only unless
    include_animations (keeps alarm/state colours intact). objects limits the scope.
    dry_run=True reports what would change."""
    from . import style as ST
    src = _load(xml_path)
    doc = G.GfxDoc(src.raw, src.path)
    changes = ST.recolor(doc, mapping, include_animations, objects)
    res: dict[str, Any] = {"changes": len(changes), "sample": changes[:100]}
    if dry_run or not changes:
        res["written"] = False
        return res
    if not out_path:
        raise ValueError("out_path is required when dry_run=false")
    res.update(_save(doc, src, out_path, overwrite))
    res["written"] = True
    return res


# ---------------------------------------------------------------------------
# Phase 3: HMI tags, parameter files, macros, global objects
# ---------------------------------------------------------------------------

@_tool
def ftv_tags_read(csv_path: str, pattern: str | None = None, limit: int = 500) -> dict:
    """Read a View tag export CSV (Tag Import and Export Wizard format ###002)."""
    from . import tags as TG
    db = TG.TagDB.read(csv_path)
    rx = re.compile(pattern, re.I) if pattern else None
    rows = [{"name": t.name, "type": t.type, **{k: v for k, v in t.values.items() if v not in (None, "")}}
            for t in db.tags.values() if not rx or rx.search(t.name)]
    return {"folders": db.folders, "count": len(rows), "tags": rows[:limit]}


@_tool
def ftv_tags_write(tags: list[dict], out_csv: str, l5x: str | None = None, overwrite: bool = False) -> dict:
    """Write HMI tags to an importable CSV. Each item: {"kind": "analog"|"digital"|"string",
    "name": "Folder\\\\Tag", "source": "memory"|"device", "address": "[SCADA]PLC_Tag" (device),
    plus options (description, min, max, initial, units, off_label, on_label, length, ...).
    With l5x, device addresses are resolved against the PLC program first."""
    from . import tags as TG
    out = Path(out_csv)
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} exists; pass overwrite=true")
    db = TG.TagDB()
    for t in tags:
        t = dict(t)
        kind = t.pop("kind", "analog")
        fn = {"analog": db.add_analog, "digital": db.add_digital, "string": db.add_string}.get(kind)
        if fn is None:
            raise ValueError(f"unknown tag kind {kind!r}")
        fn(t.pop("name"), **t)
    issues = db.validate(P.load(l5x) if l5x else None)
    if any(i["severity"] == "error" for i in issues):
        raise ToolError("[INVALID] not written; fix these first: " + json.dumps([i for i in issues if i["severity"] == "error"])[:3000])
    db.write(out)
    return {"written": True, "path": str(out), "tags": len(db.tags), "folders": db.folders, "issues": issues}


@_tool
def ftv_tags_validate(csv_path: str, l5x: str | None = None) -> dict:
    """Validate a tag CSV: folders defined, min/max/initial, device addresses against the L5X."""
    from . import tags as TG
    db = TG.TagDB.read(csv_path)
    issues = db.validate(P.load(l5x) if l5x else None)
    return {"tags": len(db.tags), "issues": issues}


@_tool
def ftv_parameter_file(out_path: str, params: dict[str, str], comments: dict[str, str] | None = None,
                       l5x: str | None = None, overwrite: bool = False) -> dict:
    """Write a .par parameter file: params {"#1": "[SCADA]P101A_RUN", ...}. Use it with
    Display "X" /P<name>. Validated against the L5X when given."""
    from . import textfiles as TF
    out = Path(out_path)
    if out.suffix.lower() != ".par":
        raise ValueError("parameter files end in .par")
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} exists; pass overwrite=true")
    pf = TF.ParameterFile(dict(params), dict(comments or {}))
    issues = pf.validate(P.load(l5x) if l5x else None)
    if any(i["severity"] == "error" for i in issues):
        raise ToolError("[INVALID] not written; fix these first: " + json.dumps([i for i in issues if i["severity"] == "error"])[:3000])
    pf.write(out)
    return {"written": True, "path": str(out), "issues": issues}


@_tool
def ftv_macro(out_path: str, commands: list[str], project: str | None = None, l5x: str | None = None,
              overwrite: bool = False) -> dict:
    """Write a .mcr macro (one FactoryTalk View command per line), validating command verbs,
    target displays (with project) and PLC tags (with l5x)."""
    from . import textfiles as TF
    out = Path(out_path)
    if out.suffix.lower() != ".mcr":
        raise ValueError("macros end in .mcr")
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} exists; pass overwrite=true")
    m = TF.Macro(list(commands))
    issues = m.validate(PR.display_names(project) if project else None, P.load(l5x) if l5x else None)
    if any(i["severity"] == "error" for i in issues):
        raise ToolError("[INVALID] not written; fix these first: " + json.dumps([i for i in issues if i["severity"] == "error"])[:3000])
    m.write(out)
    return {"written": True, "path": str(out), "issues": issues}


@_tool
def ftv_read_text_component(path: str) -> dict:
    """Read a .par parameter file or .mcr macro (from an HMI project or anywhere)."""
    from . import textfiles as TF
    p = Path(path)
    if p.suffix.lower() == ".par":
        pf = TF.ParameterFile.read(p)
        return {"type": "parameters", "params": pf.params, "comments": pf.comments}
    if p.suffix.lower() == ".mcr":
        return {"type": "macro", "commands": TF.Macro.read(p).commands}
    raise ValueError("expected a .par or .mcr file")


@_tool
def ftv_global_object_define(xml_path: str, object_name: str, parameters: dict[str, str], out_path: str,
                             overwrite: bool = False) -> dict:
    """Add parameter definitions to an object of a global object file (exported .ggfx XML),
    e.g. {"#1": "Label", "#2": "Run tag"} (name -> description)."""
    from . import build as B
    src = _load(xml_path)
    doc = G.GfxDoc(src.raw, src.path)
    B.set_parameters(doc.get(object_name), {k: "" for k in parameters}, parameters)
    return _save(doc, src, out_path, overwrite)


@_tool
def ftv_global_object_place(display_xml: str, global_xml: str, object_name: str, instance_name: str,
                            x: float, y: float, values: dict[str, str], out_path: str,
                            overwrite: bool = False) -> dict:
    """Place an instance of a global object (reference object linked to '<global file>.<object>')
    on a display at (x, y), with parameter values {"#1": "P-101A", "#2": "[SCADA]P101A_RUN"}."""
    from . import build as B
    go = _load(global_xml)
    base = go.get(object_name)
    b = G.bbox(base)
    src = _load(display_xml)
    doc = G.GfxDoc(src.raw, src.path)
    inst = B.reference_instance(base, Path(global_xml).stem, instance_name, x - b.left, y - b.top, values)
    renames = doc.insert(inst)
    res = _save(doc, src, out_path, overwrite)
    res["inserted"] = renames[0][1]
    return res


# ---------------------------------------------------------------------------
# Headless, read-only access to live applications (no View Studio UI)
# ---------------------------------------------------------------------------

@_tool
def ftv_live_list_displays(app: str, server: str | None = None) -> dict:
    """List the displays of an SE local application straight from the project, without View
    Studio (View's own Gfx-to-XML engine; read-only, safe on any app, ~5 s)."""
    from . import headless as H
    return {"app": app, "displays": H.list_displays(app, server)}


@_tool
def ftv_live_export(app: str, out_dir: str, displays: list[str] | None = None, server: str | None = None) -> dict:
    """Export displays of an SE local application to XML without View Studio (read-only, safe on
    any app; all displays when `displays` is omitted). Same XML as the Graphics Import Export
    Wizard. Use it to get current, authoritative XML before editing."""
    from . import headless as H
    out = Path(out_dir).expanduser().resolve()
    if str(out).lower().startswith(str(PR.SE_PROJECTS.resolve()).lower()):
        raise PermissionError("export into a folder outside HMI Projects")
    return H.export_displays(app, out, displays, server)


@_tool
def ftv_live_update_display(xml_path: str, display: str | None = None, app: str | None = None,
                            confirm: bool = False) -> dict:
    """Apply an edited display XML WITHOUT View Studio (no UI, ~1 min), in update mode: changes to
    objects that already exist (move/resize, colours, captions, tooltips, tag bindings, animations).
    It cannot add objects - an XML with new objects is refused (use ftv_studio_replace_display).
    Safety: sandbox app by default (other apps need app=... and confirm=true); refused while the app
    is open in View Studio; the .gfx is backed up; the VBA project and global-object tables are kept;
    the result is exported and compared with the XML, and restored from backup on any difference.
    display defaults to the XML file name."""
    from . import headless as H
    target = app or H.SANDBOX
    if target != H.SANDBOX and not confirm:
        raise PermissionError(f"'{target}' is not the sandbox; pass confirm=true to change it")
    name = display or Path(xml_path).stem
    return H.update_display(target, name, xml_path, allow_app=target)


@_tool
def ftv_live_diff(app: str, display: str, xml_path: str) -> dict:
    """Compare what is live in the application with an XML file (e.g. to check that an import
    really applied, or what changed since the last export). Read-only, no UI."""
    from . import headless as H
    from . import verify as V
    work = Path(tempfile.mkdtemp(prefix="ftv_live_"))
    res = H.export_displays(app, work, [display])
    if display not in res["files"]:
        raise FileNotFoundError(f"display {display!r} not exported from {app}: {res}")
    live = G.GfxDoc.load(res["files"][display])
    r = V.compare(_load(xml_path), live)
    return {"in_sync": r["ok"], "objects_in_file": r["objects_sent"], "objects_live": r["objects_exported"],
            "in_file_not_live": r["missing"][:100], "live_not_in_file": r["extra"][:100],
            "differences": r["differences"][:100], "live_xml": res["files"][display]}


# ---------------------------------------------------------------------------
# View Studio automation and end-to-end tests (needs the interactive desktop)
# ---------------------------------------------------------------------------

def _studio(app: str | None, confirm: bool, mutating: bool):
    from . import studio as S
    name = app or S.SANDBOX
    if name != S.SANDBOX and mutating and not confirm:
        raise PermissionError(f"'{name}' is not the sandbox ({S.SANDBOX}); pass confirm=true to change it")
    return S.Studio(name, allow_app=name)


@_tool
def ftv_studio_status() -> dict:
    """Is View Studio running, which application is open, and what is in the sandbox."""
    from . import studio as S
    st = S.Studio()
    running = st.running()
    out = {"running": running, "sandbox": S.SANDBOX, "sandbox_exists": st.sed.is_file()}
    if running:
        texts = st.tree_texts()
        out["sandbox_open"] = S.SANDBOX in texts
        out["tree_top"] = texts[:3]
    if st.sed.is_file():
        out["sandbox_displays"] = st.display_names()
    return out


@_tool
def ftv_studio_import_displays(batch_file: str, mode: str = "create", node: str = "Displays",
                               app: str | None = None, confirm: bool = False) -> dict:
    """Import a batch file through View Studio's Graphics Import Export Wizard (default: the
    sandbox app). mode must be 'create' (new displays/objects; objects whose names already exist
    on a display are SKIPPED) or 'update' (changes existing objects only; cannot add or remove
    objects). For a full replace use ftv_studio_replace_display. node 'Global Objects' for .ggfx.
    Returns the parsed import log; check every display's 'applied'."""
    st = _studio(app, confirm, True)
    log = st.import_displays(batch_file, mode, node)
    return PR.parse_import_log(log)


@_tool
def ftv_studio_export_displays(out_dir: str, node: str = "Displays", app: str | None = None) -> dict:
    """Export every display (or global object) of the open application to XML in out_dir."""
    st = _studio(app, False, False)
    return {"files": [str(p) for p in st.export_displays(out_dir, node)]}


@_tool
def ftv_studio_replace_display(xml_path: str, display_name: str | None = None, app: str | None = None,
                               confirm: bool = False) -> dict:
    """Reliable apply: delete the display (if present) then import the XML in create mode, then
    export it back and compare with what was sent. This is the only mode that also applies
    added/removed objects exactly. display_name: the display to replace; View names an imported
    display after the XML file, so the default is the file name without .xml - pass display_name
    when the file is an edited copy (e.g. 'X_edit.xml' -> display_name='X')."""
    from . import verify as V
    st = _studio(app, confirm, True)
    src = _load(xml_path)
    name = display_name or Path(xml_path).stem
    existed = name in st.display_names()
    if existed:
        st.delete_display(name)
    work = Path(tempfile.mkdtemp(prefix="ftv_replace_"))
    shutil_copy = work / f"{name}.xml"
    shutil_copy.write_bytes(src.to_bytes())
    log = st.import_displays(PR.write_batch_import([str(shutil_copy)], work / "Batch.xml"), "create")
    parsed = PR.parse_import_log(log)
    out = work / "export"
    st.export_displays(out)  # wizard export: the app is open in Studio, so headless is not allowed
    cmp = V.compare(src, G.GfxDoc.load(out / f"{name}.xml"))
    return {"display": name, "replaced_existing": existed, "import": parsed, "verified": cmp["ok"],
            "differences": cmp["differences"][:50], "missing": cmp["missing"],
            "normalized": cmp["normalized_count"], "normalized_sample": cmp["normalized_sample"]}


@_tool
def ftv_studio_delete(names: list[str], node: str = "Displays", app: str | None = None,
                      confirm: bool = False) -> dict:
    """Delete displays (node='Displays') or global objects (node='Global Objects') from the
    sandbox app (or `app` with confirm=true). Use to clean up after tests."""
    if node not in ("Displays", "Global Objects"):
        raise ValueError("node must be 'Displays' or 'Global Objects'")
    st = _studio(app, confirm, True)
    done = []
    for n in names:
        st.delete_display(n, node=node)
        done.append(n)
    return {"deleted": done, "node": node}


@_tool
def ftv_studio_tags(action: str, csv_dir_or_file: str, sed: str | None = None, app: str | None = None,
                    confirm: bool = False, on_exists: str = "update") -> dict:
    """HMI tags through View's Tag Import and Export Wizard. action 'export': write
    <project>-Tags.CSV/-Alarms.CSV of `sed` (any SE .sed, read-only; default sandbox) to the
    folder. action 'import': import the CSV into the sandbox (or `app` with confirm=true).
    Returns the import log errors and counts."""
    if action == "export":
        st = _studio(app, False, False)
        return st.export_tags(csv_dir_or_file, sed)
    if action == "import":
        st = _studio(app, confirm, True)
        return st.import_tags(csv_dir_or_file, on_exists=on_exists)
    raise ValueError("action must be 'export' or 'import'")


@_tool
def ftv_studio_screenshot(out_png: str | None = None):
    """Screenshot of the desktop (to see what View Studio is showing)."""
    from . import studio as S
    p = Path(out_png) if out_png else Path(tempfile.gettempdir()) / "ftview_mcp" / "screen.png"
    S.screenshot(p)
    return [str(p), Image(path=p)]


@_tool
def ftv_e2e_run(scenarios: list[str] | None = None) -> dict:
    """Start the end-to-end test suite against real View Studio (sandbox app) in the background.
    Scenarios: generate, edit, corpus, tags, global (default all; ~2.5 min, display scenarios run as one batched pipeline). Poll with
    ftv_e2e_status. Uses the desktop (mouse/keyboard focus) while running."""
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    log = Path(tempfile.gettempdir()) / "ftview_mcp" / "e2e_last.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        proc = subprocess.Popen([sys.executable, "-m", "ftview_mcp.e2e", *(scenarios or [])], cwd=str(root),
                                stdout=f, stderr=subprocess.STDOUT)
    return {"started": True, "pid": proc.pid, "console_log": str(log),
            "runs_dir": str(root.parent / "ftview-e2e" / "runs")}


@_tool
def ftv_e2e_status(run_dir: str | None = None) -> dict:
    """Result of the latest (or given) end-to-end run: per-scenario pass/fail and failed checks."""
    import json
    root = Path(__file__).resolve().parents[1].parent / "ftview-e2e" / "runs"
    d = Path(run_dir) if run_dir else max(root.iterdir(), key=lambda p: p.name)
    rep = d / "report.json"
    if not rep.exists():
        tail = (d / "run.log").read_text(encoding="utf-8").splitlines()[-15:] if (d / "run.log").exists() else []
        return {"finished": False, "run_dir": str(d), "progress": tail}
    data = json.loads(rep.read_text(encoding="utf-8"))
    return {"finished": True, "passed": data["passed"], "run_dir": str(d),
            "scenarios": {r["scenario"]: {"passed": r["passed"], "error": r.get("error"),
                                          "failed_checks": [c for c in r["checks"] if not c["ok"]][:10]}
                          for r in data["results"]},
            "report_md": str(d / "report.md")}


def main() -> None:
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
