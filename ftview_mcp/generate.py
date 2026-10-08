"""Generate displays and faceplates from a JSON spec using the ISA-101 templates.

Spec (all coordinates in pixels, origin top-left)::

    {
      "name": "EQUALIZATION",              # display name (used for nav highlighting)
      "size": [1920, 1080],
      "shortcut": "SCADA",                  # device shortcut prefixed to bare tag names
      "style": {"pipe": "#707070"},         # optional overrides of templates.HP_STYLE
      "header": {"title": "Equalization"},
      "nav": {"displays": ["OVERVIEW", "EQUALIZATION"], "y": 1030},
      "objects": [
        {"type": "tank", "id": "TK101", "x": 600, "y": 300, "w": 140, "h": 180, "label": "TK-101",
         "tags": {"level": "LIT101_PV", "hi": "LSH101"}, "range": [0, 100], "units": "%"},
        {"type": "pump", "id": "P101A", "x": 820, "y": 430, "label": "P-101A",
         "tags": {"run": "P101A_RUN", "fault": "P101A_TRIP", "auto": "P101A_AUTO"},
         "faceplate": {"display": "FP_MOTOR", "tags": ["P101A_RUN", "P101A_TRIP", "P101A_AUTO", "P101A_CMD"]}},
        {"type": "pipe", "id": "L1", "from": "TK101.bottom", "to": "P101A.in"},
        {"type": "pipe", "id": "L2", "points": [[866, 435], [1000, 435]], "arrow": true}
      ]
    }

Tags: bare names get [shortcut]; "[X]tag" is kept; "=expression" is used verbatim.
Pipes: "from"/"to" are ports "ID.port" or "ID.port@0.25" (fraction along a tank side) or [x, y];
routing is orthogonal and chosen from the port directions, or give "via": [[x, y], ...].
"""
from __future__ import annotations

from . import build as B
from . import gfx as G
from . import templates as T

_PORT_SIDES = {"top", "bottom", "left", "right"}


class SpecError(ValueError):
    pass


def _resolve_port(ref, ports: dict, boxes: dict):
    if isinstance(ref, (list, tuple)):
        return (float(ref[0]), float(ref[1]), None)
    if not isinstance(ref, str) or "." not in ref:
        raise SpecError(f"bad port reference {ref!r}; use 'ID.port' or [x, y]")
    oid, port = ref.split(".", 1)
    frac = None
    if "@" in port:
        port, f = port.split("@", 1)
        frac = float(f)
    if oid not in ports:
        raise SpecError(f"pipe refers to unknown object {oid!r}")
    p = ports[oid]
    if frac is not None and port in _PORT_SIDES and oid in boxes:
        b = boxes[oid]
        if port == "top":
            return (b.left + b.width * frac, p["top"][1], "up")
        if port == "bottom":
            return (b.left + b.width * frac, p["bottom"][1], "down")
        if port == "left":
            return (p["left"][0], b.top + b.height * frac, "left")
        return (p["right"][0], b.top + b.height * frac, "right")
    if port not in p:
        raise SpecError(f"{oid} has no port {port!r}; available: {sorted(p)}")
    return p[port]


_DIRS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}


def route(a, b, stub: int = 20) -> list:
    """Orthogonal route that leaves and enters each port with a straight stub in the port's
    direction, so a pipe never cuts back through the equipment it connects to."""
    (x1, y1, d1), (x2, y2, d2) = a, b
    if d1 in _DIRS or d2 in _DIRS:
        sx, sy = (x1 + _DIRS[d1][0] * stub, y1 + _DIRS[d1][1] * stub) if d1 in _DIRS else (x1, y1)
        ex, ey = (x2 + _DIRS[d2][0] * stub, y2 + _DIRS[d2][1] * stub) if d2 in _DIRS else (x2, y2)
        mid = _best_core((sx, sy), (ex, ey), d1 if d1 in _DIRS else None,
                         _opposite(d2) if d2 in _DIRS else None)
        pts = [(x1, y1)] + mid + [(x2, y2)]
        return _simplify(pts)
    return _route_core(a, b)


def _seg_dir(p, q):
    if p[0] == q[0] and p[1] == q[1]:
        return None
    if p[1] == q[1]:
        return "right" if q[0] > p[0] else "left"
    return "down" if q[1] > p[1] else "up"


