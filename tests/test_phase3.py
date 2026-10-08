"""Phase 3 (offline): tag CSV, parameter files, macros, global objects, importer rules,
round-trip verification rules, log parsing. The live View Studio checks are in
ftview_mcp.e2e (run: python -m ftview_mcp.e2e)."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FX = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent))

from ftview_mcp import build as B  # noqa: E402
from ftview_mcp import gfx as G  # noqa: E402
from ftview_mcp import lint as L  # noqa: E402
from ftview_mcp import project as PR  # noqa: E402
from ftview_mcp import studio as S  # noqa: E402
from ftview_mcp import tags as TG  # noqa: E402
from ftview_mcp import templates as T  # noqa: E402
from ftview_mcp import textfiles as TF  # noqa: E402
from ftview_mcp import verify as V  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)


class Tags(Tmp):
    def test_view_exports_round_trip_byte_identical(self):
        for f in ("device_tags_export.CSV", "memory_tags_export.CSV"):
            with self.subTest(f):
                raw = (FX / f).read_bytes().decode("utf-8-sig")
                self.assertEqual(TG.TagDB.read(FX / f).to_text(), raw)

    def test_build_write_read_compare(self):
        db = TG.TagDB()
        db.add_analog("A\\B\\Level", min=0, max=10, initial=2.5, units="m")
        db.add_digital("A\\Run", initial=True)
        db.add_string("A\\Txt", length=20, initial='say "hi"')
        db.add_analog("A\\Dev", "device", address="[SCADA]FIT101_OUT")
        p = db.write(self.tmp / "t.csv")
        self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf;Tag Type"))
        back = TG.TagDB.read(p)
        self.assertEqual(back.folders, ["A", "A\\B"])
        self.assertTrue(TG.compare(db, back)["ok"])
        self.assertEqual(back.tags["A\\Dev"].values["address"], "/::[SCADA]FIT101_OUT")
        self.assertEqual(back.tags["A\\Dev"].values["initial_analog"], 0)  # View requires it
        self.assertEqual(back.tags["A\\Txt"].values["initial_string"], 'say "hi"')

    def test_rejects(self):
        db = TG.TagDB()
        with self.assertRaises(TG.TagError):
            db.add_analog("bad*name")
        with self.assertRaises(TG.TagError):
            db.add_analog("Dev", "device")
        db.add_digital("X")
        with self.assertRaises(TG.TagError):
            db.add_digital("x")
        db.add_analog("Y", min=10, max=0)
        self.assertTrue(any(i["message"] == "min > max" for i in db.validate()))


class AgentFindings(Tmp):
    """Regressions for the bugs found by the independent agent test run (7 Oct 2026)."""

    def test_device_digital_has_initial_label(self):
        db = TG.TagDB()
        db.add_digital("D", "device", address="[SCADA]X", off_label="Stopped", on_label="Running")
        back = TG.TagDB.read(db.write(self.tmp / "d.csv"))
        self.assertEqual(back.tags["D"].values["initial_digital"], "Stopped")
        self.assertEqual(db.validate(), [])

    def test_nested_typos_rejected(self):
        from ftview_mcp import generate as GEN
        for obj in ({"type": "pump", "id": "P", "x": 0, "y": 0, "tags": {"runn": "X"}},
                    {"type": "valve", "id": "V", "x": 0, "y": 0, "tags": {"opn": "X"}},
                    {"type": "analog", "id": "A", "x": 0, "y": 0, "tag": "X", "bar": {"low": 1}},
                    {"type": "status", "id": "S", "x": 0, "y": 0, "states": [{"when": "X", "txt": "a"}]}):
            with self.subTest(obj["type"]), self.assertRaises(GEN.SpecError):
                GEN.generate({"objects": [obj]})

    def test_route_stays_out_of_equipment(self):
        from ftview_mcp import generate as GEN
        # pump inlet faces left; source is to the right and below -> must approach from the left
        pts = GEN.route((700, 600, "down"), (620, 520, "left"))
        self.assertEqual(pts[-1], (620, 520))
        self.assertLess(pts[-2][0], 620)          # last segment comes in from the left
        self.assertEqual(pts[-2][1], 520)
        # valve outlet faces right; target above-left -> must leave to the right first
        pts = GEN.route((1090, 505, "right"), (900, 300, None))
        self.assertGreater(pts[1][0], 1090)

    def test_bad_import_mode_rejected(self):
        st = S.Studio()
        with self.assertRaises(S.StudioError):
            st.import_displays(FX / "nothing.xml", mode="merge")


class TextFiles(Tmp):
    def test_parameter_file(self):
        pf = TF.ParameterFile({"#2": "[SCADA]B", "#1": "[SCADA]A"}, {"#1": "run"})
        p = pf.write(self.tmp / "P101.par")
        raw = p.read_bytes()
        self.assertTrue(raw.startswith(b"\xff\xfe@\x00!\x00=\x001\x00"))  # UTF-16 "@!=1" like View
        back = TF.ParameterFile.read(p)
        self.assertEqual(back.params, {"#1": "[SCADA]A", "#2": "[SCADA]B"})
        self.assertEqual(back.comments, {"#1": "run"})
        self.assertTrue(any("gaps" in i["message"] for i in TF.ParameterFile({"#1": "a", "#3": "b"}).validate()))

    def test_macro(self):
        m = TF.Macro(['Display "OVERVIEW"', "&Set [SCADA]X 1", "Frobnicate 2"])
        back = TF.Macro.read(m.write(self.tmp / "m.mcr"))
        self.assertEqual(back.commands, m.commands)
        issues = back.validate(displays=["CLARIFIER"])
        self.assertTrue(any("not in project" in i["message"] for i in issues))
        self.assertTrue(any("unknown command" in i["message"] for i in issues))


class ImporterRules(unittest.TestCase):
    """Rules learned by importing into View Studio v12 (see e2e runs)."""

    def test_builders_avoid_known_importer_problems(self):
        el = B.line("L", 0, 10, 100, 10, "#000", 3)
        self.assertEqual(el.tag, "rectangle")  # <line> would lose its width
        self.assertEqual(B.line("L", 0, 10, 100, 10, "#000", 1).tag, "line")
        nd = B.numeric_display("N", 0, 0, 200, 30, "{X}", size=11, field_length=8)
        self.assertIsNone(nd.get("fontSize"))  # View ignores fontSize here
        self.assertIsNotNone(nd.get("charHeight"))
        g = B.group("G", [B.shape("rectangle", "R", 0, 0, 5, 5)])
        self.assertIsNone(g.find("rectangle").get("wallpaper"))
        ctx = T.Ctx()
        st = T.status_text(ctx, "S", 0, 0, [{"when": "A", "text": "a"}, {"when": "B", "text": "b"}]).element
        exprs = [a.get("expression") for a in st.iter("animateVisibility")]
        self.assertFalse(any(L._PAREN_TAG.match(e) for e in exprs), exprs)
        doc = G.GfxDoc.new(titleBar="false", titleBarText="x")
        self.assertIsNone(doc.settings.get("titleBarText"))
        self.assertEqual(B.text("T", 0, 0, 10, 10, "x", size=7.7).get("fontSize"), "8")

    def test_lint_flags_importer_problems(self):
        doc = G.GfxDoc.new()
        t = B.text("T1", 0, 0, 50, 20, "x")
        B.add_animations(t, [B.visibility_anim("({[SCADA]X})")])
        doc.insert(t)
        b = B.button("B1", 0, 40, 50, 20, "go", press="&Set [SCADA]X 1", confirm="Sure?")
        doc.insert(b)
        doc.root.append(G.etree.fromstring('<line name="L1" height="1" width="10" left="0" top="80" '
                                           'line="0 80 9 80 " lineWidth="3"/>'))
        codes = {f["code"] for f in L.lint(doc)}
        self.assertTrue({"IMPORT_PAREN_TAG_EXPR", "IMPORT_CONFIRM_DROPPED", "IMPORT_LINE_WIDTH"} <= codes, codes)


class GlobalObjects(unittest.TestCase):
    def test_instances(self):
        ctx = T.Ctx()
        base = T.pump(ctx, "GP", 10, 10, label="#1", tags={"run": "#2"}).element
        B.set_parameters(base, {"#1": "", "#2": ""}, {"#1": "Label", "#2": "Run"})
        disp = G.GfxDoc.new(800, 400)
        for i in range(2):
            disp.insert(B.reference_instance(base, "GOFILE", f"I{i}", 100 * i, 0, {"#1": f"P{i}", "#2": "[S]R"}))
        self.assertEqual(disp.validate(), [])
        self.assertEqual(disp.duplicate_names(), [])
        i0 = disp.get("I0")
        self.assertEqual(i0.get("linkBaseObject"), "GOFILE.GP")
        self.assertTrue(all(c.get("isReferenceObject") == "true" for c in i0.iter() if c.get("isReferenceObject")))
        self.assertEqual(i0.find("parameters/parameter[@name='#1']").get("value"), "P0")


class Verification(unittest.TestCase):
    def _doc(self, xml):
        return G.GfxDoc(('<?xml version="1.0" encoding="UTF-8"?>\r\n<gfx xmlns:xsi="http://www.w3.org/2001/'
                         f'XMLSchema-instance">{xml}</gfx>').encode())

    def test_normalizations_vs_real_differences(self):
        sent = self._doc('<numericDisplay name="N" left="0" top="0" width="200" height="30" fontSize="11"/>'
                         '<text name="T" left="0" top="0" width="60" height="15" sizeToFit="true" '
                         'fontFamily="Arial Unicode MS" fontSize="10" caption="a"/>'
                         '<rectangle name="R" left="10" top="10" width="5" height="5" backColor="red"/>')
        back = self._doc('<numericDisplay name="N" left="0" top="0" width="168" height="31" charHeight="19"/>'
                         '<text name="T" left="0" top="0" width="53" height="15" sizeToFit="true" '
                         'fontFamily="Arial" charHeight="13" caption="a"/>'
                         '<rectangle name="R" left="11" top="10" width="5" height="5" backColor="#FF0000"/>')
        r = V.compare(sent, back)
        self.assertTrue(r["ok"], r["differences"])
        back2 = self._doc('<numericDisplay name="N" left="0" top="0" width="168" height="31"/>'
                          '<text name="T" left="0" top="0" width="53" height="15" sizeToFit="true" '
                          'fontFamily="Arial" charHeight="13" caption="b"/>')
        r = V.compare(sent, back2)
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["R"])
        self.assertEqual([d["attribute"] for d in r["differences"]], ["caption"])


class Logs(unittest.TestCase):
    def test_dbi(self):
        bad = S.parse_dbi(FX / "failed_import.DBI")
        self.assertEqual(bad["failures"], 1)
        self.assertIn("Initial value is invalid", bad["errors"][0])
        ok = S.parse_dbi(FX / "sandbox_ok.DBI")
        self.assertEqual((ok["errors"], ok["failures"]), ([], 0))

    def test_display_log(self):
        self.assertTrue(S.log_done(FX / "DisplaysImport_success.txt"))
        r = PR.parse_import_log(FX / "DisplaysImport_success.txt")
        self.assertTrue(all(d["applied"] for d in r["displays"]))


@unittest.skipUnless((S.SE_PROJECTS / S.SANDBOX).is_dir(), "View SE sandbox app not present")
class Headless(Tmp):
    """No UI: View's own Gfx-to-XML engine lists and exports displays (read-only)."""

    def test_guard_refuses_app_open_in_studio(self):
        from ftview_mcp import headless as H
        if S.SANDBOX not in H.apps_open_in_studio():
            self.skipTest("sandbox not open in View Studio right now")
        with self.assertRaises(H.HeadlessError):
            H.list_displays(S.SANDBOX)

    def test_list_and_export(self):
        from ftview_mcp import headless as H
        if S.SANDBOX in H.apps_open_in_studio():
            self.skipTest("sandbox is open in View Studio; headless export is refused by design")
        names = H.list_displays(S.SANDBOX)
        if not names:
            self.skipTest("sandbox has no displays")
        self.assertTrue(names)
        res = H.export_displays(S.SANDBOX, self.tmp, names[:1])
        self.assertEqual(res["missing"], [])
        doc = G.GfxDoc.load(res["files"][names[0]])
        self.assertEqual(doc.validate(), [])


class StudioSafety(unittest.TestCase):
    def test_refuses_other_apps(self):
        with self.assertRaises(S.StudioError):
            S.Studio("OTHER_APP")
        S.Studio("OTHER_APP", allow_app="OTHER_APP")  # explicit opt-in only constructs, does nothing yet

    def test_keys_escape(self):
        self.assertEqual(S._keys_escape(r"C:\Plant (A)+x"), r"C:\Plant {(}A{)}{+}x")


if __name__ == "__main__":
    unittest.main()
