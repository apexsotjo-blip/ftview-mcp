"""Phase 2: templates, generation, expression evaluator, simulation, style tools."""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ftview_mcp import expr as E  # noqa: E402
from ftview_mcp import generate as GEN  # noqa: E402
from ftview_mcp import gfx as G  # noqa: E402
from ftview_mcp import render as R  # noqa: E402
from ftview_mcp import schema as S  # noqa: E402
from ftview_mcp import style as ST  # noqa: E402
from ftview_mcp import templates as T  # noqa: E402

EXAMPLE = HERE.parent / "examples" / "equalization_hp.json"

SAMPLES = {
    "pump": dict(x=100, y=100, label="P-1", tags={"run": "R", "fault": "F", "auto": "A"}, faceplate="FP"),
    "blower": dict(x=200, y=100, label="B-1", tags={"run": "R", "hand": "H"}),
    "mixer": dict(x=300, y=100, label="M-1", tags={"run": "R", "fault": "F"}, shaft=80),
    "motor": dict(x=400, y=100, label="MTR", tags={"run": "R"}, size=1.5),
    "valve": dict(x=500, y=100, label="XV-1", tags={"open": "O", "closed": "C", "fault": "F"}, vertical=True),
    "tank": dict(x=600, y=100, label="TK-1", tags={"level": "L", "hihi": "HH", "hi": "H", "lo": "LO", "lolo": "LL"}),
    "analog": dict(x=100, y=400, tag="PV", label="FIT-1", units="m3/h", alarm="AH",
                   bar={"min": 0, "max": 10, "lo": 2, "hi": 8}),
    "setpoint": dict(x=100, y=500, tag="SP", label="SP", units="%", confirm=True),
    "status": dict(x=300, y=400, states=[{"when": "F", "text": "FAULT"}, {"when": "=1", "text": "X"}],
                   default={"text": "OFF"}),
    "button": dict(x=300, y=500, caption="GO", action="momentary", tag="CMD", enable="NOT F"),
    "alarm": dict(x=500, y=400, expr="A", priority=3),
    "mode": dict(x=520, y=400, expr="NOT A"),
    "label": dict(x=500, y=500, text="Hello"),
    "panel": dict(x=700, y=400, w=200, h=100, title="P"),
    "pipe": dict(points=[[10, 10], [100, 10], [100, 80], [100, 90]], arrow=True),
    "header": dict(width=1920, title="T"),
    "nav": dict(x=0, y=1000, width=1920, displays=["A", {"display": "B", "caption": "Bee"}], current="A"),
}


class Templates(unittest.TestCase):
    def test_every_template_is_schema_valid(self):
        self.assertEqual(set(SAMPLES), set(T.TEMPLATES))
        doc = G.GfxDoc.new()
        ctx = T.Ctx("SCADA")
        for name, params in SAMPLES.items():
            with self.subTest(name):
                built = T.TEMPLATES[name](ctx, name.upper(), **params)
                doc.insert(built.element)
        self.assertEqual(doc.validate(), [])
        self.assertEqual(doc.duplicate_names(), [])

    def test_tag_syntax(self):
        ctx = T.Ctx("SCADA")
        self.assertEqual(ctx.ref("P1"), "{[SCADA]P1}")
        self.assertEqual(ctx.ref("[PLC]P1"), "{[PLC]P1}")
        self.assertEqual(ctx.ref("#3"), "{#3}")
        self.assertEqual(ctx.ref("={a} + 1"), "{a} + 1")
        self.assertEqual(ctx.tag("{[X]Y}"), "[X]Y")

    def test_names_unique_and_legal(self):
        ctx = T.Ctx(taken={"P1"})
        self.assertEqual(ctx.name("P1"), "P1_2")
        self.assertTrue(ctx.name("1-bad name").startswith("O_"))


class Generation(unittest.TestCase):
    def test_example_valid_and_ported(self):
        doc, rep = GEN.generate(json.loads(EXAMPLE.read_text()))
        self.assertEqual(doc.validate(), [])
        self.assertIn("P101A", rep["objects"])
        self.assertIn("out", rep["ports"]["P101A"])
        nav = [c for c in G.commands(doc) if c["verb"] == "display"]
        self.assertIn("TREATMENT", {c["display"] for c in nav})

    def test_spec_errors(self):
        with self.assertRaises(GEN.SpecError):
            GEN.generate({"objects": [{"type": "nope"}]})
        with self.assertRaises(GEN.SpecError):
            GEN.generate({"objects": [{"type": "label", "id": "A", "x": 0, "y": 0, "text": "a"},
                                      {"type": "label", "id": "A", "x": 0, "y": 0, "text": "b"}]})
        with self.assertRaises(GEN.SpecError):
            GEN.generate({"objects": [{"type": "pump", "id": "P", "x": 0, "y": 0},
                                      {"type": "pipe", "from": "P.nozzle", "to": [5, 5]}]})
        with self.assertRaises(GEN.SpecError):
            GEN.generate({"objects": [{"type": "pump", "id": "P", "x": 0, "y": 0, "colour": "red"}]})

    def test_route_is_orthogonal(self):
        pts = GEN.route((0, 0, "right"), (100, 50, "left"))
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            self.assertTrue(x1 == x2 or y1 == y2)

    def test_faceplate(self):
        doc, order = GEN.motor_faceplate(auto=False, reset=True)
        self.assertEqual(doc.validate(), [])
        self.assertEqual(order, ["run", "fault", "cmd", "reset"])
        refs = {r.ref for r in G.tag_refs(doc)}
        self.assertTrue({"#1", "#2", "#3", "#4"} <= refs)
        self.assertEqual(doc.settings.get("displayType"), "onTop")