def _best_core(s, e, depart, arrive):
    """Shortest orthogonal path s->e whose first segment does not reverse the departure stub and
    whose last segment does not reverse the arrival direction (so it never doubles back through
    the equipment at either end)."""
    (sx, sy), (ex, ey) = s, e
    mx, my = (sx + ex) / 2, (sy + ey) / 2
    cands = [[s, (ex, sy), e], [s, (sx, ey), e], [s, (mx, sy), (mx, ey), e], [s, (sx, my), (ex, my), e]]

    def ok(path):
        segs = [d for d in (_seg_dir(p, q) for p, q in zip(path, path[1:])) if d]
        if not segs:
            return True
        if depart and segs[0] == _opposite(depart):
            return False
        if arrive and segs[-1] == _opposite(arrive):
            return False
        return True

    def length(path):
        return sum(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in zip(path, path[1:]))

    good = [c for c in cands if ok(c)]
    return min(good or cands, key=lambda c: (length(c), len(c)))


def _opposite(d):
    return {"left": "right", "right": "left", "up": "down", "down": "up"}.get(d, d)


def _simplify(pts):
    out = []
    for p in pts:
        p = (round(p[0]), round(p[1]))
        if not out or out[-1] != p:
            out.append(p)
    i = 1
    while i < len(out) - 1:
        (ax, ay), (bx, by), (cx, cy) = out[i - 1], out[i], out[i + 1]
        if (ax == bx == cx) or (ay == by == cy):
            out.pop(i)
        else:
            i += 1
    return out


def _route_core(a, b) -> list:
    (x1, y1, d1), (x2, y2, d2) = a, b
    h1 = d1 in ("left", "right") or d1 is None
    h2 = d2 in ("left", "right") or (d2 is None and h1)
    if h1 and h2:
        mx = (x1 + x2) / 2
        return [(x1, y1), (mx, y1), (mx, y2), (x2, y2)]
    if h1 and not h2:
        return [(x1, y1), (x2, y1), (x2, y2)]
    if not h1 and h2:
        return [(x1, y1), (x1, y2), (x2, y2)]
    my = (y1 + y2) / 2
    return [(x1, y1), (x1, my), (x2, my), (x2, y2)]


def generate(spec: dict) -> tuple[G.GfxDoc, dict]:
    w, h = spec.get("size", [1920, 1080])
    ctx = T.Ctx(spec.get("shortcut", "SCADA"), spec.get("style"))
    s = ctx.s
    doc = G.GfxDoc.new(int(w), int(h), backColor=s["bg"], displayType=spec.get("display_type", "replace"),
                       titleBar="false" if not spec.get("title_bar") else "true",
                       titleBarText=spec.get("title_bar", ""))
    layers: dict[str, list] = {"panel": [], "pipe": [], "main": [], "top": []}
    ports: dict[str, dict] = {}
    boxes: dict = {}
    ids: dict[str, str] = {}
    objs = spec.get("objects", [])
    pipes = []
    for i, o in enumerate(objs):
        o = dict(o)
        typ = o.pop("type", None)
        if typ not in T.TEMPLATES:
            raise SpecError(f"objects[{i}]: unknown type {typ!r}; known: {sorted(T.TEMPLATES)}")
        oid = o.pop("id", None) or f"{typ}{i + 1}"
        if oid in ids:
            raise SpecError(f"duplicate object id {oid!r}")
        ids[oid] = ""
        if typ == "pipe" and ("from" in o or "to" in o):
            rest = {k: v for k, v in o.items() if k not in ("from", "to", "via")}
            try:
                T.check_params("pipe", {"points": [], **rest})
            except ValueError as e:
                raise SpecError(f"objects[{i}] ({oid}): {e}") from e
            if "from" not in o or "to" not in o:
                raise SpecError(f"objects[{i}] ({oid}): pipe needs both 'from' and 'to'")
            pipes.append((oid, o))
            continue
        try:
            T.check_params(typ, o)
            built = T.TEMPLATES[typ](ctx, oid, **o)
        except (TypeError, ValueError, KeyError) as e:
            raise SpecError(f"objects[{i}] ({typ} {oid}): {e}") from e
        ids[oid] = built.element.get("name")
        ports[oid] = built.ports
        boxes[oid] = built.box()
        layer = "panel" if typ == "panel" else "pipe" if typ == "pipe" else "main"
        layers[layer].append(built.element)
    for oid, o in pipes:
        a = _resolve_port(o.pop("from"), ports, boxes)
        b = _resolve_port(o.pop("to"), ports, boxes)
        via = o.pop("via", None)
        pts = [a[:2]] + [tuple(v) for v in via] + [b[:2]] if via else route(a, b)
        built = T.pipe(ctx, oid, pts, **o)
        ids[oid] = built.element.get("name")
        layers["pipe"].append(built.element)
    if spec.get("header") is not None:
        hd = dict(spec["header"])
        hd.setdefault("title", spec.get("name", ""))
        layers["top"].append(T.header(ctx, "Header", int(w), **hd).element)
    if spec.get("nav"):
        nv = dict(spec["nav"])
        nv.setdefault("x", 8)
        nv.setdefault("y", int(h) - 48)
        nv.setdefault("width", int(w) - 16)
        nv.setdefault("current", spec.get("name"))
        layers["top"].append(T.nav_bar(ctx, "Nav", **nv).element)
    for layer in ("panel", "pipe", "main", "top"):
        for el in layers[layer]:
            doc.insert(el)
    report = {"objects": ids, "ports": {k: {p: [round(v[0]), round(v[1]), v[2]] for p, v in pp.items()}
                                        for k, pp in ports.items() if pp}}
    return doc, report


