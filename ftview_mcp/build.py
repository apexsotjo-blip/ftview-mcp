"""Low-level builders for Gfx-SE display elements.

Every builder emits the full attribute set in the order View Studio exports it
(copied from real v12 exports), so generated XML looks like View's own output and
does not lean on importer defaults.
"""
from __future__ import annotations

from lxml import etree

GRAD = (("endColor", "white"), ("gradientStop", "50"), ("gradientDirection", "gradientDirectionHorizontal"),
        ("gradientShadingStyle", "gradientHorizontalFromRight"))
FILL_GRAD = (("fillEndColor", "white"), ("fillGradientStop", "50"),
             ("fillGradientDirection", "gradientDirectionHorizontal"),
             ("fillGradientShadingStyle", "gradientHorizontalFromRight"))
FONT_FLAGS = (("italic", "false"), ("underline", "false"), ("strikethrough", "false"))
ANIM_ORDER = ["animateVisibility", "animateRotation", "animateWidth", "animateHeight",
              "animateHorizontalPosition", "animateVerticalPosition", "animateHorizontalSlider",
              "animateVerticalSlider", "animateFill", "animateTouch", "animateColor", "animateHyperlink"]


def _el(tag: str, attrs, *children) -> etree._Element:
    e = etree.Element(tag)
    for k, v in attrs:
        if v is None:
            continue
        e.set(k, _s(v))
    for c in children:
        if c is not None:
            e.append(c)
    return e


def _s(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:g}"
    return str(v)


def _i(v) -> int:
    return int(round(v))


def _base(name, l, t, w, h, tooltip="", wallpaper=True):
    a = [("name", name), ("height", max(1, _i(h))), ("width", max(1, _i(w))), ("left", _i(l)), ("top", _i(t)),
         ("visible", "true")]
    if wallpaper:
        a.append(("wallpaper", "false"))
    a += [("toolTipText", tooltip), ("exposeToVba", "notExposed"), ("isReferenceObject", "false")]
    return a


# ---------------------------------------------------------------------------
# Drawing objects
# ---------------------------------------------------------------------------

def shape(kind: str, name: str, l, t, w, h, fill="white", stroke="black", lw=1, line_style="solid",
          transparent=False, tooltip="", rounding=None) -> etree._Element:
    """rectangle | roundedRectangle | ellipse"""
    a = _base(name, l, t, w, h, tooltip)
    a += [("backStyle", "transparent" if transparent else "solid"), ("backColor", fill), ("foreColor", stroke),
          ("lineStyle", line_style if lw else "none"), ("lineWidth", max(1, lw)), ("patternStyle", "none"),
          ("patternColor", stroke)]
    if kind == "roundedRectangle":
        r = rounding or max(2, _i(min(w, h) * 0.2))
        a += [("roundingWidth", r), ("roundingHeight", r)]
    a += list(GRAD)
    return _el(kind, a)


