"""End-to-end tests against real FactoryTalk View Studio, in the sandbox application.

    .venv\\Scripts\\python.exe -m ftview_mcp.e2e [scenario ...] [--workdir DIR]

Scenarios (default: all):
  generate   generated displays (every template + example spec + faceplate):
             lint clean -> import (create) -> import log clean -> export -> compare
  edit       edit imported displays with the edit API, then
             a) import in 'update' mode (E2E_EDIT_U) -> edits applied exactly
             b) 'replace' = delete display + create (E2E_EDIT_R) -> inserts/deletes applied exactly
  corpus     a real display export (FTVIEW_E2E_CORPUS=path\to\Display.xml) -> import -> export -> compare
  global     global object with parameters + display with two linked instances
  tags       HMI tag CSV written by ftview_mcp.tags -> import -> export -> compare

The four display scenarios run as ONE batched pipeline (one reset, one create import of every
display, one export, one update + one replace import, one export) to keep wizard runs to a minimum.

Each run writes <workdir>/runs/<timestamp>/report.json + report.md + screenshots.
Exit code 0 only if every scenario passed. Needs an interactive desktop session.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

from . import generate as GEN
from . import gfx as G
from . import lint as L
from . import project as PR
from . import studio as S
from . import templates as T
from . import verify as V

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "equalization_hp.json"
CORPUS = os.environ.get("FTVIEW_E2E_CORPUS", "")


class Run:
    def __init__(self, workdir: Path):
        self.dir = workdir / "runs" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.results: list[dict] = []
        self.studio = S.Studio(workdir=workdir, log=self.log)

    def log(self, msg):
        line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        with open(self.dir / "run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def shot(self, name):
        return str(S.screenshot(self.dir / f"{name}.png"))

    def scenario(self, name, fn):
        self.log(f"=== scenario {name}")
        rec = {"scenario": name, "passed": False, "checks": []}
        try:
            fn(rec)
            rec["passed"] = all(c["ok"] for c in rec["checks"]) and bool(rec["checks"])
        except Exception as e:  # noqa: BLE001 - report every failure, keep going
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["traceback"] = traceback.format_exc()
            rec["screenshot"] = self.shot(f"{name}_error")
            self.log(f"!!! {name} failed: {e}")
        self.log(f"=== {name}: {'PASS' if rec['passed'] else 'FAIL'}")
        self.results.append(rec)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def check(rec, what, ok, **detail):
        rec["checks"].append({"check": what, "ok": bool(ok), **detail})

    def fresh(self, names):
        existing = {n.lower(): n for n in self.studio.display_names()}
        for n in names:
            if n.lower() in existing:
                self.log(f"deleting sandbox display {n}")
                self.studio.delete_display(existing[n.lower()])

    def import_batch(self, docs: dict[str, G.GfxDoc], mode: str, tag: str, owner: dict[str, dict]):
        """One wizard import for several displays; checks go to each display's scenario record."""
        d = self.dir / f"in{tag}"
        d.mkdir(exist_ok=True)
        files = []
        for name, doc in docs.items():
            p = d / f"{name}.xml"
            p.write_bytes(doc.to_bytes())
            files.append(str(p))
        log = self.studio.import_displays(PR.write_batch_import(files, d / "Batch.xml"), mode)
        shutil.copy(log, self.dir / f"DisplaysImport{tag}.txt")
        parsed = PR.parse_import_log(log)
        raw = Path(log).read_bytes().decode("utf-16", "replace")
        by_display: dict[str, list[str]] = {}
        cur = None
        for ln in raw.splitlines():
            s = ln.strip()
            if s.startswith("Importing file"):
                cur = s.split("into display '")[-1].rstrip("'")
            elif s and cur and not s.startswith(("Line number", "This status", "Import completed", "If there is VBA")):
                by_display.setdefault(cur, []).append(s)
        for disp in parsed["displays"]:
            rec = owner[disp["display"]]
            self.check(rec, f"import '{disp['display']}' ({mode})",
                       disp["result"] and "success" in disp["result"].lower(),
                       result=disp["result"], skipped=disp["skipped_count"])
            msgs = by_display.get(disp["display"], [])
            self.check(rec, f"import log for '{disp['display']}' has no messages", not msgs, messages=msgs[:30])

    def export_all(self, tag: str) -> Path:
        # Wizard export: headless export must not run while Studio has this app open (it would
        # open and lock the displays inside Studio; see headless._guard).
        out = self.dir / f"out{tag}"
        self.studio.export_displays(out)
        return out

    def round_trip(self, rec, what: str, sent: G.GfxDoc, out: Path, name: str):
        p = out / f"{name}.xml"
        if not p.exists():
            self.check(rec, what, False, missing_export=str(p))
            return None
        back = G.GfxDoc.load(p)
        r = V.compare(sent, back)
        self.check(rec, what, r["ok"], missing=r["missing"][:20], differences=r["differences"][:40],
                   normalized=r["normalized_count"])
        (self.dir / f"compare_{name}{'_' + rec['scenario']}.json").write_text(json.dumps(r, indent=1),
                                                                               encoding="utf-8")
        return back

    # -- display pipeline ---------------------------------------------------
    def displays_pipeline(self, wanted: set[str]):
        """generate, edit, corpus and global in one batched pass through View Studio:
        reset -> global object import -> ONE create import of every display -> ONE export ->
        compare; then ONE update import + ONE create import (replace) -> ONE export -> compare."""
        from . import build as B
        recs = {k: {"scenario": k, "passed": False, "checks": []}
                for k in ("generate", "edit", "corpus", "global") if k in wanted}
        for k in recs:
            self.log(f"=== scenario {k} (batched)")
        docs: dict[str, G.GfxDoc] = {}
        owner: dict[str, dict] = {}

        def add(rec_key, name, doc):
            if rec_key in recs:
                docs[name] = doc
                owner[name] = recs[rec_key]

        ctx = T.Ctx("SCADA")
        spec = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        if "generate" in recs:
            every = G.GfxDoc.new(1920, 1080)
            for kind, params in _template_samples().items():
                every.insert(T.TEMPLATES[kind](ctx, f"E2E_{kind}", **params).element)
            raw, _ = GEN.generate(spec)
            fp, _ = GEN.motor_faceplate("Motor", auto=True, reset=True)
            for n, d in (("E2E_TEMPLATES", every), ("E2E_EXAMPLE", raw), ("E2E_FP_MOTOR", fp)):
                errs = [f for f in L.lint(d) if f["severity"] == "error"]
                self.check(recs["generate"], f"lint {n} has no errors", not errs, errors=errs[:10])
                add("generate", n, d)
        if "corpus" in recs:
            src = Path(CORPUS) if CORPUS else None
            if src and src.is_file():
                add("corpus", "E2E_CORPUS", G.GfxDoc.load(src))
            elif src:
                self.check(recs["corpus"], "corpus file present", False, path=str(src))
            else:
                recs["corpus"]["skipped"] = "FTVIEW_E2E_CORPUS not set"
        go_name, go = "E2E_GLOBAL", None
        if "global" in recs:
            go = G.GfxDoc.new(400, 300)
            base = T.pump(ctx, "GPump", 50, 50, label="#1", tags={"run": "#2", "fault": "#3"}).element
            B.set_parameters(base, {"#1": "", "#2": "", "#3": ""}, {"#1": "Label", "#2": "Run tag", "#3": "Fault tag"})
            go.insert(base)
            use = G.GfxDoc.new(800, 400)
            for i, (lbl, run, flt) in enumerate([
                    ("P-101A", "P101A_RUN", "P101A_FAULT"),
                    ("P-101B", "P101B_RUN", "P101B_FAULT")]):
                use.insert(B.reference_instance(go.get("GPump"), go_name, f"Inst{i + 1}", 150 + 200 * i, 100,
                                                {"#1": lbl, "#2": f"[SCADA]{run}", "#3": f"[SCADA]{flt}"}))
            self.check(recs["global"], "two instances have unique object names", not use.duplicate_names())
            add("global", "E2E_GLOBAL_USE", use)
        if "edit" in recs:
            base_doc, _ = GEN.generate(spec)
            add("edit", "E2E_EDIT_U", base_doc)                     # update-mode path
            add("edit", "E2E_EDIT_R", G.GfxDoc(base_doc.to_bytes()))  # replace path

        try:
            # 1. reset: every display of the sandbox (sandbox only), then the global object
            for n in self.studio.display_names():
                self.log(f"reset: deleting display {n}")
                self.studio.delete_display(n)
            if go is not None and (S.SE_PROJECTS / self.studio.app_name / "Global Objects" / f"{go_name}.ggfx").exists():
                self.studio.delete_display(go_name, node="Global Objects")
            # 2. global object definition
            if go is not None:
                gd = self.dir / "in_global_object"
                gd.mkdir(exist_ok=True)
                (gd / f"{go_name}.xml").write_bytes(go.to_bytes())
                log = self.studio.import_displays(PR.write_batch_import([str(gd / f"{go_name}.xml")], gd / "Batch.xml"),
                                                  "create", node="Global Objects")
                res = PR.parse_import_log(log)["displays"]
                self.check(recs["global"], "global object imported", res and "success" in (res[0]["result"] or "").lower(),
                           result=res[0]["result"] if res else None)
            # 3. one create import of every display, 4. one export, 5. compare
            self.import_batch(docs, "create", "", owner)
            out = self.export_all("")
            backs = {}
            for n, d in docs.items():
                rec = owner[n]
                label = "base round trip" if rec["scenario"] == "edit" else "round trip"
                backs[n] = self.round_trip(rec, f"{label} {n}", d, out, n)
            if "global" in recs and backs.get("E2E_GLOBAL_USE") is not None:
                for inst in ("Inst1", "Inst2"):
                    e = backs["E2E_GLOBAL_USE"].index().get(inst)
                    self.check(recs["global"], f"{inst} still linked to {go_name}.GPump",
                               e is not None and e.get("linkBaseObject") == f"{go_name}.GPump")
                gout = self.dir / "out_global_objects"
                self.studio.export_displays(gout, node="Global Objects")
                self.round_trip(recs["global"], "global object definition round trip", go, gout, go_name)
            # 6. edit round: update mode for _U, replace (delete + create) for _R
            if "edit" in recs and backs.get("E2E_EDIT_U") is not None and backs.get("E2E_EDIT_R") is not None:
                rec = recs["edit"]
                modified = G.GfxDoc(backs["E2E_EDIT_U"].to_bytes())
                before = G.bbox(modified.get("TK101"))
                modified.move("TK101", 40, 0)
                modified.get("FIT101_Lbl").set("caption", "FIT-101  Inlet flow (edited)")
                edited = G.GfxDoc(backs["E2E_EDIT_R"].to_bytes())
                edited.move("TK101", 40, 0)
                edited.get("FIT101_Lbl").set("caption", "FIT-101  Inlet flow (edited)")
                edited.delete("LIT102")
                dup = copy.deepcopy(edited.get("P101B"))
                dup.tail = None
                G.shift(dup, 0, 200)
                new_name = edited.insert(dup)[0][1]
                self.check(rec, "edited XML lint clean", not [f for f in L.lint(edited) if f["severity"] == "error"])
                self.import_batch({"E2E_EDIT_U": modified}, "update", "_update", owner)
                self.studio.delete_display("E2E_EDIT_R")
                self.import_batch({"E2E_EDIT_R": edited}, "create", "_replace", owner)
                out2 = self.export_all("_edit")
                upd = self.round_trip(rec, "update mode: exact round trip", modified, out2, "E2E_EDIT_U")
                if upd is not None:
                    moved = G.bbox(upd.get("TK101"))
                    self.check(rec, "update mode: move applied", abs(moved.left - before.left - 40) <= 2,
                               before=before.as_list(), after=moved.as_list())
                    self.check(rec, "update mode: caption applied", "edited" in upd.get("FIT101_Lbl").get("caption", ""))
                rep = self.round_trip(rec, "replace: exact round trip", edited, out2, "E2E_EDIT_R")
                if rep is not None:
                    self.check(rec, "replace: deleted object is gone", "LIT102" not in rep.index())
                    self.check(rec, "replace: inserted object present", new_name in rep.index(), name=new_name)
        except Exception as e:  # noqa: BLE001 - report on every scenario of the pipeline
            err = f"{type(e).__name__}: {e}"
            shot = self.shot("displays_pipeline_error")
            self.log(f"!!! display pipeline failed: {err}")
            for rec in recs.values():
                rec["error"] = err
                rec["traceback"] = traceback.format_exc()
                rec["screenshot"] = shot
        for k, rec in recs.items():
            if rec.get("skipped") and not rec.get("error"):
                rec["passed"] = True
                self.log(f"=== {k}: SKIPPED ({rec['skipped']})")
            else:
                rec["passed"] = not rec.get("error") and bool(rec["checks"]) and all(c["ok"] for c in rec["checks"])
                self.log(f"=== {k}: {'PASS' if rec['passed'] else 'FAIL'}")
            self.results.append(rec)

    def s_tags(self, rec):
        from . import tags as TG
        db = TG.TagDB()
        db.add_folder("E2E")
        db.add_analog("E2E\\Level", "memory", description="E2E analog", min=0, max=100, initial=12.5, units="%")
        db.add_digital("E2E\\Run", "memory", description="E2E digital", off_label="Stopped", on_label="Running")
        db.add_string("E2E\\Text", "memory", description="E2E string", length=40, initial="hello")
        db.add_analog("E2E\\DevPV", "device", description="E2E device", address="{::[PLC]FIT101_FLOW}",
                      min=0, max=200)
        db.add_digital("E2E\\DevRun", "device", description="E2E device digital",
                       address="[PLC]P201A_RUN", off_label="Stopped", on_label="Running")
        csv = self.dir / "E2E-Tags.CSV"
        db.write(csv)
        res = self.studio.import_tags(csv)
        shutil.copy(res["log"], self.dir / "TagImport.DBI")
        self.check(rec, "tag import: no errors or failures", not res["errors"] and not res["failures"],
                   errors=res["errors"][:10], counts=res["counts"])
        out = self.studio.export_tags(self.dir / "tags_out")
        back = TG.TagDB.read(out["tags"])
        cmp = TG.compare(db, back)
        self.check(rec, "tags round trip", cmp["ok"], **{k: v for k, v in cmp.items() if k != "ok"})

    def s_headless(self, rec):
        """No-UI update path: close Studio (sandbox only), edit a display through
        headless.update_display, verify, and check the refusal for added objects."""
        import time as _t
        from . import headless as H
        self.studio.close()
        self.check(rec, "sandbox not open in View Studio", H.SANDBOX not in H.apps_open_in_studio())
        names = H.list_displays(H.SANDBOX)
        target = next((n for n in ("E2E_EDIT_U", "E2E_FP_MOTOR", "E2E_EXAMPLE") if n in names), None)
        if target is None:
            self.check(rec, "a sandbox display to edit", False, displays=names)
            return
        live = G.GfxDoc.load(H.export_displays(H.SANDBOX, self.dir / "hl_before", [target])["files"][target])
        doc = G.GfxDoc(live.to_bytes())
        top = [o.get("name") for o in doc.root if G.is_object(o)]
        doc.move(top[0], 12, 8)
        texts = [o for o, _ in doc.iter_objects() if o.tag == "text" and o.get("caption")]
        if texts:
            texts[0].set("caption", texts[0].get("caption") + " (HL)")
        xml = self.dir / f"{target}.xml"
        xml.write_bytes(doc.to_bytes())
        t0 = _t.time()
        res = H.update_display(H.SANDBOX, target, xml)
        rec["headless_update_seconds"] = round(_t.time() - t0, 1)
        self.check(rec, f"headless update of {target} verified", res.get("verified"), result=res)
        self.check(rec, "VBA/global-object sections preserved", "preserved_from_backup" in res)
        bad = G.GfxDoc(doc.to_bytes())
        bad.insert(B_text("HL_NEW_OBJECT"))
        bad_xml = self.dir / f"{target}.adds_object.xml"
        bad_xml.write_bytes(bad.to_bytes())
        try:
            H.update_display(H.SANDBOX, target, bad_xml)
            refused = False
        except H.HeadlessError:
            refused = True
        self.check(rec, "refuses an XML that adds objects", refused)
        after = G.GfxDoc.load(H.export_displays(H.SANDBOX, self.dir / "hl_after", [target])["files"][target])
        r = V.compare(doc, after)
        self.check(rec, "display unchanged by the refused call", r["ok"], differences=r["differences"][:10])

    def report(self):
        passed = all(r["passed"] for r in self.results)
        data = {"passed": passed, "app": self.studio.app_name, "dir": str(self.dir), "results": self.results}
        (self.dir / "report.json").write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        md = [f"# ftview-mcp end-to-end run {self.dir.name}", "",
              f"Application: `{self.studio.app_name}`. Overall: **{'PASS' if passed else 'FAIL'}**", ""]
        for r in self.results:
            state = "SKIPPED" if r.get("skipped") else ("PASS" if r["passed"] else "FAIL")
            md.append(f"## {r['scenario']}: {state}")
            if r.get("error"):
                md.append(f"Error: `{r['error']}`")
            for c in r["checks"]:
                extra = {k: v for k, v in c.items() if k not in ("check", "ok") and v}
                md.append(f"- {'PASS' if c['ok'] else 'FAIL'} {c['check']}"
                          + (f" - `{json.dumps(extra, default=str)[:400]}`" if extra and not c["ok"] else ""))
            md.append("")
        (self.dir / "report.md").write_text("\n".join(md), encoding="utf-8")
        return data