# ---------------------------------------------------------------------------
# Faceplates
# ---------------------------------------------------------------------------

def faceplate_params(auto: bool = True, reset: bool = True) -> list[str]:
    """Order of the /T tags a motor faceplate expects (#1, #2, ...)."""
    return ["run", "fault"] + (["auto"] if auto else []) + ["cmd"] + (["reset"] if reset else [])


def motor_faceplate(title: str = "Motor", style: dict | None = None, auto: bool = True,
                    reset: bool = True) -> tuple[G.GfxDoc, list[str]]:
    """On-top popup for a motor/pump driven by tag placeholders. Open it with
    Display "<name>" /T<run>,<fault>[,<auto>],<cmd>[,<reset>]  (see faceplate_params):
      run, fault, auto: BOOL status;  cmd: BOOL HMI command (START = Set 1, STOP = Set 0);
      reset: BOOL, momentary while pressed."""
    ctx = T.Ctx(shortcut="", style=style)
    s = ctx.s
    order = faceplate_params(auto, reset)
    ph = {k: f"#{i + 1}" for i, k in enumerate(order)}
    W = 280
    doc = G.GfxDoc.new(W, 300, backColor=s["bg"], displayType="onTop", titleBar="true", titleBarText=title)
    els = [
        T.label(ctx, "LblState", 16, 16, "State", w=80, bold=True).element,
        T.status_text(ctx, "State", 100, 16, [{"when": ph["fault"], "text": "FAULT", "color": s["alarm1"]},
                                               {"when": ph["run"], "text": "RUNNING", "color": s["text"]}],
                      {"text": "STOPPED"}, w=130, size=10).element,
        T.alarm_indicator(ctx, "Flt", 240, 17, ph["fault"], 1, "F").element,
    ]
    y = 42
    if auto:
        els += [T.label(ctx, "LblMode", 16, y, "Mode", w=80, bold=True).element,
                T.status_text(ctx, "Mode", 100, y, [{"when": ph["auto"], "text": "AUTO", "color": s["text"]}],
                              {"text": "HAND / LOCAL", "color": s["text"]}, w=130, size=10).element]
        y += 26
    y += 8
    els += [
        T.panel(ctx, "Cmd", 12, y, W - 24, 104, "Command").element,
        T.command_button(ctx, "Start", 28, y + 26, "START", "set", ph["cmd"], 1, w=104, h=36).element,
        T.command_button(ctx, "Stop", 148, y + 26, "STOP", "set", ph["cmd"], 0, w=104, h=36).element,
        T.label(ctx, "CmdNote", 28, y + 70, "Writes the PLC HMI command bit.", w=224, size=8,
                color=s["units"]).element,
    ]
    y += 116
    if reset:
        els.append(T.command_button(ctx, "Reset", 28, y, "FAULT RESET", "momentary", ph["reset"], w=224, h=34,
                                    enable=ph["fault"]).element)
        y += 46
    els.append(T.command_button(ctx, "Close", W - 108, y, "CLOSE", "command", "Abort", w=92, h=30).element)
    y += 42
    doc.settings.set("height", str(y))
    for e in els:
        doc.insert(e)
    return doc, order
