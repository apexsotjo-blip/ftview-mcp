"""Run: python -m unittest discover -s tests -v

Optional corpus: set FTVIEW_TEST_CORPUS to a folder of display XML exported by View Studio (or
keep them in ..\\..\\xml). The round-trip tests then run on every file there. Without a corpus,
tests that need a display use one generated from examples/equalization_hp.json.
"""
import asyncio
import copy
import glob
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ftview_mcp import gfx as G  # noqa: E402
from ftview_mcp import lint as L  # noqa: E402
from ftview_mcp import plc as P  # noqa: E402
from ftview_mcp import project as PR  # noqa: E402

CORPUS = Path(os.environ.get("FTVIEW_TEST_CORPUS", HERE.parent.parent / "xml"))
DISPLAYS = sorted(p for p in CORPUS.glob("*.xml")
                  if not p.name.startswith("BatchImport") and b"<gfx" in p.read_bytes()[:400])


def _small_display() -> Path:
    """Smallest corpus display with buttons and groups; otherwise one generated from the example."""
    for p in sorted(DISPLAYS, key=lambda p: p.stat().st_size):
        d = G.GfxDoc.load(p)
        if d.root.find(".//button") is not None and d.root.find(".//group//polygon") is not None:
            return p
    import json
    from ftview_mcp import generate as GEN
    out = HERE / ".generated"
    out.mkdir(exist_ok=True)
    doc, _ = GEN.generate(json.loads((HERE.parent / "examples" / "equalization_hp.json").read_text()))
    p = out / "EXAMPLE.xml"
    p.write_bytes(doc.to_bytes())
    return p


SMALL = _small_display()
SHORTCUT = next((r.shortcut for r in G.tag_refs(G.GfxDoc.load(SMALL)) if r.kind == "device"), "PLC")

MINI_L5X = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content><Controller Name="C1">
<DataTypes><DataType Name="MOTOR"><Members>
  <Member Name="ZZZZZZZZZZMOTOR0" DataType="SINT" Dimension="0" Hidden="true"/>
  <Member Name="Run" DataType="BIT" Dimension="0"/>
  <Member Name="Speed" DataType="REAL" Dimension="0"/>
  <Member Name="Faults" DataType="DINT" Dimension="4"/>
  <Member Name="Tmr" DataType="TIMER" Dimension="0"/>
</Members></DataType></DataTypes>
<Modules><Module Name="RackIO"/></Modules>
<AddOnInstructionDefinitions><AddOnInstructionDefinition Name="PUMP">
  <Parameters><Parameter Name="Cmd" DataType="BOOL"/></Parameters>
  <LocalTags><LocalTag Name="State" DataType="DINT"/></LocalTags>
</AddOnInstructionDefinition></AddOnInstructionDefinitions>
<Tags>
  <Tag Name="M1" TagType="Base" DataType="MOTOR"/>
  <Tag Name="Motors" TagType="Base" DataType="MOTOR" Dimensions="10"/>
  <Tag Name="P1" TagType="Base" DataType="PUMP"/>
  <Tag Name="Word" TagType="Base" DataType="INT"/>
  <Tag Name="M1_Alias" TagType="Alias" AliasFor="M1"/>