def B_text(name: str):
    from . import build as B
    return B.text(name, 10, 10, 120, 20, "added", "#1E1E1E", 10)


def _template_samples() -> dict:
    """One instance of every template, laid out on a 1920x1080 display."""
    return {
        "pump": dict(x=100, y=120, label="P-1", tags={"run": "R1", "fault": "F1", "auto": "A1"}, faceplate="FP"),
        "blower": dict(x=220, y=120, label="B-1", tags={"run": "R2", "hand": "H2"}),
        "mixer": dict(x=340, y=100, label="M-1", tags={"run": "R3", "fault": "F3"}, shaft=80),
        "motor": dict(x=520, y=120, label="MTR", tags={"run": "R4"}, size=1.5),
        "valve": dict(x=640, y=120, label="XV-1", tags={"open": "O5", "closed": "C5", "fault": "F5"}, vertical=True),
        "tank": dict(x=760, y=100, label="TK-1", tags={"level": "L6", "hihi": "HH6", "hi": "H6", "lo": "LO6",
                                                         "lolo": "LL6"}),
        "analog": dict(x=100, y=400, tag="PV7", label="FIT-1", units="m3/h", alarm="AH7",
                       bar={"min": 0, "max": 10, "lo": 2, "hi": 8}),
        "setpoint": dict(x=100, y=500, tag="SP8", label="SP", units="%"),
        "status": dict(x=400, y=400, states=[{"when": "F9", "text": "FAULT"}, {"when": "R9", "text": "RUN"}],
                       default={"text": "OFF"}),
        "button": dict(x=400, y=500, caption="GO", action="momentary", tag="CMD10", enable="NOT F10"),
        "alarm": dict(x=600, y=400, expr="A11", priority=3),
        "mode": dict(x=630, y=400, expr="NOT A12"),
        "label": dict(x=600, y=500, text="Hello"),
        "panel": dict(x=1000, y=400, w=300, h=200, title="Panel"),
        "pipe": dict(points=[[1000, 700], [1200, 700], [1200, 800], [1300, 800]], arrow=True),
        "header": dict(width=1920, title="E2E templates"),
        "nav": dict(x=0, y=1030, width=1920, displays=["E2E_TEMPLATES", "E2E_EXAMPLE"], current="E2E_TEMPLATES"),
    }


