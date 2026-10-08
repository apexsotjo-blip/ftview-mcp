"""Headless, read-only access to the displays of a View SE application - no View Studio UI.

Uses View's own Gfx-to-XML conversion engine ("Rocket", RocketConversionManager.RktMgr in
Gfx2XmlWorkflowEngine.dll; the engine ViewPoint/View Designer use). It is a 32-bit in-process
COM server, so calls go through 32-bit PowerShell running headless_rocket.ps1.

Verified 7 Oct 2026: lists and exports displays of local apps (the sandbox and a production application) while
View Studio is open or closed; output is the same display XML as the Graphics Import Export
Wizard (schema Gfx-SE13; only difference: imageReference="noImage" is omitted).
Import is NOT available headless (see TESTING.md, "Headless").
Must not run against an application that is open in View Studio (see _guard).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

PS32 = Path(os.environ.get("WINDIR", r"C:\Windows")) / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
SCRIPT = Path(__file__).with_name("headless_rocket.ps1")


class HeadlessError(RuntimeError):
    pass


def apps_open_in_studio() -> set[str]:
    """Applications currently open in FactoryTalk View Studio, read from the Windows Running
    Object Table (Studio registers objects named '...<App>...' / '...@<App>'). No UI involved."""
    import re
    import pythoncom
    pythoncom.CoInitialize()
    rot = pythoncom.GetRunningObjectTable()
    ctx = pythoncom.CreateBindCtx(0)
    apps = set()
    for mk in rot:
        try:
            name = mk.GetDisplayName(ctx, None)
        except Exception:  # noqa: BLE001
            continue
        m = re.match(r"^!\{[0-9A-F-]{36}\}(.+)\{[0-9A-F-]{36}\}$", name, re.I) or \
            re.match(r"^[0-9A-F-]{36}@(.+)$", name, re.I)
        if m:
            apps.add(m.group(1))
    return apps


def _guard(app: str):
    """The conversion engine attaches to a running View Studio through the Running Object Table.
    If Studio has this application open, it opens the exported displays *inside Studio* and leaves
    them open (observed 7 Oct 2026: later imports then fail with 'Unable to perform an import when
    the display is open'). So refuse in that case."""
    if app.lower() in {a.lower() for a in apps_open_in_studio()}:
        raise HeadlessError(f"'{app}' is open in FactoryTalk View Studio; headless export would open its "
                            "displays inside Studio and lock them. Close the application in Studio "
                            "(or use the wizard export) and retry.")


def _run(args: list[str], timeout: float = 600) -> dict:
    if not PS32.is_file():
        raise HeadlessError(f"32-bit PowerShell not found at {PS32}")
    cmd = [str(PS32), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.strip().startswith("{")), None)
    if line is None:
        raise HeadlessError(f"no result from conversion engine (exit {p.returncode}): {p.stderr.strip()[:800]}")
    data = json.loads(line)
    if not data.get("ok"):
        raise HeadlessError(f"conversion engine failed for {args}: {data.get('error')}")
    return data


def list_displays(app: str, server: str | None = None, directory: str = "Local") -> list[str]:
    """Display names of an SE application (Local = SE local station app)."""
    _guard(app)
    d = _run(["-Action", "list", "-App", app, "-Server", server or app, "-Directory", directory])
    return list(d.get("displays") or [])


UPDATE_SCRIPT = Path(__file__).with_name("headless_update.ps1")
SANDBOX = os.environ.get("FTVIEW_SANDBOX_APP", "FTVMCP_SANDBOX")
SE_PROJECTS = Path(os.environ.get("FTVIEW_SE_PROJECTS",
                                  r"C:\Users\Public\Documents\RSView Enterprise\SE\HMI Projects"))


def _display_file(app: str, display: str) -> Path:
    for sub in ("GFX", "Gfx"):
        p = SE_PROJECTS / app / sub / f"{display}.gfx"
        if p.is_file():
            return p
    raise HeadlessError(f"display file for '{display}' not found in {SE_PROJECTS / app}")


