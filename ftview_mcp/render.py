"""Static preview of a display: XML -> SVG -> PNG (rasterized by headless Edge).

This is an approximation, not a runtime screenshot. By default it draws the design
view (every object, as View Studio shows it). With ``simulate`` it evaluates
visibility, colour, fill and horizontal-position animations for given tag values.
ActiveX controls and images are drawn as placeholders.
"""
from __future__ import annotations

import copy
import math
import os
import subprocess
import tempfile
from html import escape
from pathlib import Path

from . import expr as E
from . import gfx as G

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _color(v: str | None, default: str = "none") -> str:
    if not v or v in ("true", "false"):
        return default
    return v


def _fill(el) -> str:
    if el.get("backStyle") == "transparent":
        return "none"
    return _color(el.get("backColor"), "none")


def _stroke(el) -> tuple[str, float]:
    if el.get("lineStyle") == "none":
        return "none", 0
    return _color(el.get("foreColor"), "black"), float(el.get("lineWidth", "1") or 1)


def _pts(s: str) -> str:
    v = s.split()
    return " ".join(f"{v[i]},{v[i + 1]}" for i in range(0, len(v) - 1, 2))


def _text(x, y, w, h, caption, el, color, size, align="middleCenter", bold=None, font=None) -> str:
    if not caption:
        return ""
    font = font or el.get("fontFamily") or "Arial"
    bold = el.get("bold") == "true" if bold is None else bold
    vert, horiz = "middle", "center"
    a = align or "middleCenter"
    for k in ("top", "middle", "bottom"):
        if a.lower().startswith(k):
            vert, horiz = k, a[len(k):].lower() or "center"
    anchor = {"left": "start", "center": "middle", "right": "end"}.get(horiz, "middle")
    tx = {"start": x + 2, "middle": x + w / 2, "end": x + w - 2}[anchor]
    lines = caption.replace("\r\n", "\n").split("\n")
    lh = size * 1.15
    total = lh * len(lines)
    if vert == "top":
        ty = y + size
    elif vert == "bottom":
        ty = y + h - total + size
    else:
        ty = y + (h - total) / 2 + size * 0.85
    out = []
    for i, line in enumerate(lines):
        out.append(f'<text x="{tx:.1f}" y="{ty + i * lh:.1f}" fill="{color}" font-family="{escape(font)}" '
                   f'font-size="{size:.1f}" text-anchor="{anchor}"'
                   f'{" font-weight=\"bold\"" if bold else ""}>{escape(line)}</text>')
    return "".join(out)


def _font_px(el, default=12.0) -> float:
    if el.get("fontSize"):
        return float(el.get("fontSize")) * 1.333
    if el.get("charHeight"):
        return float(el.get("charHeight")) * 0.8
    return default


def _eval(expression, sim):
    try:
        return E.evaluate(expression, sim.get("values", {}), sim.get("default", 0))
    except E.ExprError:
        return None


def _simulate(el, sim):
    """Apply animations for a simulated tag state. Returns (hidden, element, fill_fraction, dx)."""
    anims = el.find("animations")
    if sim is None or anims is None:
        return False, el, None, 0
    vis = anims.find("animateVisibility")
    if vis is not None:
        v = _eval(vis.get("expression", ""), sim)
        if v is not None:
            shown = bool(v) if vis.get("expressionTrueState", "visible") == "visible" else not bool(v)
            if not shown:
                return True, el, None, 0
    col = anims.find("animateColor")
    if col is not None:
        v = _eval(col.get("expression", ""), sim)
        if v is not None:
            for entry in col.findall("color"):
                if float(entry.get("value", "nan")) == float(v):
                    el = copy.deepcopy(el)
                    el.set("backColor", entry.get("backColor1"))
                    el.set("foreColor", entry.get("foreColor1"))
                    if el.get("backStyle") == "transparent" and el.tag != "text":
                        el.set("backStyle", "solid")
                    break
    frac = None
    fill = anims.find("animateFill")
    rng = fill.find("constantExpressionRange") if fill is not None else None
    if fill is not None and rng is not None:
        v = _eval(fill.get("expression", ""), sim)
        lo, hi = float(rng.get("min", 0)), float(rng.get("max", 100))
        if isinstance(v, (int, float)) and hi != lo:
            frac = min(1.0, max(0.0, (v - lo) / (hi - lo)))
    dx = 0
    hp = anims.find("animateHorizontalPosition")
    rng = hp.find("constantExpressionRange") if hp is not None else None
    if hp is not None and rng is not None:
        v = _eval(hp.get("expression", ""), sim)
        lo, hi = float(rng.get("min", 0)), float(rng.get("max", 100))
        if isinstance(v, (int, float)) and hi != lo:
            f = min(1.0, max(0.0, (v - lo) / (hi - lo)))
            o0, o1 = float(hp.get("horizontalOffsetMin", 0)), float(hp.get("horizontalOffsetMax", 0))
            dx = o0 + f * (o1 - o0)
    return False, el, frac, dx


