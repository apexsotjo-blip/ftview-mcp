"""Colour utilities, ISA-101 style audit and controlled recolouring of existing displays."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import gfx as G

NAMED = {
    "black": "#000000", "white": "#FFFFFF", "red": "#FF0000", "lime": "#00FF00", "green": "#008000",
    "blue": "#0000FF", "yellow": "#FFFF00", "cyan": "#00FFFF", "aqua": "#00FFFF", "magenta": "#FF00FF",
    "fuchsia": "#FF00FF", "teal": "#008080", "navy": "#000080", "maroon": "#800000", "purple": "#800080",
    "olive": "#808000", "gray": "#808080", "grey": "#808080", "silver": "#C0C0C0", "lightgray": "#D3D3D3",
    "lightgrey": "#D3D3D3", "darkgray": "#A9A9A9", "whitesmoke": "#F5F5F5", "orange": "#FFA500",
}
COLOR_ATTR = re.compile(r"(?:^|[a-z])(?:[Cc]olor\d?)$")
TEXT_TAGS = {"text", "numericDisplay", "numericInput", "stringDisplay", "stringInput", "timeAndDateDisplay"}
ALARM_HINT =re.compile(r"alarm|fault|trip|fail|alm|hh|ll", re.I)


def to_rgb(c: str | None):
    if not c:
        return None
    c = c.strip()
    hx = NAMED.get(c.lower(), c)
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", hx):
        return tuple(int(hx[i:i + 2], 16) for i in (1, 3, 5))
    return None


def saturation(c: str) -> float:
    rgb = to_rgb(c)
    if not rgb:
        return 0.0
    mx, mn = max(rgb), min(rgb)
    return 0.0 if mx == 0 else (mx - mn) / mx


def is_color_attr(name: str) -> bool:
    return bool(COLOR_ATTR.search(name)) or name in ("color",)


def _in_animation(el) -> bool:
    e = el
    while e is not None:
        if e.tag == "animations":
            return True
        e = e.getparent()
    return False


def color_report(doc: G.GfxDoc) -> dict:
    static, animated = Counter(), Counter()
    for el in doc.root.iter():
        if not isinstance(el.tag, str):
            continue
        anim = _in_animation(el)
        for k, v in el.attrib.items():
            if is_color_attr(k) and v and k not in ("endColor", "fillEndColor", "patternColor") \
                    and not k.startswith(("field", "focus", "interactive")):
                (animated if anim else static)[v] += 1
    return {"static": dict(static.most_common()), "in_animations": dict(animated.most_common())}


def isa101_audit(doc: G.GfxDoc, sat_limit: float = 0.45) -> dict:
    """Flag high-performance HMI violations: saturated colours on static (non-animated)
    graphics, gradients, a non-grey background, blinking used outside alarm-like animations."""
    findings = []
    ds = doc.settings
    if ds is not None and saturation(ds.get("backColor", "")) > 0.15:
        findings.append({"code": "BG_NOT_GREY", "object": "(display)",
                         "message": f"display background {ds.get('backColor')} is not a neutral grey"})
    if ds is not None and to_rgb(ds.get("backColor")) and sum(to_rgb(ds.get("backColor"))) / 3 < 120:
        findings.append({"code": "BG_DARK", "object": "(display)",
                         "message": "dark background; ISA-101 recommends light/medium grey"})
    sat_by_color: dict[str, list] = defaultdict(list)
    gradients = []
    blink = []
    def conditional(el):  # shown only under a condition (alarm indicators, state texts)
        e = el
        while e is not None and e is not doc.root:
            if e.find("animations/animateVisibility") is not None:
                return True
            e = e.getparent()
        return False

    def flagged(c, text=False):  # saturated; dark text colours (e.g. navy values) are tolerated
        rgb = to_rgb(c)
        if not rgb or saturation(c) <= sat_limit:
            return False
        lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        return not (text and lum < 60)

    for obj, depth in doc.iter_objects():
        if obj.tag in G.CONTAINER_TAGS or conditional(obj):
            continue
        anims = obj.find("animations")
        has_color_anim = anims is not None and anims.find("animateColor") is not None
        name = obj.get("name")
        if obj.get("backStyle") == "gradient" or obj.find(".//*[@backStyle='gradient']") is not None:
            gradients.append(name)
        for k in ("backColor", "foreColor"):
            v = obj.get(k)
            if k == "backColor" and obj.get("backStyle") == "transparent":
                continue
            is_text = k == "foreColor" and obj.tag in TEXT_TAGS
            if v and flagged(v, is_text) and not has_color_anim and obj.tag != "activeX":
                sat_by_color[v].append(name)
        for btn in obj.iter("up"):
            for k in ("backColor", "foreColor"):
                v = btn.get(k)
                if v and flagged(v):
                    sat_by_color[v].append(name)
        if anims is not None:
            col = anims.find("animateColor")
            if col is not None and col.find("color[@backBehavior='blink']") is not None \
                    and not ALARM_HINT.search(col.get("expression", "")):
                blink.append(name)
    for c, objs in sorted(sat_by_color.items(), key=lambda x: -len(x[1])):
        findings.append({"code": "SATURATED_STATIC_COLOR", "object": ", ".join(objs[:8]) +
                         (f" (+{len(objs) - 8} more)" if len(objs) > 8 else ""),
                         "message": f"{c} used on {len(objs)} static objects; reserve colour for abnormal states",
                         "color": c, "count": len(objs)})
    if gradients:
        findings.append({"code": "GRADIENT", "object": ", ".join(gradients[:10]),
                         "message": f"{len(gradients)} objects use gradients/3-D shading"})
    if blink:
        findings.append({"code": "BLINK_NON_ALARM", "object": ", ".join(blink[:10]),
                         "message": "blinking colour animation whose expression does not look alarm-related"})
    # Rough 0-100 indicator: background issues -15 each, saturated static objects up to -45,
    # gradients up to -20, non-alarm blinking up to -10.
    n_sat = sum(len(v) for v in sat_by_color.values())
    penalty = (15 * sum(f["code"].startswith("BG_") for f in findings) + min(45, n_sat // 4)
               + min(20, len(gradients) // 3) + min(10, len(blink)))
    return {"score": max(0, 100 - penalty), "saturated_static_objects": n_sat,
            "distinct_saturated_colors": len(sat_by_color), "findings": findings}


def recolor(doc: G.GfxDoc, mapping: dict[str, str], include_animations: bool = False,
            objects: list[str] | None = None) -> list[dict]:
    """Replace colours (case-insensitive; names and #hex treated as the same colour) in colour
    attributes. Static graphics only unless include_animations. Returns the changes."""
    norm = {}
    for k, v in mapping.items():
        key = (NAMED.get(k.lower(), k)).upper()
        norm[key] = v
    scope = None
    if objects:
        scope = set()
        for n in objects:
            scope.update(doc.get(n).iter())
    changes = []
    for el in doc.root.iter():
        if not isinstance(el.tag, str) or (scope is not None and el not in scope):
            continue
        if not include_animations and _in_animation(el):
            continue
        for k, v in list(el.attrib.items()):
            if not v or not is_color_attr(k):
                continue
            key = NAMED.get(v.lower(), v).upper()
            if key in norm and norm[key] != v:
                el.set(k, norm[key])
                owner = G._owner(el, doc)
                changes.append({"object": owner.get("name") if owner is not None else "(display)",
                                "element": el.tag, "attribute": k, "from": v, "to": norm[key]})
    return changes