</Tags>
<Programs><Program Name="Main"><Tags><Tag Name="Local1" TagType="Base" DataType="DINT"/></Tags></Program></Programs>
</Controller></RSLogix5000Content>"""


class RoundTrip(unittest.TestCase):
    @unittest.skipUnless(DISPLAYS, f"no display exports in {CORPUS}")
    def test_byte_identical_and_valid(self):
        for p in DISPLAYS:
            with self.subTest(p.name):
                d = G.GfxDoc.load(p)
                self.assertEqual(d.to_bytes(), d.raw)
                self.assertEqual(d.validate(), [])
                self.assertEqual(d.duplicate_names(), [])

    def test_move_and_back_is_identical(self):
        d = G.GfxDoc.load(SMALL)
        names = [o.get("name") for o in d.root if G.is_object(o)][:40]
        for n in names:
            d.move(n, 37, -12)
        self.assertNotEqual(d.to_bytes(), d.raw)
        for n in names:
            d.move(n, -37, 12)
        self.assertEqual(d.to_bytes(), d.raw)

    def test_insert_then_delete_is_identical(self):
        d = G.GfxDoc.load(SMALL)
        first = next(o for o, _ in d.iter_objects() if o.tag == "button")
        el = copy.deepcopy(first)
        el.tail = None
        renames = d.insert(el)
        self.assertNotEqual(renames[0][1], first.get("name"))  # renamed, not a duplicate
        self.assertEqual(d.validate(), [])
        self.assertEqual(d.duplicate_names(), [])
        d.delete(renames[0][1])
        self.assertEqual(d.to_bytes(), d.raw)

    def test_group_move_shifts_points(self):
        d = G.GfxDoc.load(SMALL)
        g = next(o for o in d.root if G.is_object(o) and o.tag == "group"
                 and o.find(".//polygon") is not None)
        before = G.bbox(g)
        poly = g.find(".//polygon")
        p0 = float(poly.get("path").split()[0])
        d.move(g.get("name"), 10, 5)
        after = G.bbox(g)
        self.assertEqual((after.left - before.left, after.top - before.top), (10, 5))
        self.assertEqual(float(poly.get("path").split()[0]), p0 + 10)


class Commands(unittest.TestCase):
    def test_parse(self):
        c = G.parse_commands('Display "TANK TRANSFER" /T[SCADA]P101,[SCADA]P102; &Set [SCADA]X 1')
        self.assertEqual(c[0].display, "TANK TRANSFER")
        self.assertEqual(c[0].tags, ["[SCADA]P101", "[SCADA]P102"])
        self.assertEqual((c[1].verb, c[1].tags), ("set", ["[SCADA]X"]))
        self.assertIsNone(G.parse_commands("Display ;Display ODOR")[0].display)

    def test_classify(self):
        self.assertEqual(G.classify("[SCADA]A.B[2]"), ("device", "SCADA", "A.B[2]"))
        self.assertEqual(G.classify("/Area::[PLC]T")[0:2], ("device", "PLC"))
        self.assertEqual(G.classify("#1")[0], "parameter")
        self.assertEqual(G.classify("System\\Second")[0], "hmi")


class Plc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        p = Path(cls.tmp) / "mini.L5X"
        p.write_text(MINI_L5X, encoding="utf-8")
        cls.plc = P.L5X(p)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def st(self, path):
        return self.plc.resolve(path)["status"]

    def test_resolution(self):
        cases = {
            "M1": "ok", "M1.Run": "ok", "M1.Speed": "ok", "m1.speed": "ok",
            "M1.Faults[3]": "ok", "M1.Faults[4]": "bad_index", "M1.Faults[2].31": "ok",
            "M1.Faults[2].32": "bad_index", "M1.Tmr.DN": "ok", "M1.Tmr.XX": "missing_member",
            "M1.Nope": "missing_member", "Motors[9].Run": "ok", "Motors[10].Run": "bad_index",
            "Motors.Run": "bad_index", "P1.Cmd": "ok", "P1.State": "ok", "Word.15": "ok",
            "Word.16": "bad_index", "Missing": "missing_tag", "Program:Main.Local1": "ok",
            "Program:Main.Nope": "missing_tag", "Program:Other.X": "missing_tag",
            "M1_Alias.Speed": "ok", "RackIO:1:I.Data": "unverified", "Ghost:1:I.Data": "missing_tag",
            "Motors[Idx].Run": "ok", "M1.ZZZZZZZZZZMOTOR0": "missing_member",
        }
        for path, want in cases.items():
            with self.subTest(path):
                self.assertEqual(self.st(path), want, self.plc.resolve(path))


class Safety(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_rejects_schema_breaking_edit(self):
        src = G.GfxDoc.load(SMALL)
        d = G.GfxDoc(src.raw)
        name = next(o.get("name") for o in d.root if G.is_object(o))
        d.set_attrs(name, {"visible": "maybe"})
        with self.assertRaises(G.GfxError):
            PR.safe_write(d, self.tmp / "out.xml", baseline=src)
        self.assertFalse((self.tmp / "out.xml").exists())

    def test_overwrite_needs_flag_and_backs_up(self):
        src = G.GfxDoc.load(SMALL)
        out = self.tmp / "out.xml"
        PR.safe_write(src, out, baseline=src)
        with self.assertRaises(FileExistsError):
            PR.safe_write(src, out, baseline=src)
        r = PR.safe_write(src, out, overwrite=True, baseline=src)
        self.assertTrue(Path(r["backup"]).is_file())
        self.assertEqual(out.read_bytes(), src.raw)

    def test_refuses_hmi_projects_and_non_xml(self):
        src = G.GfxDoc.load(SMALL)
        with self.assertRaises(PermissionError):
            PR.safe_write(src, PR.SE_PROJECTS / "X" / "Gfx" / "a.xml")
        with self.assertRaises(PermissionError):
            PR.safe_write(src, self.tmp / "a.gfx")

    def test_new_display_valid(self):
        d = G.GfxDoc.new(1280, 800)
        self.assertEqual(d.validate(), [])
        d.insert('<rectangle name="Rectangle1" height="10" width="10" left="5" top="5"/>')
        self.assertEqual(d.validate(), [])

    def test_batch_and_log(self):
        shutil.copy(SMALL, self.tmp / "SMALL.xml")
        b = PR.write_batch_import([str(self.tmp / "SMALL.xml")], self.tmp / "Batch.xml")
        self.assertTrue(b.read_bytes().startswith(b"\xff\xfe"))
        self.assertIn('importFile="SMALL.xml"', b.read_bytes().decode("utf-16"))
        log = CORPUS / "DisplaysImport.txt"
        if log.exists():
            r = PR.parse_import_log(log)
            self.assertTrue(any(d["skipped_count"] for d in r["displays"]))
            self.assertTrue(all(not d["applied"] for d in r["displays"] if d["skipped_count"]))


class Lint(unittest.TestCase):
    def test_finds_problems(self):
        from ftview_mcp import build as B
        d = G.GfxDoc.new()
        d.insert(B.button("Nav1", 0, 0, 100, 30, "A", press='Display "OTHER"'))
        d.insert(B.button("Nav2", 0, 40, 100, 30, "B", press="Display ;Display OTHER"))
        f = L.lint(d, displays=["MAIN"])
        codes = {x["code"] for x in f}
        self.assertIn("UNKNOWN_DISPLAY", codes)     # OTHER is not in the given list
        self.assertIn("DISPLAY_NO_NAME", codes)     # 'Display ;Display OTHER'


class Server(unittest.TestCase):
    def test_tools_registered_and_callable(self):
        from fastmcp import Client
        from ftview_mcp.server import mcp

        async def go():
            async with Client(mcp) as c:
                tools = {t.name for t in await c.list_tools()}
                self.assertIn("ftv_lint", tools)
                r = await c.call_tool("ftv_display_info", {"xml_path": str(SMALL)})
                self.assertTrue(r.data["schema_valid"])
                r = await c.call_tool("ftv_replace", {"xml_path": str(SMALL), "find": f"[{SHORTCUT}]",
                                                      "replace": "[NEWPLC]"})
                self.assertGreater(r.data["matches"], 0)
                self.assertFalse(r.data["written"])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