def _obj(el, highlight: set[str], show_tags: bool, sim=None) -> str:
    t = el.tag
    name = el.get("name", "")
    if el.get("visible") == "false":
        return ""
    hidden, el, frac, dx = _simulate(el, sim)
    if hidden:
        return ""
    if t == "group":
        inner = "".join(_obj(c, highlight, show_tags, sim) for c in el if G.is_object(c))
        return f'<g data-name="{escape(name)}">{inner}</g>' + _hl(el, highlight)
    try:
        x, y = float(el.get("left", 0)), float(el.get("top", 0))
        w, h = float(el.get("width", 0)), float(el.get("height", 0))
    except ValueError:
        return ""
    fill = _fill(el)
    stroke, sw = _stroke(el)
    s = ""
    if frac is not None and t in ("rectangle", "roundedRectangle"):
        y, h = y + h * (1 - frac), h * frac  # fill direction 'up'
    if t in ("rectangle", "panel"):
        s = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
    elif t == "roundedRectangle":
        r = min(w, h) * 0.15
        s = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r:.1f}" fill="{fill}" stroke="{stroke}" '
             f'stroke-width="{sw}"/>')
    elif t == "ellipse":
        s = (f'<ellipse cx="{x + w / 2}" cy="{y + h / 2}" rx="{w / 2}" ry="{h / 2}" fill="{fill}" '
             f'stroke="{stroke}" stroke-width="{sw}"/>')
    elif t in ("polygon", "freehand") and el.get("path"):
        tag = "polygon" if t == "polygon" else "polyline"
        f = fill if t == "polygon" else "none"
        s = f'<{tag} points="{_pts(el.get("path"))}" fill="{f}" stroke="{stroke}" stroke-width="{sw}"/>'
    elif t == "polyline" and el.get("path"):
        s = f'<polyline points="{_pts(el.get("path"))}" fill="none" stroke="{stroke}" stroke-width="{sw}"/>'
    elif t == "line":
        pts = el.get("line") or f"{x} {y} {x + w} {y + h}"
        s = f'<polyline points="{_pts(pts)}" fill="none" stroke="{stroke}" stroke-width="{max(sw, 1)}"/>'
    elif t == "polyPolygon":
        s = "".join(f'<polygon points="{_pts(p.get("polygon", ""))}" fill="{fill}" stroke="{stroke}" '
                    f'stroke-width="{sw}"/>' for p in el.iter("innerPolygon"))
    elif t in ("arc", "wedge"):
        cx, cy, rx, ry = x + w / 2, y + h / 2, w / 2, h / 2
        a0 = float(el.get("startAngle", 0))
        a1 = float(el.get("endAngle", math.pi))
        p0 = (cx + rx * math.cos(a0), cy - ry * math.sin(a0))
        p1 = (cx + rx * math.cos(a1), cy - ry * math.sin(a1))
        large = 1 if ((a1 - a0) % (2 * math.pi)) > math.pi else 0
        d = f"M{p0[0]:.1f},{p0[1]:.1f} A{rx},{ry} 0 {large} 0 {p1[0]:.1f},{p1[1]:.1f}"
        if t == "wedge":
            d += f" L{cx},{cy} Z"
        s = f'<path d="{d}" fill="{fill if t == "wedge" else "none"}" stroke="{stroke}" stroke-width="{sw}"/>'
    elif t == "text":
        bg = "" if fill == "none" else f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}"/>'
        size = _font_px(el)
        s = bg + _text(x, y, w, h, el.get("caption", ""), el, _color(el.get("foreColor"), "black"), size,
                       el.get("alignment", "middleCenter"))
    elif t in ("button", "momentaryButton", "maintainedButton", "latchedButton", "interlockedButton",
               "multistateButton", "rampButton", "navigationButton", "displayListSelector"):
        up = el.find("up")
        if up is None:
            up = el.find("states/state")
        src = up if up is not None else el
        bg = _color(src.get("backColor"), "#D4D0C8")
        fg = _color(src.get("foreColor"), "black")
        cap = src.find("caption")
        caption = cap.get("caption", "") if cap is not None else ""
        size = float(cap.get("fontSize", 10)) * 1.33 if cap is not None else 12
        bold = cap is not None and cap.get("bold") == "true"
        font = cap.get("fontFamily") if cap is not None else None
        s = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{bg}" stroke="#808080" stroke-width="1"/>'
             + _text(x, y, w, h, caption or (name if t != "button" else ""), el, fg, size, "middleCenter",
                     bold, font))
    elif t in ("numericDisplay", "numericInput", "stringDisplay", "stringInput"):
        conn = el.find("connections/connection[@name='Value']")
        expr = conn.get("expression", "") if conn is not None else ""
        label = expr.strip("{}").split("]")[-1] if show_tags else ("###.#" if "numeric" in t else "abc")
        if sim is not None and not show_tags and expr and "{" in expr:
            v = _eval(expr, sim)
            if isinstance(v, (int, float)):
                label = f"{v:.{int(el.get('decimalPlaces', '0') or 0)}f}" if "numeric" in t else str(v)
            elif isinstance(v, str):
                label = v
        bg = "none" if el.get("backStyle") == "transparent" else _color(el.get("backColor"), "white")
        border = "#808080" if "Input" in t else "none"
        size = _font_px(el) * 0.95
        s = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{bg}" stroke="{border}"/>'
             + _text(x, y, w, h, label, el, _color(el.get("foreColor"), "black"), size,
                     "middle" + (el.get("justification") or "right").capitalize()))
    elif t == "timeAndDateDisplay":
        s = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}"/>'
             + _text(x, y, w, h, "01/01/2026 12:00:00", el, _color(el.get("foreColor"), "black"),
                     _font_px(el) * 0.9))
    else:
        label = el.get("imageName") or el.get("classId") or t
        if t == "activeX":
            label = name
        s = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#F4F4F4" fill-opacity="0.6" '
             f'stroke="#999" stroke-dasharray="4 3"/>'
             + _text(x, y, w, h, f"[{t}] {label}"[:60], el, "#666", min(12, max(8, h / 3)), bold=False,
                     font="Arial"))
    if dx:
        s = f'<g transform="translate({dx:.1f},0)">{s}</g>'
    return s + _hl(el, highlight)


