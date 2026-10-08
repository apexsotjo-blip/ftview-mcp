"""ISA-101 / high-performance HMI object templates.

Design rules applied (overridable through the style dict):
* grey background, dark grey static lines, no gradients or 3-D;
* colour is reserved for abnormal states: alarm indicators carry priority colour
  *and* shape *and* a character, so they do not rely on colour alone;
* equipment state is shown by fill: active (running / open) = dark, inactive =
  light, travelling / unknown = mid grey;
* only abnormal modes are shown (an "H" box when not in auto);
* live values in dark blue, static text in dark grey, units in mid grey.

Each template returns a ``Built``: one group element plus named ports
(``{"out": (x, y, "right")}``) that pipes can connect to.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from . import build as B
from . import gfx as G

HP_STYLE = {
    "bg": "#DDDDDD", "panel": "#E4E4E4", "panel_border": "#B4B4B4", "header": "#C8C8C8",
    "text": "#1E1E1E", "label": "#3C3C3C", "units": "#606060", "value": "#0A2A78",
    "equip_line": "#404040", "active": "#404040", "inactive": "#F4F4F4", "transit": "#A0A0A0",
    "tank_bg": "#F4F4F4", "level": "#8C9DB5", "pipe": "#7F7F7F", "pipe_width": 3,
    "bar_bg": "#F4F4F4", "bar_normal": "#C4C4C4", "pointer": "#1E1E1E",
    "alarm1": "#E00000", "alarm2": "#FF9900", "alarm3": "#F5E000", "alarm_text1": "white",
    "alarm_text2": "black", "alarm_text3": "black",
    "mode_bg": "#404040", "mode_text": "white",
    "button": "#C8C8C8", "button_text": "#1E1E1E", "nav_current": "#7A7A7A", "nav_current_text": "white",
    "font": "Arial",
}


# ---------------------------------------------------------------------------
# Context: names, tag syntax, style
# ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, shortcut: str = "SCADA", style: dict | None = None, taken: set[str] | None = None):
        self.shortcut = shortcut
        self.s = {**HP_STYLE, **(style or {})}
        self.taken: set[str] = set(taken or ())

    def name(self, base: str) -> str:
        n = re.sub(r"[^A-Za-z0-9_]", "_", base)
        if not n or not n[0].isalpha():
            n = "O_" + n
        n = n[:60]
        cand, i = n, 1
        while cand in self.taken:
            i += 1
            cand = f"{n[:57]}_{i}"
        self.taken.add(cand)
        return cand

    def tag(self, t: str) -> str:
        """Tag path for commands: 'P101_RUN' -> '[SCADA]P101_RUN'. '#1' and '[X]..' kept."""
        t = t.strip()
        if t.startswith("{") and t.endswith("}"):
            t = t[1:-1]
        if t.startswith(("[", "#", "::", "/")) or "::" in t or not self.shortcut:
            return t
        return f"[{self.shortcut}]{t}"

    def ref(self, t: str) -> str:
        """Expression operand: '{[SCADA]P101_RUN}'. A value starting with '=' is a raw expression."""
        t = t.strip()
        if t.startswith("="):
            return t[1:].strip()
        return "{" + self.tag(t) + "}"


@dataclass
class Built:
    element: etree._Element
    ports: dict = field(default_factory=dict)

    def box(self):
        return G.bbox(self.element)


def _scaled(ctx, built: Built, x, y, size: float) -> Built:
    if size and size != 1:
        G.scale(built.element, size, size, x, y)
        built.ports = {k: (x + (px - x) * size, y + (py - y) * size, d) for k, (px, py, d) in built.ports.items()}
    return built


def _touch(ctx, faceplate: dict | str | None):
    if not faceplate:
        return None
    if isinstance(faceplate, str):
        return B.touch_anim(faceplate if faceplate.lower().startswith(("display", "&", "abort")) else
                            f'Display "{faceplate}"')
    disp = faceplate["display"]
    params = ",".join(ctx.tag(t) for t in faceplate.get("tags", []))
    cmd = f'Display "{disp}"' + (f" /T{params}" if params else "")
    return B.touch_anim(cmd)


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def alarm_indicator(ctx: Ctx, id: str, x, y, expr: str, priority: int = 1, text: str | None = None,
                    size: int = 16, **_) -> Built:
    """Priority-coded alarm indicator, visible only while `expr` is true.
    P1 red square, P2 orange triangle, P3 yellow inverted triangle; each with a character."""
    s = ctx.s
    p = max(1, min(3, int(priority)))
    fill, tc = s[f"alarm{p}"], s[f"alarm_text{p}"]
    n = ctx.name(f"{id}_Alm")
    if p == 1:
        shape = B.shape("rectangle", ctx.name(f"{n}_Shp"), x, y, size, size, fill, "#1E1E1E", 1)
        ty = y
    elif p == 2:
        shape = B.polygon(ctx.name(f"{n}_Shp"), [(x + size / 2, y), (x + size, y + size), (x, y + size)],
                          fill, "#1E1E1E", 1)
        ty = y + size * 0.2
    else:
        shape = B.polygon(ctx.name(f"{n}_Shp"), [(x, y), (x + size, y), (x + size / 2, y + size)],
                          fill, "#1E1E1E", 1)
        ty = y - size * 0.2
    t = B.text(ctx.name(f"{n}_Txt"), x, ty, size, size, text or str(p), tc, max(7, size * 0.5), True,
               font=s["font"])
    g = B.group(n, [shape, t], tooltip=f"{id} alarm (priority {p})", anims=[B.visibility_anim(ctx.ref(expr))])
    return Built(g)


def mode_indicator(ctx: Ctx, id: str, x, y, expr: str, text: str = "H", size: int = 14, **_) -> Built:
    """Small dark box with a letter, visible when `expr` is true (e.g. not in auto)."""
    s = ctx.s
    n = ctx.name(f"{id}_Mode")
    r = B.shape("rectangle", ctx.name(f"{n}_Box"), x, y, size, size, s["mode_bg"], s["mode_bg"], 1)
    t = B.text(ctx.name(f"{n}_Txt"), x, y, size, size, text, s["mode_text"], max(7, size * 0.55), True,
               font=s["font"])
    return Built(B.group(n, [r, t], tooltip=f"{id} not in auto",
                         anims=[B.visibility_anim(_expr(ctx, expr))]))


def _expr(ctx, e: str) -> str:
    """Accept 'TAG', '=raw expression', or 'NOT TAG'."""
    e = e.strip()
    if e.upper().startswith("NOT "):
        return "NOT " + ctx.ref(e[4:])
    return ctx.ref(e)


def _state_expr(ctx, tags: dict, active_key: str) -> str | None:
    """0 = inactive, 1 = active, 2 = transit (valves with both limit switches)."""
    a = tags.get(active_key)
    closed = tags.get("closed")
    if a and closed:
        return f"If {ctx.ref(a)} Then 1 Else If {ctx.ref(closed)} Then 0 Else 2"
    if a:
        return f"If {ctx.ref(a)} Then 1 Else 0"
    return None


def _state_colors(ctx, expr, line):
    s = ctx.s
    if not expr:
        return None
    return B.color_anim(expr, [(0, s["inactive"], line), (1, s["active"], line), (2, s["transit"], line)])


def _equipment_extras(ctx, id, x, y, w, tags, label, children, label_y):
    s = ctx.s
    if label:
        children.append(B.text(ctx.name(f"{id}_Lbl"), x - 30, label_y, w + 60, 16, label, s["label"], 9, True,
                               font=s["font"]))
    if tags.get("fault"):
        children.append(alarm_indicator(ctx, id, x + w + 2, y, tags["fault"], 1, "F").element)
    if tags.get("auto"):
        children.append(mode_indicator(ctx, id, x - 16, y, "NOT " + tags["auto"]).element)
    elif tags.get("hand"):
        children.append(mode_indicator(ctx, id, x - 16, y, tags["hand"]).element)


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------

def pump(ctx: Ctx, id: str, x, y, label: str | None = None, tags: dict | None = None, size: float = 1.0,
         faceplate=None, **_) -> Built:
    """Centrifugal pump. tags: run, fault, auto | hand. Ports: in (left), out (nozzle, right)."""
    tags, s = tags or {}, ctx.s
    line = s["equip_line"]
    expr = _state_expr(ctx, tags, "run")
    base = B.polygon(ctx.name(f"{id}_Base"), [(x + 8, y + 50), (x + 32, y + 50), (x + 20, y + 36)], line, line, 1)
    nozzle = B.shape("rectangle", ctx.name(f"{id}_Noz"), x + 20, y, 26, 10, s["inactive"], line, 2)
    body = B.shape("ellipse", ctx.name(f"{id}_Body"), x, y, 40, 40, s["inactive"], line, 2)
    for el in (nozzle, body):
        B.add_animations(el, [_state_colors(ctx, expr, line)])
    children = [base, nozzle, body]
    _equipment_extras(ctx, id, x, y, 46, tags, label, children, y + 52)
    g = B.group(ctx.name(id), children, tooltip=label or id, anims=[_touch(ctx, faceplate)])
    return _scaled(ctx, Built(g, {"in": (x, y + 20, "left"), "out": (x + 46, y + 5, "right")}), x, y, size)


def blower(ctx: Ctx, id: str, x, y, label: str | None = None, tags: dict | None = None, size: float = 1.0,
           faceplate=None, **_) -> Built:
    """Blower / compressor. tags: run, fault, auto | hand. Ports: in (left), out (top)."""
    tags, s = tags or {}, ctx.s
    line = s["equip_line"]
    expr = _state_expr(ctx, tags, "run")
    base = B.polygon(ctx.name(f"{id}_Base"), [(x + 4, y + 62), (x + 40, y + 62), (x + 22, y + 44)], line, line, 1)
    outlet = B.shape("rectangle", ctx.name(f"{id}_Out"), x + 26, y, 14, 22, s["inactive"], line, 2)
    body = B.shape("ellipse", ctx.name(f"{id}_Body"), x, y + 12, 44, 44, s["inactive"], line, 2)
    for el in (outlet, body):
        B.add_animations(el, [_state_colors(ctx, expr, line)])
    children = [base, outlet, body]
    _equipment_extras(ctx, id, x, y + 12, 44, tags, label, children, y + 64)
    g = B.group(ctx.name(id), children, tooltip=label or id, anims=[_touch(ctx, faceplate)])
    return _scaled(ctx, Built(g, {"in": (x, y + 34, "left"), "out": (x + 33, y, "up")}), x, y, size)


def mixer(ctx: Ctx, id: str, x, y, label: str | None = None, tags: dict | None = None, size: float = 1.0,
          faceplate=None, shaft: int = 60, **_) -> Built:
    """Top-entry mixer/agitator (motor + shaft + impeller). tags: run, fault, auto | hand."""
    tags, s = tags or {}, ctx.s
    line = s["equip_line"]
    expr = _state_expr(ctx, tags, "run")
    motor = B.shape("rectangle", ctx.name(f"{id}_Mot"), x + 8, y, 24, 18, s["inactive"], line, 2)
    B.add_animations(motor, [_state_colors(ctx, expr, line)])
    sh = B.line(ctx.name(f"{id}_Shaft"), x + 20, y + 18, x + 20, y + 18 + shaft, line, 2)
    yb = y + 18 + shaft
    blade_l = B.polygon(ctx.name(f"{id}_BladeL"), [(x + 20, yb), (x + 4, yb - 6), (x + 4, yb + 6)], line, line, 1)
    blade_r = B.polygon(ctx.name(f"{id}_BladeR"), [(x + 20, yb), (x + 36, yb - 6), (x + 36, yb + 6)], line, line, 1)
    children = [sh, blade_l, blade_r, motor]
    _equipment_extras(ctx, id, x + 8, y, 24, tags, None, children, y)
    if label:
        children.append(B.text(ctx.name(f"{id}_Lbl"), x + 54, y + 2, 90, 16, label, s["label"], 9, True,
                               align="middleLeft", font=s["font"]))
    g = B.group(ctx.name(id), children, tooltip=label or id, anims=[_touch(ctx, faceplate)])
    return _scaled(ctx, Built(g, {}), x, y, size)


def motor(ctx: Ctx, id: str, x, y, label: str | None = None, tags: dict | None = None, size: float = 1.0,
          faceplate=None, **_) -> Built:
    """Generic motor: circle with 'M'. tags: run, fault, auto | hand."""
    tags, s = tags or {}, ctx.s
    line = s["equip_line"]
    expr = _state_expr(ctx, tags, "run")
    body = B.shape("ellipse", ctx.name(f"{id}_Body"), x, y, 36, 36, s["inactive"], line, 2)
    B.add_animations(body, [_state_colors(ctx, expr, line)])
    m = B.text(ctx.name(f"{id}_M"), x, y, 36, 36, "M", s["transit"], 12, True, font=s["font"])
    children = [body, m]
    _equipment_extras(ctx, id, x, y, 36, tags, label, children, y + 38)
    g = B.group(ctx.name(id), children, tooltip=label or id, anims=[_touch(ctx, faceplate)])
    return _scaled(ctx, Built(g, {"in": (x, y + 18, "left"), "out": (x + 36, y + 18, "right")}), x, y, size)


def valve(ctx: Ctx, id: str, x, y, label: str | None = None, tags: dict | None = None, size: float = 1.0,
          vertical: bool = False, actuator: bool = True, faceplate=None, **_) -> Built:
    """On/off valve (bow-tie). tags: open (ZSO), closed (ZSC), fault, auto | hand.
    open+closed -> transit state (mid grey) when neither is made. Ports: in, out."""
    tags, s = tags or {}, ctx.s
    line = s["equip_line"]
    expr = _state_expr(ctx, tags, "open")
    W, H = 30, 20

    def P(px, py):  # rotate 90 degrees for vertical valves
        return (x + py, y + px) if vertical else (x + px, y + py)

    body = B.polygon(ctx.name(f"{id}_Body"), [P(0, 0), P(15, 10), P(30, 0), P(30, 20), P(15, 10), P(0, 20)],
                     s["inactive"], line, 2)
    B.add_animations(body, [_state_colors(ctx, expr, line)])
    children = [body]
    if actuator:
        if vertical:
            children.append(B.line(ctx.name(f"{id}_Stem"), x + 10, y + 15, x - 6, y + 15, line, 2))
            children.append(B.shape("rectangle", ctx.name(f"{id}_Act"), x - 14, y + 9, 8, 12, line, line, 1))
        else:
            children.append(B.line(ctx.name(f"{id}_Stem"), x + 15, y + 10, x + 15, y - 6, line, 2))
            children.append(B.shape("rectangle", ctx.name(f"{id}_Act"), x + 9, y - 14, 12, 8, line, line, 1))
    w = H if vertical else W
    _equipment_extras(ctx, id, x, y, w, tags, label, children, y + (W if vertical else H) + 2)
    g = B.group(ctx.name(id), children, tooltip=label or id, anims=[_touch(ctx, faceplate)])
    ports = ({"in": (x + 10, y, "up"), "out": (x + 10, y + 30, "down")} if vertical else
             {"in": (x, y + 10, "left"), "out": (x + 30, y + 10, "right")})
    return _scaled(ctx, Built(g, ports), x, y, size)


def tank(ctx: Ctx, id: str, x, y, w: int = 120, h: int = 160, label: str | None = None,
         tags: dict | None = None, range: tuple = (0, 100), units: str = "%", decimals: int = 1,
         show_value: bool = True, faceplate=None, **_) -> Built:
    """Vertical tank with level fill. tags: level, hihi, hi, lo, lolo (alarm bits).
    Ports: top, bottom, left, right (use 'TK.top@0.25' for a fraction along the side)."""
    tags, s = tags or {}, ctx.s
    line = s["equip_line"]
    shell = B.shape("rectangle", ctx.name(f"{id}_Shell"), x, y, w, h, s["tank_bg"], line, 2)
    children = [shell]
    if tags.get("level"):
        fill = B.shape("rectangle", ctx.name(f"{id}_Level"), x + 3, y + 3, w - 6, h - 6, s["level"], s["level"], 1,
                       line_style="none")
        B.add_animations(fill, [B.fill_anim(ctx.ref(tags["level"]), range[0], range[1], "up")])
        children.append(fill)
        if show_value:
            children.append(B.numeric_display(ctx.name(f"{id}_PV"), x + w / 2 - 40, y + 8, 56, 20,
                                              ctx.ref(tags["level"]), decimals, s["value"], 11, True,
                                              font=s["font"], field_length=6))
            children.append(B.text(ctx.name(f"{id}_EU"), x + w / 2 + 18, y + 8, 30, 20, units, s["units"], 9,
                                   align="middleLeft", font=s["font"]))
    for key, prio, yy in (("hihi", 1, y), ("hi", 2, y + 18), ("lo", 2, y + h - 34), ("lolo", 1, y + h - 16)):
        if tags.get(key):
            children.append(alarm_indicator(ctx, f"{id}_{key}", x + w + 4, yy, tags[key], prio,
                                            {"hihi": "HH", "hi": "H", "lo": "L", "lolo": "LL"}[key]).element)
    if label:
        children.append(B.text(ctx.name(f"{id}_Lbl"), x, y + h + 2, w, 16, label, s["label"], 9, True,
                               font=s["font"]))
    g = B.group(ctx.name(id), children, tooltip=label or id, anims=[_touch(ctx, faceplate)])
    ports = {"top": (x + w / 2, y, "up"), "bottom": (x + w / 2, y + h, "down"),
             "left": (x, y + h / 2, "left"), "right": (x + w, y + h / 2, "right")}
    return Built(g, ports)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

def analog(ctx: Ctx, id: str, x, y, tag: str, label: str | None = None, units: str = "", decimals: int = 1,
           w: int = 120, alarm: str | None = None, alarm_priority: int = 2, bar: dict | None = None,
           faceplate=None, **_) -> Built:
    """Analog value: label, value, units, optional alarm indicator, optional ISA-101 moving
    analog indicator bar: bar={"min":0,"max":10,"lo":2,"hi":8} (normal band lo..hi shaded)."""
    s = ctx.s
    children = []
    yy = y
    if label:
        children.append(B.text(ctx.name(f"{id}_Lbl"), x, y, w, 14, label, s["label"], 8, True,
                               align="middleLeft", font=s["font"]))
        yy += 15
    children.append(B.shape("rectangle", ctx.name(f"{id}_Fld"), x, yy, w - 40, 22, "#ECECEC", "#B4B4B4", 1))
    children.append(B.numeric_display(ctx.name(f"{id}_PV"), x + 2, yy + 1, w - 44, 20, ctx.ref(tag), decimals,
                                      s["value"], 11, True, font=s["font"], field_length=8))
    children.append(B.text(ctx.name(f"{id}_EU"), x + w - 37, yy, 37, 22, units, s["units"], 9,
                           align="middleLeft", font=s["font"]))
    if alarm:
        children.append(alarm_indicator(ctx, id, x + w + 2, yy + 3, alarm, alarm_priority).element)
    yy += 24
    if bar:
        lo_v, hi_v = float(bar.get("min", 0)), float(bar.get("max", 100))
        span = (hi_v - lo_v) or 1
        bw = w - 40
        children.append(B.shape("rectangle", ctx.name(f"{id}_Bar"), x, yy + 4, bw, 8, s["bar_bg"], "#A0A0A0", 1))
        if "lo" in bar and "hi" in bar:
            nl = x + bw * (float(bar["lo"]) - lo_v) / span
            nh = x + bw * (float(bar["hi"]) - lo_v) / span
            children.append(B.shape("rectangle", ctx.name(f"{id}_Norm"), nl, yy + 5, max(1, nh - nl), 6,
                                    s["bar_normal"], s["bar_normal"], 1, line_style="none"))
        ptr = B.polygon(ctx.name(f"{id}_Ptr"), [(x - 4, yy), (x + 4, yy), (x, yy + 8)], s["pointer"], s["pointer"], 1)
        B.add_animations(ptr, [B.hpos_anim(ctx.ref(tag), lo_v, hi_v, bw)])
        children.append(ptr)
        yy += 14
    g = B.group(ctx.name(id), children, tooltip=label or id, anims=[_touch(ctx, faceplate)])
    return Built(g, {"left": (x, y + 26, "left"), "right": (x + w - 40, y + 26, "right")})


def setpoint(ctx: Ctx, id: str, x, y, tag: str, label: str | None = None, units: str = "", decimals: int = 1,
             w: int = 160, security: str = "*", confirm: bool = False, **_) -> Built:
    """Operator entry: label, numeric input, units."""
    s = ctx.s
    children = []
    lw = 0
    if label:
        lw = w - 110
        children.append(B.text(ctx.name(f"{id}_Lbl"), x, y, lw, 22, label, s["label"], 9, True,
                               align="middleLeft", font=s["font"]))
    children.append(B.numeric_input(ctx.name(f"{id}_In"), x + lw, y, 70, 22, ctx.ref(tag), decimals, 10,
                                    s["font"], security=security, confirm=confirm))
    children.append(B.text(ctx.name(f"{id}_EU"), x + lw + 72, y, 38, 22, units, s["units"], 9,
                           align="middleLeft", font=s["font"]))
    return Built(B.group(ctx.name(id), children, tooltip=label or id))


def status_text(ctx: Ctx, id: str, x, y, states: list, default: dict | None = None, w: int = 100, h: int = 18,
                size: int = 9, **_) -> Built:
    """Text that changes with state. states: [{"when": "TAG" | "=expr", "text": "FAULT",
    "color": "#E00000"}...] (first match wins); default: {"text": "STOPPED"} when none match."""
    s = ctx.s
    conds = [_expr(ctx, st["when"]) for st in states]
    children = []
    for i, st in enumerate(states):
        # Never emit an expression that is only "(tag)": View's importer corrupts it to "{({{tag}})}".
        e = conds[i] if i == 0 else f"({conds[i]})" + "".join(f" AND NOT ({c})" for c in conds[:i])
        t = B.text(ctx.name(f"{id}_S{i}"), x, y, w, h, st["text"], st.get("color", s["text"]), size, True,
                   font=s["font"])
        B.add_animations(t, [B.visibility_anim(e)])
        children.append(t)
    if default:
        e = "NOT (" + " OR ".join(f"({c})" for c in conds) + ")"
        t = B.text(ctx.name(f"{id}_Def"), x, y, w, h, default["text"], default.get("color", s["label"]), size, True,
                   font=s["font"])
        B.add_animations(t, [B.visibility_anim(e)])
        children.append(t)
    return Built(B.group(ctx.name(id), children))


def command_button(ctx: Ctx, id: str, x, y, caption: str, action: str = "set", tag: str | None = None,
                   value=1, display: str | None = None, w: int = 90, h: int = 30, confirm: str | None = None,
                   enable: str | None = None, **_) -> Built:
    """action: set (&Set tag value) | momentary (Set 1 on press, 0 on release) | toggle |
    display (Display "name") | command (raw FT View command in `tag`)."""
    s = ctx.s
    press = release = ""
    if action == "set":
        press = f"&Set {ctx.tag(tag)} {value}"
    elif action == "momentary":
        press, release = f"Set {ctx.tag(tag)} 1", f"Set {ctx.tag(tag)} 0"
    elif action == "toggle":
        press = f"Toggle {ctx.tag(tag)}"
    elif action == "display":
        press = f'Display "{display}"'
    elif action == "command":
        press = tag or ""
    else:
        raise ValueError(f"unknown button action {action!r}")
    el = B.button(ctx.name(id), x, y, w, h, caption, press, release, back=s["button"], fore=s["button_text"],
                  font=s["font"], confirm=confirm, enable_expr=_expr(ctx, enable) if enable else "")
    return Built(el)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def label(ctx: Ctx, id: str, x, y, text: str, w: int = 160, h: int = 18, size: int = 9, bold: bool = False,
          align: str = "middleLeft", color: str | None = None, **_) -> Built:
    s = ctx.s
    return Built(B.text(ctx.name(id), x, y, w, h, text, color or s["label"], size, bold, align, font=s["font"]))


def panel(ctx: Ctx, id: str, x, y, w: int, h: int, title: str | None = None, **_) -> Built:
    """Background grouping box (slightly lighter than the display background)."""
    s = ctx.s
    children = [B.shape("rectangle", ctx.name(f"{id}_Box"), x, y, w, h, s["panel"], s["panel_border"], 1)]
    if title:
        children.append(B.text(ctx.name(f"{id}_Title"), x + 6, y + 2, w - 12, 18, title, s["label"], 9, True,
                                align="middleLeft", font=s["font"]))
    return Built(B.group(ctx.name(id), children))


def pipe(ctx: Ctx, id: str, points, width: int | None = None, color: str | None = None, arrow: bool = False,
         dashed: bool = False, **_) -> Built:
    s = ctx.s
    width = width or s["pipe_width"]
    color = color or s["pipe"]
    pts = _dedupe(points)
    if len(pts) == 2 and not dashed:
        # View turns a 2-point polyline into a <line> and resets its width to 1;
        # B.line emits a filled rectangle for thick straight runs instead.
        (x1, y1), (x2, y2) = pts
        children = [B.line(ctx.name(f"{id}_Line"), x1, y1, x2, y2, color, width)]
    else:
        children = [B.polyline(ctx.name(f"{id}_Line"), pts, color, width, "dash" if dashed else "solid")]
    if arrow and len(pts) >= 2:
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        a = 6 + width
        if abs(x2 - x1) >= abs(y2 - y1):
            d = 1 if x2 > x1 else -1
            tri = [(x2, y2), (x2 - d * a, y2 - a / 2), (x2 - d * a, y2 + a / 2)]
        else:
            d = 1 if y2 > y1 else -1
            tri = [(x2, y2), (x2 - a / 2, y2 - d * a), (x2 + a / 2, y2 - d * a)]
        children.append(B.polygon(ctx.name(f"{id}_Arrow"), tri, color, color, 1))
    if len(children) == 1:
        return Built(children[0])
    return Built(B.group(ctx.name(id), children))


def _dedupe(points):
    out = []
    for p in points:
        p = (round(p[0]), round(p[1]))
        if not out or out[-1] != p:
            out.append(p)
    # drop collinear middle points
    i = 1
    while i < len(out) - 1:
        (ax, ay), (bx, by), (cx, cy) = out[i - 1], out[i], out[i + 1]
        if (ax == bx == cx) or (ay == by == cy):
            out.pop(i)
        else:
            i += 1
    return out


def header(ctx: Ctx, id: str, width: int, title: str, h: int = 50, user: bool = True, **_) -> Built:
    """Title bar: display title (left), current user and date/time (right)."""
    s = ctx.s
    children = [B.shape("rectangle", ctx.name(f"{id}_Bar"), 0, 0, width, h, s["header"], s["header"], 1),
                B.text(ctx.name(f"{id}_Title"), 16, 0, width // 2, h, title, s["text"], 16, True,
                       align="middleLeft", font=s["font"]),
                B.time_date(ctx.name(f"{id}_Time"), width - 230, 8, 214, h - 16, s["text"], s["header"])]
    if user:
        children.append(B.string_display(ctx.name(f"{id}_User"), width - 470, 8, 230, h - 16,
                                         "CurrentUserName(  )", s["label"], 10, s["font"], justify="right"))
    return Built(B.group(ctx.name(id), children))


def nav_bar(ctx: Ctx, id: str, x, y, width: int, displays: list, current: str | None = None, h: int = 40,
            gap: int = 4, **_) -> Built:
    """Row of navigation buttons. displays: ["NAME", ...] or [{"display": "NAME", "caption": "..."}]."""
    s = ctx.s
    items = [d if isinstance(d, dict) else {"display": d} for d in displays]
    n = max(1, len(items))
    bw = min(260, (width - gap * (n - 1)) / n)  # few items: normal-sized buttons, left-aligned
    children = []
    for i, it in enumerate(items):
        cur = current and it["display"].lower() == current.lower()
        children.append(B.button(ctx.name(f"{id}_{it['display']}"), x + i * (bw + gap), y, bw, h,
                                 it.get("caption", it["display"]), f'Display "{it["display"]}"',
                                 back=s["nav_current"] if cur else s["button"],
                                 fore=s["nav_current_text"] if cur else s["button_text"], font=s["font"],
                                 tab_index=i + 1))
    return Built(B.group(ctx.name(id), children))


TEMPLATES = {
    "pump": pump, "blower": blower, "mixer": mixer, "motor": motor, "valve": valve, "tank": tank,
    "analog": analog, "setpoint": setpoint, "status": status_text, "button": command_button,
    "alarm": alarm_indicator, "mode": mode_indicator, "label": label, "panel": panel, "pipe": pipe,
    "header": header, "nav": nav_bar,
}


TAG_ROLES = {
    "pump": {"run", "fault", "auto", "hand"}, "blower": {"run", "fault", "auto", "hand"},
    "mixer": {"run", "fault", "auto", "hand"}, "motor": {"run", "fault", "auto", "hand"},
    "valve": {"open", "closed", "fault", "auto", "hand"},
    "tank": {"level", "hihi", "hi", "lo", "lolo"},
}
NESTED_KEYS = {"bar": {"min", "max", "lo", "hi"}, "faceplate": {"display", "tags"}}
STATE_KEYS = {"when", "text", "color"}


def _check_keys(kind: str, what: str, given, allowed: set) -> None:
    unknown = sorted(set(given) - allowed)
    if unknown:
        raise ValueError(f"{kind}: unknown key(s) {unknown} in '{what}'; allowed: {sorted(allowed)}")


def check_params(kind: str, params: dict) -> None:
    """Reject unknown or missing parameters, including keys inside 'tags', 'bar', 'faceplate'
    and status 'states' (templates accept **_ internally, so a typo such as 'colour' or a tag
    role 'runn' would otherwise be silently ignored)."""
    import inspect
    if kind not in TEMPLATES:
        raise ValueError(f"unknown template {kind!r}; known: {sorted(TEMPLATES)}")
    if isinstance(params.get("tags"), dict) and kind in TAG_ROLES:
        _check_keys(kind, "tags", params["tags"], TAG_ROLES[kind])
    for key, allowed in NESTED_KEYS.items():
        if isinstance(params.get(key), dict):
            _check_keys(kind, key, params[key], allowed)
    for i, st in enumerate(params.get("states") or []):
        _check_keys(kind, f"states[{i}]", st, STATE_KEYS)
    if isinstance(params.get("default"), dict):
        _check_keys(kind, "default", params["default"], {"text", "color"})
    sig = inspect.signature(TEMPLATES[kind])
    allowed = {p for p in sig.parameters if p not in ("ctx", "_", "id")}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"{kind}: unknown parameter(s) {unknown}; allowed: {sorted(allowed)}")
    missing = sorted(p for p, v in sig.parameters.items()
                     if v.default is inspect.Parameter.empty and v.kind == v.POSITIONAL_OR_KEYWORD
                     and p not in ("ctx", "id") and p not in params)
    if missing:
        raise ValueError(f"{kind}: missing required parameter(s) {missing}")


def catalog() -> dict:
    import inspect
    out = {}
    for k, f in TEMPLATES.items():
        sig = inspect.signature(f)
        params = {p: (None if v.default is inspect.Parameter.empty else v.default)
                  for p, v in sig.parameters.items() if p not in ("ctx", "_")}
        out[k] = {"doc": (f.__doc__ or "").strip(), "params": params}
        if k in TAG_ROLES:
            out[k]["tag_roles"] = sorted(TAG_ROLES[k])
    return out