def _bbox(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def _path(points, close=False):
    pts = [(_i(x), _i(y)) for x, y in points]
    if close and pts[0] != pts[-1]:
        pts.append(pts[0])
    return " ".join(f"{x} {y}" for x, y in pts) + " "


def polygon(name: str, points, fill="white", stroke="black", lw=1, line_style="solid", tooltip="") -> etree._Element:
    l, t, w, h = _bbox(points)
    a = _base(name, l, t, w, h, tooltip, wallpaper=False)
    a += [("backStyle", "solid"), ("backColor", fill), ("foreColor", stroke),
          ("lineStyle", line_style if lw else "none"), ("lineWidth", max(1, lw)), ("patternStyle", "none"),
          ("patternColor", stroke), ("path", _path(points, close=True))] + list(GRAD)
    return _el("polygon", a)


def polyline(name: str, points, color="#808080", lw=3, line_style="solid", tooltip="") -> etree._Element:
    l, t, w, h = _bbox(points)
    a = _base(name, l, t, w, h, tooltip, wallpaper=False)
    a += [("backStyle", "transparent"), ("backColor", color), ("foreColor", color), ("lineStyle", line_style),
          ("lineWidth", max(1, lw)), ("patternStyle", "none"), ("patternColor", color),
          ("path", _path(points))] + list(GRAD)
    return _el("polyline", a)


def line(name: str, x1, y1, x2, y2, color="#808080", lw=1, line_style="solid") -> etree._Element:
    """Straight line. View's importer resets lineWidth of <line> objects to 1, so a thicker
    horizontal/vertical line is emitted as a filled rectangle of that thickness instead."""
    if lw > 1 and line_style == "solid" and (x1 == x2 or y1 == y2):
        if y1 == y2:
            l, t, w, h = min(x1, x2), y1 - lw // 2, abs(x2 - x1) + 1, lw
        else:
            l, t, w, h = x1 - lw // 2, min(y1, y2), lw, abs(y2 - y1) + 1
        return shape("rectangle", name, l, t, w, h, color, color, 1, line_style="none")
    l, t, w, h = _bbox([(x1, y1), (x2, y2)])
    a = _base(name, l, t, w, h, wallpaper=False)
    a += [("line", _path([(x1, y1), (x2, y2)])), ("backStyle", "transparent"), ("lineStyle", line_style),
          ("lineWidth", max(1, lw)), ("backColor", "white"), ("foreColor", color)]
    return _el("line", a)


def text(name: str, l, t, w, h, caption: str, color="black", size=10, bold=False, align="middleCenter",
         font="Arial", fill=None, wrap=False, size_to_fit=False, tooltip="") -> etree._Element:
    a = _base(name, l, t, w, h, tooltip)
    a += [("backStyle", "solid" if fill else "transparent"), ("backColor", fill or "white"), ("foreColor", color),
          ("wordWrap", wrap), ("sizeToFit", size_to_fit), ("alignment", align), ("fontFamily", font),
          ("fontSize", max(1, round(size))), ("bold", bold)] + list(FONT_FLAGS) + [("caption", caption)]
    return _el("text", a)


# ---------------------------------------------------------------------------
# Data objects
# ---------------------------------------------------------------------------

def connections(**conns) -> etree._Element:
    return _el("connections", [], *[_el("connection", [("name", k), ("expression", v)]) for k, v in conns.items()])


def char_metrics(size_pt: float) -> tuple[int, int]:
    """charHeight/charWidth for a font size. View sizes numeric/string displays from these
    (it ignores fontSize on import), so we emit them the way View's own exports do."""
    ch = max(8, round(size_pt * 1.5))
    return ch, max(4, round(ch * 0.42))


def _fit(l, t, w, h, chars: int, size_pt: float, justify: str):
    """Box View will produce for `chars` characters, placed inside the requested box."""
    ch, cw = char_metrics(size_pt)
    fw, fh = chars * (cw + 2), ch + 1
    fw = min(fw, w) if w else fw
    left = l + (w - fw if justify == "right" else (w - fw) / 2 if justify == "center" else 0)
    top = t + max(0, (h - fh) / 2)
    return left, top, fw, fh, ch, cw


def numeric_display(name: str, l, t, w, h, expression: str, decimals=1, color="black", size=10, bold=False,
                    justify="right", font="Arial", fill=None, field_length=8, tooltip="") -> etree._Element:
    """Numeric display fitted inside the box (l, t, w, h), justified within it."""
    l, t, w, h, ch, cw = _fit(l, t, w, h, field_length, size, justify)
    a = _base(name, l, t, w, h, tooltip)
    a += [("backColor", fill or "white"), ("backStyle", "solid" if fill else "transparent"), ("foreColor", color),
          ("fontFamily", font), ("charHeight", ch), ("charWidth", cw), ("bold", bold)] + list(FONT_FLAGS) + [
        ("justification", justify), ("fieldLength", field_length), ("showDigitGrouping", False),
        ("decimalPlaces", decimals), ("format", "floatingPoint" if decimals else "decimal"),
        ("overflow", "showExponent"), ("leadingCharacter", "blanks"), ("decimalPlaceType", "fixed")]
    return _el("numericDisplay", a, connections(Value=expression))


def string_display(name: str, l, t, w, h, expression: str, color="black", size=10, font="Arial", fill=None,
                   justify="left", chars=20, tooltip="") -> etree._Element:
    l, t, w, h, ch, cw = _fit(l, t, w, h, chars, size, justify)
    a = _base(name, l, t, w, h, tooltip)
    a += [("backColor", fill or "white"), ("backStyle", "solid" if fill else "transparent"), ("foreColor", color),
          ("fontFamily", font), ("charHeight", ch), ("charWidth", cw), ("bold", False)] + list(FONT_FLAGS) + [
        ("justification", justify), ("dimensionsHeight", 1), ("dimensionsWidth", chars), ("characterOffset", 0)]
    return _el("stringDisplay", a, connections(Value=expression))


def _confirm(enabled=False, prompt="Are you sure you want to perform this action?"):
    return _el("confirm", [("confirmAction", enabled), ("buttonSetting", "okCancel"), ("titleBar", True),
                           ("titleBarText", "Confirmation"), ("windowPosition", "Centered of screen")],
               _el("caption", [("fontFamily", "Arial"), ("fontSize", 10), ("bold", False)] + list(FONT_FLAGS)
                   + [("caption", prompt)]),
               _el("imageSettings", [("imageReference", "noImage")]))


def _esig():
    return _el("eSignature", [("requireElectronicSignature", False), ("allowBlankComment", False),
                              ("requireReAuthentication", False), ("requireCounterSignature", False),
                              ("authorizedGroup", "")])


def numeric_input(name: str, l, t, w, h, expression: str, decimals=1, size=10, font="Arial", field_length=8,
                  security="*", confirm=False, tab_index=1, tooltip="") -> etree._Element:
    a = _base(name, l, t, w, h, tooltip)
    a += [("fontFamily", font), ("fontSize", size), ("bold", False)] + list(FONT_FLAGS) + [
        ("fieldLength", field_length), ("justification", "right"), ("enabledWhenExpressionIsTrue", True),
        ("tabIndex", tab_index), ("continuouslyUpdate", True), ("discardInputOnFocusLost", False),
        ("defaultData", ""), ("decimalPlaces", decimals), ("format", "floatingPoint" if decimals else "decimal"),
        ("overflow", "showExponent"), ("leadingCharacter", "blanks"), ("inputSecurityCode", security),
        ("displayOnScreenKeypad", False), ("keypadCustomCaption", ""), ("decimalPlaceType", "fixed")]
    return _el("numericInput", a, connections(Value=expression),
               _confirm(confirm, "Write new value?"), _esig())


def button(name: str, l, t, w, h, caption: str, press="", release="", repeat="", back="#C8C8C8",
           fore="#1E1E1E", size=10, bold=True, font="Arial", style="3d", confirm=None, enable_expr="",
           tab_index=1, tooltip="") -> etree._Element:
    a = _base(name, l, t, w, h, tooltip)
    a += [("style", style), ("captureCursor", False), ("highlightOnFocus", True), ("tabIndex", tab_index)]
    cap = lambda c: _el("caption", [("fontFamily", font), ("fontSize", size), ("bold", bold)]  # noqa: E731
                        + list(FONT_FLAGS) + [("caption", c)])
    face = [("patternColor", "black"), ("patternStyle", "none"), ("backColor", back), ("backStyle", "solid"),
            ("foreColor", fore)] + list(GRAD)
    return _el("button", a,
               _el("command", [("pressAction", press), ("repeatAction", repeat), ("releaseAction", release),
                               ("repeatRate", "0.25")]),
               _el("up", face, cap(caption), _el("imageSettings", [("imageReference", "noImage")])),
               _el("down", [("downSameAsUp", True)] + face, cap(caption),
                   _el("imageSettings", [("imageReference", "noImage")])),
               _el("ability", [("showDisabledState", bool(enable_expr)), ("expression", enable_expr),
                               ("enabledWhenExpressionIsTrue", True), ("disabledImageType", "useGrayscale")]),
               _confirm(bool(confirm), confirm or "Are you sure you want to perform this action?"),
               _esig())


def time_date(name: str, l, t, w, h, fore="#1E1E1E", back="#C8C8C8", size=11, fmt="timeShortDate") -> etree._Element:
    a = _base(name, l, t, w, h)
    a += [("backColor", back), ("foreColor", fore), ("backStyle", "transparent"), ("borderColor", back),
          ("borderStyle", "none"), ("borderUsesBackColor", True), ("borderWidth", 1), ("patternColor", "white"),
          ("patternStyle", "none"), ("description", ""), ("blink", False), ("wordWrap", True),
          ("alignment", "middleRight"), ("timeAndDateFormat", fmt), ("fontFamily", "Arial"), ("fontSize", size),
          ("bold", True)] + list(FONT_FLAGS) + list(GRAD)
    return _el("timeAndDateDisplay", a)


def strip_group_child_attrs(el: etree._Element) -> None:
    """View's importer rejects 'wallpaper' on objects inside a group ("The wallpaper cannot be set
    on 'X', as it is the child of a group"); View's own exports omit it there."""
    for d in el.iter():
        if d is not el and d.get("name") is not None and "wallpaper" in d.attrib:
            del d.attrib["wallpaper"]


def group(name: str, children, tooltip="", anims=None) -> etree._Element:
    g = _el("group", [("name", name), ("visible", "true"), ("wallpaper", "false"), ("toolTipText", tooltip),
                      ("exposeToVba", "notExposed"), ("isReferenceObject", "false")], *children)
    strip_group_child_attrs(g)
    if anims:
        add_animations(g, anims)
    return g


# ---------------------------------------------------------------------------
# Global objects
# ---------------------------------------------------------------------------

def set_parameters(el: etree._Element, params: dict[str, str], descriptions: dict[str, str] | None = None):
    """<parameters> on a global object (definitions, with descriptions) or on a reference
    instance (values). Keys are '#1', '#2', ... Placed last, as the schema requires."""
    old = el.find("parameters")
    if old is not None:
        el.remove(old)
    p = etree.SubElement(el, "parameters")
    for k, v in params.items():
        if not k.startswith("#"):
            raise ValueError(f"parameter names look like '#1', got {k!r}")
        attrs = [("name", k), ("value", v)]
        if descriptions and k in descriptions:
            attrs.append(("description", descriptions[k]))
        p.append(_el("parameter", attrs))
    return el


def reference_instance(base: etree._Element, base_file: str, name: str, dx: float = 0, dy: float = 0,
                       values: dict[str, str] | None = None, link_animations: str = "linkWithExpression"
                       ) -> etree._Element:
    """Instance of a global object: a copy of the base object marked as a reference to
    '<global object file>.<object name>', with parameter values for #1, #2, ..."""
    import copy as _copy
    from . import gfx as _g
    inst = _copy.deepcopy(base)
    inst.tail = None
    _g.shift(inst, dx, dy)
    inst.set("name", name)
    inst.set("isReferenceObject", "true")
    inst.set("linkSize", "true")
    inst.set("linkConnections", "true")
    inst.set("linkAnimations", link_animations)
    inst.set("linkBaseObject", f"{base_file}.{base.get('name')}")
    inst.set("linkToolTipText", "true")
    for child in inst.iter():  # View marks every object of an instance as a reference
        if child is not inst and child.get("isReferenceObject") is not None:
            child.set("isReferenceObject", "true")
    if values is not None:
        set_parameters(inst, values)
    return inst


# ---------------------------------------------------------------------------
# Animations
# ---------------------------------------------------------------------------

def color_anim(expression: str, states, blink_rate=1) -> etree._Element:
    """states: [(value, back, fore)] or [(value, back, fore, blink_back)] - at least 2."""
    entries = []
    for st in states:
        value, back, fore = st[0], st[1], st[2]
        blink = st[3] if len(st) > 3 else None
        entries.append(_el("color", [
            ("value", value), ("foreBehavior", "solid"), ("foreColor1", fore), ("foreColor2", fore),
            ("backBehavior", "blink" if blink else "solid"), ("backColor1", back), ("backColor2", blink or back),
            ("fillColorMode", "2")] + list(GRAD) + list(FILL_GRAD)))
    if len(entries) < 2:
        raise ValueError("color animation needs at least 2 states")
    return _el("animateColor", [("expression", expression), ("blinkRate", blink_rate)], *entries)


def visibility_anim(expression: str, visible_when_true=True) -> etree._Element:
    return _el("animateVisibility", [("expression", expression),
                                     ("expressionTrueState", "visible" if visible_when_true else "invisible")])


def fill_anim(expression: str, lo: float, hi: float, direction="up") -> etree._Element:
    return _el("animateFill", [("expression", expression), ("fillDirection", direction)],
               _el("constantExpressionRange", [("min", float(lo)), ("max", float(hi))]))


def hpos_anim(expression: str, lo: float, hi: float, offset_max: float, offset_min: float = 0) -> etree._Element:
    return _el("animateHorizontalPosition",
               [("expression", expression), ("horizontalOffsetMin", float(offset_min)),
                ("horizontalOffsetMax", float(offset_max))],
               _el("constantExpressionRange", [("min", float(lo)), ("max", float(hi))]))


def touch_anim(press: str, release: str = "") -> etree._Element:
    return _el("animateTouch", [("pressAction", press), ("repeatAction", ""), ("releaseAction", release),
                                ("repeatRate", "0.25"), ("TouchStyle", "rectangular")])


_BEFORE = ("connections", "objectKeys", "parameters")


def add_animations(el: etree._Element, anims) -> etree._Element:
    anims = [a for a in anims if a is not None]
    if not anims:
        return el
    box = el.find("animations")
    if box is None:
        box = etree.Element("animations")
        anchor = next((c for c in el if c.tag in _BEFORE), None)
        if anchor is not None:
            anchor.addprevious(box)
        else:
            el.append(box)
    existing = {c.tag: c for c in box}
    for a in anims:
        if a.tag in existing:
            box.remove(existing[a.tag])
        existing[a.tag] = a
    for c in list(box):
        box.remove(c)
    for tag in sorted(existing, key=ANIM_ORDER.index):
        box.append(existing[tag])
    return el