def update_display(app: str, display: str, xml_path: str | Path, allow_app: str | None = None,
                   backup_dir: str | Path | None = None, timeout: float = 300) -> dict:
    """Apply an edited display XML to a live display WITHOUT View Studio (update mode).

    Safety: sandbox only unless allow_app == app; refuses if the app is open in View Studio;
    refuses if the XML contains objects the live display does not have (the headless engine
    cannot create objects); backs up the .gfx first; afterwards exports the display headless and
    compares it with the XML - on any difference the backup is restored and an error raised.
    Objects that exist live but not in the XML are kept (reported as 'kept_not_in_xml')."""
    import datetime as dt
    import shutil
    import tempfile
    from . import gfx as G
    from . import verify as V

    if app != SANDBOX and allow_app != app:
        raise HeadlessError(f"refusing to modify '{app}': only the sandbox '{SANDBOX}' unless allow_app='{app}'")
    _guard(app)
    xml = Path(xml_path).resolve()
    want = G.GfxDoc.load(xml)
    if want.validate():
        raise HeadlessError("XML fails schema validation; fix it first")
    work = Path(tempfile.mkdtemp(prefix="ftv_upd_"))
    before = export_displays(app, work / "before", [display])
    if display not in before["files"]:
        raise HeadlessError(f"display '{display}' not found in '{app}'")
    live = G.GfxDoc.load(before["files"][display])
    new_objects = sorted(set(want.index()) - set(live.index()))
    if new_objects:
        raise HeadlessError(f"the XML adds objects the live display does not have ({new_objects[:10]}); headless "
                            "update cannot create objects - use the View Studio import (ftv_studio_replace_display)")
    gfx_file = _display_file(app, display)
    bdir = Path(backup_dir) if backup_dir else Path(__file__).resolve().parents[2] / "ftview-backups" / app
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / f"{display}.{dt.datetime.now():%Y%m%d_%H%M%S}.gfx"
    shutil.copy2(gfx_file, backup)
    if not PS32.is_file():
        raise HeadlessError(f"32-bit PowerShell not found at {PS32}")
    cmd = [str(PS32), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(UPDATE_SCRIPT),
           "-App", app, "-Display", display, "-Xml", str(xml)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.strip().startswith("{")), None)
        res = json.loads(line) if line else {"ok": False, "error": f"no result: {p.stderr.strip()[:500]}"}
    except subprocess.TimeoutExpired:
        res = {"ok": False, "error": f"timed out after {timeout}s"}
    if not res.get("ok"):
        shutil.copy2(backup, gfx_file)
        raise HeadlessError(f"headless update failed, display restored from backup: {res.get('error')} {res.get('log', '')}")
    try:
        merged = _merge_missing_storage(backup, gfx_file)
        _check_preserved(backup, gfx_file)
    except Exception as e:  # noqa: BLE001
        shutil.copy2(backup, gfx_file)
        raise HeadlessError(f"could not preserve VBA/global-object data, display restored from backup: {e}") from e
    after = export_displays(app, work / "after", [display])
    got = G.GfxDoc.load(after["files"][display])
    cmp = V.compare(want, got)
    if not cmp["ok"]:
        shutil.copy2(backup, gfx_file)
        raise HeadlessError("verification failed, display restored from backup: "
                            + json.dumps({"differences": cmp["differences"][:10], "missing": cmp["missing"][:10]}))
    return {"app": app, "display": display, "verified": True, "backup": str(backup), "preserved_from_backup": merged,
            "normalized": cmp["normalized_count"], "kept_not_in_xml": cmp["extra"][:50], "log": res.get("log")}


# Sections of a .gfx (OLE compound file) that the headless engine's save does not write. Observed
# 8 Oct 2026: the save keeps Contents, Settings, ls<LCID> (strings) and caches, but drops DSI_VBA
# (the VBA project) and GOPerTables. Update mode does not change the object set, so copying them
# back from the backup restores exactly what View Studio's own import keeps ("VBA left intact").
def _merge_missing_storage(src: Path, dst: Path) -> list[str]:
    import pythoncom
    from win32com import storagecon as sc
    pythoncom.CoInitialize()
    s = pythoncom.StgOpenStorage(str(src), None, sc.STGM_READ | sc.STGM_SHARE_DENY_WRITE, None, 0)
    d = pythoncom.StgOpenStorage(str(dst), None, sc.STGM_READWRITE | sc.STGM_SHARE_EXCLUSIVE, None, 0)
    have = {st[0] for st in d.EnumElements()}
    copied = []
    for st in s.EnumElements():
        if st[0] not in have:
            s.MoveElementTo(st[0], d, st[0], sc.STGMOVE_COPY)
            copied.append(st[0])
    d.Commit(0)
    del d, s
    return copied


def _check_preserved(src: Path, dst: Path) -> None:
    """Every top-level section of the original must exist; the VBA project must be byte-identical."""
    import olefile
    a, b = olefile.OleFileIO(str(src)), olefile.OleFileIO(str(dst))
    try:
        top_a = {e[0] for e in a.listdir(streams=True, storages=True)}
        top_b = {e[0] for e in b.listdir(streams=True, storages=True)}
        missing = sorted(top_a - top_b)
        if missing:
            raise HeadlessError(f"sections missing after save: {missing}")
        for e in a.listdir(streams=True, storages=False):
            if e[0] == "DSI_VBA":
                name = "/".join(e)
                if not b.exists(name) or a.openstream(name).read() != b.openstream(name).read():
                    raise HeadlessError(f"VBA stream changed: {name}")
    finally:
        a.close()
        b.close()


def export_displays(app: str, out_dir: str | Path, displays: list[str] | None = None, server: str | None = None,
                    directory: str = "Local", timeout: float = 900) -> dict:
    """Export displays (all when None) to <out_dir>/<display>.xml. Read-only for the app.
    Refuses while the app is open in View Studio (see _guard)."""
    _guard(app)
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for n in displays or []:
        if "|" in n:
            raise HeadlessError(f"display name {n!r} contains '|'")
    args = ["-Action", "export", "-App", app, "-Server", server or app, "-Directory", directory, "-Out", str(out)]
    if displays:
        args += ["-Displays", "|".join(displays)]
    d = _run(args, timeout)
    names = d.get("requested") or []
    files = {n: str(out / f"{n}.xml") for n in names if (out / f"{n}.xml").exists()}
    missing = [n for n in names if n not in files]
    return {"app": app, "out": str(out), "files": files, "missing": missing,
            "warnings": d.get("warnings") or "", "errors": d.get("errors") or ""}