def _hl(el, highlight) -> str:
    if el.get("name") not in highlight:
        return ""
    b = G.bbox(el)
    if b is None:
        return ""
    return (f'<rect x="{b.left - 3}" y="{b.top - 3}" width="{b.width + 6}" height="{b.height + 6}" '
            f'fill="none" stroke="#FF00FF" stroke-width="3" stroke-dasharray="6 3"/>'
            f'<text x="{b.left}" y="{b.top - 6}" fill="#FF00FF" font-family="Arial" font-size="12" '
            f'font-weight="bold">{escape(el.get("name"))}</text>')


def to_svg(doc: G.GfxDoc, highlight: list[str] | None = None, show_tags: bool = False,
           crop: tuple[int, int, int, int] | None = None, simulate: dict | None = None) -> str:
    """simulate=None draws design view (every object, like View Studio). simulate={"values":
    {"P101_RUN": 1, "LIT101": 63.5}, "default": 0} evaluates animations for that tag state."""
    w, h = doc.size
    if not w or not h:
        boxes = [G.bbox(o) for o, d in doc.iter_objects() if d == 0]
        boxes = [b for b in boxes if b]
        w = int(max((b.right for b in boxes), default=800)) + 10
        h = int(max((b.bottom for b in boxes), default=600)) + 10
    bg = _color(doc.settings.get("backColor") if doc.settings is not None else None, "white")
    body = "".join(_obj(o, set(highlight or []), show_tags, simulate) for o in doc.root if G.is_object(o))
    vb = f"0 0 {w} {h}" if not crop else " ".join(map(str, crop))
    vw, vh = (w, h) if not crop else (crop[2], crop[3])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw}" height="{vh}" viewBox="{vb}">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="{bg}"/>{body}</svg>')


def find_edge() -> str:
    for p in EDGE_PATHS:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("Microsoft Edge not found; needed to rasterize previews")


def to_png(svg: str, out: Path, width: int, height: int, timeout: int = 60) -> Path:
    edge = find_edge()
    with tempfile.TemporaryDirectory(prefix="ftview_render_") as td:
        html = Path(td) / "page.html"
        html.write_text("<!doctype html><html><head><meta charset='utf-8'><style>html,body{margin:0;"
                        "padding:0;overflow:hidden;background:#fff}</style></head><body>"
                        + svg + "</body></html>", encoding="utf-8")
        out = Path(out).resolve()
        if out.exists():
            out.unlink()
        cmd = [edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
               "--disable-extensions", f"--user-data-dir={Path(td) / 'profile'}",
               f"--window-size={width},{height}", f"--screenshot={out}", html.as_uri()]
        subprocess.run(cmd, capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not out.is_file():
        raise RuntimeError("Edge did not produce a screenshot")
    return out


def render(doc: G.GfxDoc, out_png: Path, highlight=None, show_tags=False, crop=None, simulate=None) -> Path:
    svg = to_svg(doc, highlight, show_tags, crop, simulate)
    w, h = doc.size if not crop else (crop[2], crop[3])
    if not w or not h:
        w, h = 1920, 1080
    return to_png(svg, out_png, int(w), int(h))