DISPLAY_SCENARIOS = ("generate", "edit", "corpus", "global")
SCENARIOS = (*DISPLAY_SCENARIOS, "tags", "headless")


def run(scenarios: list[str] | None = None, workdir: str | Path | None = None) -> dict:
    wd = Path(workdir or ROOT.parent / "ftview-e2e")
    r = Run(wd)
    r.log(f"run dir {r.dir}")
    wanted = set(scenarios or SCENARIOS)
    unknown = wanted - set(SCENARIOS)
    if unknown:
        raise SystemExit(f"unknown scenario(s) {sorted(unknown)}; choose from {list(SCENARIOS)}")
    if wanted & {*DISPLAY_SCENARIOS, "tags"}:
        r.studio.ensure()
    if wanted & set(DISPLAY_SCENARIOS):
        r.displays_pipeline(wanted & set(DISPLAY_SCENARIOS))
    if "tags" in wanted:
        r.scenario("tags", r.s_tags)
    if "headless" in wanted:  # last: it closes View Studio (sandbox only)
        r.scenario("headless", r.s_headless)
    return r.report()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*")
    ap.add_argument("--workdir")
    a = ap.parse_args(argv)
    data = run(a.scenarios or None, a.workdir)
    print(json.dumps({"passed": data["passed"], "report": str(Path(data["dir"]) / "report.md")}))
    return 0 if data["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