class Expressions(unittest.TestCase):
    def test_eval(self):
        v = {"A": 1, "B": 0, "[SCADA]L": 42.5}
        cases = {
            "{A}": 1, "{[SCADA]A}": 1, "NOT {A}": 0, "{A} AND {B}": 0, "{A} OR {B}": 1,
            "If {A} Then 1 Else 0": 1, "If {B} Then 1 Else If {A} Then 2 Else 0": 2,
            "{L} > 40": 1, "{[SCADA]L} * 2": 85.0, "({A} + 2) * 3": 9, "{B} == 0": 1, "{A} <> 1": 0,
            "CurrentUserHasCode( A )": 0, "-{L}": -42.5, "7 MOD 4": 3, "({A}) AND NOT ({B})": 1,
        }
        for e, want in cases.items():
            with self.subTest(e):
                self.assertEqual(E.evaluate(e, v), want)

    def test_bad(self):
        with self.assertRaises(E.ExprError):
            E.evaluate("If {A} Then", {})


class Simulation(unittest.TestCase):
    def test_alarm_visibility_follows_tag(self):
        doc = G.GfxDoc.new(400, 300)
        ctx = T.Ctx("SCADA")
        doc.insert(T.alarm_indicator(ctx, "X", 10, 10, "ALM", 1).element)
        off = R.to_svg(doc, simulate={"values": {"ALM": 0}})
        on = R.to_svg(doc, simulate={"values": {"ALM": 1}})
        design = R.to_svg(doc)
        self.assertNotIn('data-name="X_Alm"', off)
        self.assertIn('data-name="X_Alm"', on)
        self.assertIn('data-name="X_Alm"', design)

    def test_fill_and_color(self):
        doc = G.GfxDoc.new(400, 300)
        ctx = T.Ctx("SCADA")
        doc.insert(T.tank(ctx, "TK", 10, 10, w=100, h=200, tags={"level": "L"}).element)
        doc.insert(T.pump(ctx, "P", 200, 10, tags={"run": "R"}).element)
        svg = R.to_svg(doc, simulate={"values": {"L": 50, "R": 1}})
        self.assertIn('height="97.0"', svg)          # (200-6) * 50 %
        self.assertIn(T.HP_STYLE["active"], svg)


class StyleTools(unittest.TestCase):
    def test_audit_and_recolor(self):
        from ftview_mcp import build as B
        d = G.GfxDoc.new(800, 600, backColor="teal")
        for i in range(4):
            d.insert(B.shape("rectangle", f"R{i}", 10 + 60 * i, 10, 50, 50, "teal", "#404040", 1))
        d.insert(B.text("T1", 10, 100, 200, 20, "label", "blue", 10))
        before = ST.isa101_audit(d)["saturated_static_objects"]
        self.assertGreaterEqual(before, 4)
        ch = ST.recolor(d, {"teal": "#C8C8C8", "#008080": "#C8C8C8"})
        self.assertTrue(ch)
        self.assertEqual(d.validate(), [])
        self.assertLess(ST.isa101_audit(d)["saturated_static_objects"], before)

    def test_generated_display_scores_clean(self):
        doc, _ = GEN.generate(json.loads(EXAMPLE.read_text()))
        a = ST.isa101_audit(doc)
        self.assertEqual(a["findings"], [])


class SchemaInfo(unittest.TestCase):
    def test_describe(self):
        info = S.info()
        d = info.describe("button/command")
        self.assertIn("pressAction", d["attributes"])
        d = info.describe("rectangle/animations/animateFill")
        self.assertEqual(d["attributes"]["fillDirection"]["enum"], ["left", "right", "up", "down"])
        with self.assertRaises(KeyError):
            info.describe("rectangle/nonsense")


class ServerPhase2(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_tools(self):
        from fastmcp import Client
        from ftview_mcp.server import mcp

        async def go():
            async with Client(mcp) as c:
                out = self.tmp / "GEN.xml"
                r = await c.call_tool("ftv_generate_display", {"spec": str(EXAMPLE), "out_path": str(out),
                                                               "preview": False})
                self.assertEqual(r.data["summary"]["error"], 0)
                self.assertTrue(out.is_file())
                r = await c.call_tool("ftv_add_template", {
                    "xml_path": str(out), "type": "pump", "out_path": str(self.tmp / "GEN2.xml"),
                    "params": {"id": "P101A", "x": 100, "y": 700, "label": "P-999", "tags": {"run": "X"}}})
                self.assertNotEqual(r.data["inserted"], "P101A")   # renamed, existing id kept
                r = await c.call_tool("ftv_schema", {"path": "numericDisplay"})
                self.assertIn("decimalPlaces", r.data["attributes"])
                r = await c.call_tool("ftv_template_catalog", {})
                self.assertIn("tank", r.data["templates"])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
