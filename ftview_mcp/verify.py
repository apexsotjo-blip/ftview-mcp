"""Compare what we sent to View Studio with what View exported back.

View re-exports every attribute (including defaults) and may normalise values
(colour names, number formats, whitespace). This comparison checks that every
attribute we *sent* came back with the same meaning, that no object went missing,
and lists what View added. It walks sub-elements (animations, connections,
captions, states) positionally within same-tag siblings.
"""
from __future__ import annotations

import re

from . import gfx as G
from .style import NAMED

_NUM = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def norm_value(attr: str, v: str | None):
    if v is None:
        return None
    s = v.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low
    if _NUM.match(s):
        return round(float(s), 6)
    if attr.lower().endswith("color") or re.search(r"[cC]olor\d$", attr):
        hx = NAMED.get(low, s)
        return hx.upper()
    if attr in ("path", "line", "polygon"):
        return " ".join(s.split())
    if attr in ("expression", "pressAction", "releaseAction", "repeatAction", "caption", "toolTipText"):
        return re.sub(r"\s+", " ", s.replace("\r\n", "\n"))
    return s


GEOM = {"left", "top", "width", "height"}
POINTS = {"path", "line", "polygon"}
AUTO_SIZED = {"numericDisplay", "numericInput", "stringDisplay", "stringInput"}
ALWAYS_IGNORED = {"tabIndex"}  # View renumbers tab order on import
EXPORTER_ONLY_ELEMENTS = {"transform"}
GEOM_TOL = 2


def _points_close(a: str, b: str) -> bool:
    pa, pb = a.split(), b.split()
    return len(pa) == len(pb) and all(abs(float(x) - float(y)) <= GEOM_TOL for x, y in zip(pa, pb))


FONT_REMAP = {("arial unicode ms", "arial")}  # View v12 import on this install


def _classify(sent, got, k: str, v: str, gv: str | None) -> str | None:
    """None = equal; 'normalized' = known harmless View behaviour; 'real' = a difference.
    Every rule here was observed by importing into View Studio and exporting back."""
    el_tag = sent.tag
    if gv is not None and norm_value(k, v) == norm_value(k, gv):
        return None
    if k in ALWAYS_IGNORED:
        return "normalized"
    if k == "imageReference" and v == "noImage" and gv is None:
        return "normalized"  # the headless (Rocket) exporter omits this default
    if el_tag == "displaySettings" and k in ("positionX", "positionY") and gv is None:
        return "normalized"  # written by the headless exporter only
    if el_tag in AUTO_SIZED and k in GEOM | {"fontSize", "charHeight", "charWidth"}:
        return "normalized"  # View fits these boxes to fieldLength/char size
    if k == "fontFamily" and gv is not None and (v.lower(), gv.lower()) in FONT_REMAP:
        return "normalized"
    if k == "fontSize" and gv is None and got.get("charHeight") is not None:
        return "normalized"  # same size expressed as charHeight/charWidth
    if el_tag == "text" and sent.get("sizeToFit") == "true" and k in ("width", "height", "charWidth", "charHeight"):
        return "normalized"  # View re-measures size-to-fit text (and its character size) when the caption changes
    if el_tag == "confirm" and sent.get("confirmAction") == "false" and k != "confirmAction":
        return "normalized"
    if el_tag == "caption" and sent.getparent() is not None and sent.getparent().tag == "confirm" \
            and sent.getparent().get("confirmAction") == "false":
        return "normalized"  # View resets the prompt text when confirmation is off
    if gv is not None and k in GEOM:
        try:
            if abs(float(v) - float(gv)) <= GEOM_TOL:
                return "normalized"
        except ValueError:
            pass
    if gv is not None and k in POINTS and _points_close(v, gv):
        return "normalized"
    return "real"


def _compare_el(sent, got, path: str, out: list, norm: list | None = None):
    norm = out if norm is None else norm
    if sent.tag == "down" and sent.get("downSameAsUp") == "true":
        return  # View blanks the down state when it mirrors the up state
    for k, v in sent.attrib.items():
        gv = got.get(k)
        kind = _classify(sent, got, k, v, gv)
        if kind is None:
            continue
        rec = {"path": path, "attribute": k, "sent": v, "exported": gv,
               "kind": "dropped" if gv is None else "changed"}
        (norm if kind == "normalized" else out).append(rec)
    sent_kids = [c for c in sent if isinstance(c.tag, str) and not G.is_object(c)]
    got_kids = [c for c in got if isinstance(c.tag, str) and not G.is_object(c)]
    by_tag: dict[str, list] = {}
    for c in got_kids:
        by_tag.setdefault(c.tag, []).append(c)
    seen: dict[str, int] = {}
    for c in sent_kids:
        i = seen.get(c.tag, 0)
        seen[c.tag] = i + 1
        cands = by_tag.get(c.tag, [])
        key = c.get("name") or c.get("value") or c.get("stateId")
        match = None
        if key is not None:
            match = next((g for g in cands if (g.get("name") or g.get("value") or g.get("stateId")) == key), None)
        if match is None and i < len(cands):
            match = cands[i]
        sub = f"{path}/{c.tag}" + (f"[{key}]" if key is not None else f"[{i}]")
        if match is None:
            # The headless (Rocket, Gfx-SE13) exporter adds <transform> elements that the wizard's
            # SE12 export does not write; seen when comparing a headless export against a wizard one.
            (norm if c.tag in EXPORTER_ONLY_ELEMENTS else out).append({"path": sub, "kind": "element_dropped"})
            continue
        _compare_el(c, match, sub, out, norm)


def compare(sent: G.GfxDoc, exported: G.GfxDoc) -> dict:
    si, ei = sent.index(), exported.index()
    missing = sorted(set(si) - set(ei))
    extra = sorted(set(ei) - set(si))
    diffs: list[dict] = []
    normalized: list[dict] = []
    for name in sorted(set(si) & set(ei)):
        a, b = si[name], ei[name]
        if a.tag != b.tag:
            diffs.append({"path": name, "kind": "type_changed", "sent": a.tag, "exported": b.tag})
            continue
        _compare_el(a, b, name, diffs, normalized)
    if sent.settings is not None and exported.settings is not None:
        _compare_el(sent.settings, exported.settings, "(displaySettings)", diffs, normalized)
    # group membership: parent of each object must match
    parent_moves = []
    for name in sorted(set(si) & set(ei)):
        pa, pb = si[name].getparent(), ei[name].getparent()
        na = pa.get("name") if pa is not None and pa.tag == "group" else "(display)"
        nb = pb.get("name") if pb is not None and pb.tag == "group" else "(display)"
        if na != nb:
            parent_moves.append({"object": name, "sent_parent": na, "exported_parent": nb})
    ok = not missing and not diffs and not parent_moves
    return {"ok": ok, "objects_sent": len(si), "objects_exported": len(ei), "missing": missing,
            "extra": extra, "differences": diffs, "parent_changes": parent_moves,
            "normalized_count": len(normalized), "normalized_sample": normalized[:20]}
