"""Drive FactoryTalk View Studio (SE local) through UI Automation, for end-to-end tests.

What it automates (all through View's own wizards, so results are exactly what a
user would get):
* Graphics Import Export Wizard: batch import of display XML (create / update mode),
  export of all displays to XML;
* Tag Import and Export Wizard: export / import of HMI tag + alarm CSV files;
* deleting displays (to reset the sandbox between runs);
* screenshots.

Safety:
* refuses to touch an application other than the configured one (default
  FTVMCP_SANDBOX) unless `allow_app` names it explicitly;
* never closes View Studio or an application the user has open; if Studio is
  showing a different application it stops with an error;
* every typed path is read back and verified before a wizard's Finish is pressed;
* the wizard's remembered folders (HKCU ...\\DisplaysImportExport) are restored.

Needs an interactive desktop session (it moves focus and occasionally the mouse).
View Studio is 32-bit; we use the UIA backend for trees/lists/menus and the win32
backend (message based) for dialog buttons and edits.
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
import warnings
import winreg
from pathlib import Path

warnings.filterwarnings("ignore", message=".*32-bit application should be automated.*")

VSTUDIO = Path(os.environ.get("FTVIEW_VSTUDIO",
                              r"C:\Program Files (x86)\Rockwell Software\RSView Enterprise\VStudio.exe"))
SE_PROJECTS = Path(os.environ.get("FTVIEW_SE_PROJECTS",
                                  r"C:\Users\Public\Documents\RSView Enterprise\SE\HMI Projects"))
RSVIEW_TEMP = SE_PROJECTS.parent.parent / "Temp"  # ...\RSView Enterprise\Temp (tag import logs)
SANDBOX = os.environ.get("FTVIEW_SANDBOX_APP", "FTVMCP_SANDBOX")
REG_KEY = r"Software\Rockwell Software\RSView Enterprise\DisplaysImportExport"
MAIN_TITLE = re.compile(r"FactoryTalk View Studio.*")


class StudioError(RuntimeError):
    pass


def _desktop(backend="win32"):
    from pywinauto import Desktop
    return Desktop(backend=backend)


def _windows(backend: str = "win32", attempts: int = 5):
    """Top-level windows. pywinauto builds wrappers for all windows at once and raises
    InvalidWindowHandle if one closes meanwhile (seen when a dialog closes); retry."""
    last = None
    for _ in range(attempts):
        try:
            return _desktop(backend).windows()
        except Exception as e:  # noqa: BLE001 - InvalidWindowHandle / COM errors
            last = e
            time.sleep(0.2)
    raise StudioError(f"could not list windows: {last}")


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("&", "")).strip().lower()


def wait(fn, timeout: float, what: str, interval: float = 0.5):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            r = fn()
            if r:
                return r
        except Exception as e:  # noqa: BLE001 - UI trees change under us
            last = e
        time.sleep(interval)
    raise StudioError(f"timed out after {timeout:.0f}s waiting for {what}" + (f" ({last})" if last else ""))


def screenshot(path: str | Path) -> Path:
    """Full-screen PNG via .NET (works without extra Python packages)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
          "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
          "$m=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
          "$g=[System.Drawing.Graphics]::FromImage($m);"
          "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
          f"$m.Save('{path}')")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return path


_SEND_KEYS_SPECIAL = set("+^%~(){}[]")


def _keys_escape(text: str) -> str:
    return "".join("{" + c + "}" if c in _SEND_KEYS_SPECIAL else c for c in text)


# ---------------------------------------------------------------------------
# Dialog helpers (win32 backend)
# ---------------------------------------------------------------------------

def dialogs(title_sub: str, exact: bool = False):
    out = []
    for w in _windows():
        try:
            t = w.window_text()
            if not w.is_visible():
                continue
            if (exact and t == title_sub) or (not exact and title_sub.lower() in (t or "").lower()):
                out.append(w)
        except Exception:  # noqa: BLE001
            continue
    return out


def wait_dialog(title_sub: str, timeout: float = 30, exact: bool = False):
    return wait(lambda: (dialogs(title_sub, exact) or [None])[0], timeout, f"dialog '{title_sub}'")


def wait_gone(title_sub: str, timeout: float = 60, exact: bool = False):
    wait(lambda: not dialogs(title_sub, exact), timeout, f"dialog '{title_sub}' to close")


def visible_children(dlg, cls: str | None = None):
    return [c for c in dlg.descendants() if c.is_visible() and (cls is None or c.class_name() == cls)]


def button(dlg, text: str):
    want = _norm(text)
    for b in visible_children(dlg, "Button"):
        if _norm(b.window_text()) == want:
            return b
    raise StudioError(f"button '{text}' not found on '{dlg.window_text()}'; have "
                      f"{[b.window_text() for b in visible_children(dlg, 'Button')]}")


def click(dlg, text: str, settle: float = 0.35):
    b = button(dlg, text)
    if not b.is_enabled():
        raise StudioError(f"button '{text}' is disabled on '{dlg.window_text()}'")
    b.click()
    time.sleep(settle)


def type_verified(dlg, edit, text: str):
    """Type into an edit through the keyboard (wizards validate typed input) and verify."""
    dlg.set_focus()
    edit.set_focus()
    edit.type_keys("^a{BACKSPACE}", set_foreground=True)
    edit.type_keys(_keys_escape(text), with_spaces=True, set_foreground=True, pause=0.005)
    time.sleep(0.3)
    got = edit.window_text()
    if got.strip().rstrip("\\").lower() != text.strip().rstrip("\\").lower():
        raise StudioError(f"edit on '{dlg.window_text()}' reads {got!r} after typing {text!r}")


def file_dialog(title_sub: str, path: str, timeout: float = 20):
    """Fill a standard Open dialog's file-name box and confirm with Enter."""
    od = wait_dialog(title_sub, timeout)
    ed = wait(lambda: [e for e in visible_children(od, "Edit")][0], 10, "file name box")
    ed.set_edit_text(path)
    od.set_focus()
    ed.set_focus()
    ed.type_keys("{ENTER}", set_foreground=True)
    wait_gone(title_sub, 15)


def log_done(path: Path) -> bool:
    """View writes DisplaysImport/Export.txt progressively; its last line is
    "This status file can be found at '...'"."""
    if not path.exists():
        return False
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    except (OSError, UnicodeDecodeError):
        return False
    return "This status file can be found" in text


def _has_button(dlg, text: str) -> bool:
    want = _norm(text)
    return any(_norm(b.window_text()) == want for b in visible_children(dlg, "Button"))


def _is_project_page(dlg) -> bool:
    return any("project that contains the tags" in _norm(s.window_text())
               or "project you want to import" in _norm(s.window_text())
               or _norm(s.window_text()).startswith("project:")
               for s in visible_children(dlg, "Static")) and bool(visible_children(dlg, "Edit"))


def close_log_window(file_name: str, timeout: float = 5):
    """View opens its import/export log in Notepad right after finishing; wait briefly for it
    (event-driven, not a fixed sleep) and close it."""
    with contextlib.suppress(StudioError):
        wait(lambda: dialogs(f"{file_name} - Notepad"), timeout, f"{file_name} window", interval=0.25)
    close_notepad(file_name)


def close_notepad(file_name: str):
    for w in dialogs(f"{file_name} - Notepad"):
        with contextlib.suppress(Exception):
            w.close()


def parse_dbi(path: str | Path) -> dict:
    """Parse View's tag import log (<app>.DBI): errors and success/failure counts."""
    text = Path(path).read_text(errors="replace")
    errors, cur = [], None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("ERROR:"):
            cur = [s[6:].strip()]
            errors.append(cur)
        elif s.startswith(("INFORMATION:", "WARNING:")):
            cur = None
        elif cur is not None and s:
            cur.append(s)
    counts = {m.group(1).strip(): int(m.group(2))
              for m in re.finditer(r"INFORMATION:\s+(.+?)\s{2,}(\d+)\s*$", text, re.M)}
    return {"errors": [" ".join(e) for e in errors], "counts": counts,
            "failures": sum(v for k, v in counts.items() if "fail" in k.lower())}


@contextlib.contextmanager
def preserved_registry(values: dict[str, str]):
    """Temporarily set wizard folders under HKCU; restore the user's values afterwards."""
    old: dict[str, str | None] = {}
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY)
    try:
        for k, v in values.items():
            try:
                old[k] = winreg.QueryValueEx(key, k)[0]
            except FileNotFoundError:
                old[k] = None
            winreg.SetValueEx(key, k, 0, winreg.REG_SZ, v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, k)
            else:
                winreg.SetValueEx(key, k, 0, winreg.REG_SZ, v)
        winreg.CloseKey(key)


# ---------------------------------------------------------------------------
# Studio session
# ---------------------------------------------------------------------------

class Studio:
    def __init__(self, app_name: str = SANDBOX, allow_app: str | None = None, workdir: str | Path | None = None,
                 log=print):
        if app_name != SANDBOX and allow_app != app_name:
            raise StudioError(f"refusing to automate '{app_name}': only the sandbox '{SANDBOX}' is allowed "
                              f"unless allow_app='{app_name}' is passed explicitly")
        self.app_name = app_name
        self.workdir = Path(workdir or Path(__file__).resolve().parents[2] / "ftview-e2e")
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self._uia_app = None

    # -- session -------------------------------------------------------
    @property
    def sed(self) -> Path:
        return SE_PROJECTS / self.app_name / f"{self.app_name}.sed"

    def main(self):
        from pywinauto import Application
        if self._uia_app is None:
            self._uia_app = Application(backend="uia").connect(title_re=MAIN_TITLE.pattern, timeout=10)
        return self._uia_app.window(title_re=MAIN_TITLE.pattern)

    def running(self) -> bool:
        return any(MAIN_TITLE.match(w.window_text() or "") for w in _windows())

    def tree_texts(self) -> list[str]:
        return [t.window_text() for t in self.main().descendants(control_type="TreeItem")]

    def ensure(self, launch_timeout: float = 240):
        """Make sure View Studio is running with our application open."""
        if not self.running():
            self.log(f"launching View Studio and opening {self.app_name}")
            self._launch_and_open(launch_timeout)
        elif not self.tree_texts() and (self._type_selection() is not None or dialogs("New/Open Site Edition")):
            # Studio is up but still at its startup dialogs (no application open yet): continue
            self.log(f"View Studio is at its startup dialogs; opening {self.app_name}")
            self._launch_and_open(launch_timeout, launch=False)
        texts = self.tree_texts()
        if self.app_name not in texts:
            raise StudioError(f"View Studio is open but not on '{self.app_name}' (tree shows "
                              f"{texts[:4]}). Close View Studio or open '{self.app_name}' first; "
                              "the harness never switches applications on its own.")
        self.dismiss_stray_dialogs()
        return self

    def _type_selection(self):
        from pywinauto import Desktop
        spec = Desktop(backend="uia").window(title_re=MAIN_TITLE.pattern).child_window(
            title="Application Type Selection", control_type="Window")
        try:
            return spec if spec.exists(timeout=1) else None  # specification: supports child_window()
        except Exception:  # noqa: BLE001
            return None

    def _launch_and_open(self, timeout, launch: bool = True):
        if not self.sed.is_file():
            raise StudioError(f"application '{self.app_name}' not found at {self.sed}")
        if launch:
            subprocess.Popen([str(VSTUDIO)])
        from pywinauto import Desktop
        sel = Desktop(backend="uia").window(title_re=MAIN_TITLE.pattern)  # a specification, not a wrapper
        wait(lambda: sel.exists(), 120, "View Studio window")
        if not dialogs("New/Open Site Edition"):
            dlg = wait(self._type_selection, 120, "Application Type Selection")
            dlg.child_window(title="View Site Edition (Local Station)", control_type="ListItem").select()
            dlg.child_window(title="Continue", control_type="Button").invoke()
        nd = wait_dialog("New/Open Site Edition", 120)
        u = _desktop("uia").window(handle=nd.handle)
        item = wait(lambda: [i for i in u.descendants(control_type="ListItem")
                             if i.window_text() == self.app_name][0], 20, f"'{self.app_name}' in app list")
        item.select()
        time.sleep(0.5)
        click(nd, "Open")
        wait_gone("New/Open Site Edition", 60)
        wait(lambda: self.app_name in self.tree_texts() and not dialogs("Opening application"), timeout,
             "application to open")
        time.sleep(3)

    def close(self, timeout: float = 60):
        """Close View Studio WITHOUT saving (answers 'No' to save prompts). Only when it shows the
        configured application - never closes the user's work on another application."""
        if not self.running():
            return
        texts = self.tree_texts()
        if texts and self.app_name not in texts:
            raise StudioError(f"View Studio shows another application ({texts[:3]}); not closing it")
        for w in _windows():
            with contextlib.suppress(Exception):
                if MAIN_TITLE.match(w.window_text() or ""):
                    w.post_message(0x0010)  # WM_CLOSE
        end = time.time() + timeout
        while time.time() < end and self.running():
            for d in dialogs("FactoryTalk View Studio", exact=True):
                with contextlib.suppress(Exception):
                    click(d, "No")
            time.sleep(0.5)
        if self.running():
            raise StudioError("View Studio did not close")
        self._uia_app = None

    def dismiss_stray_dialogs(self):
        for w in dialogs("Add Controller Instruction Faceplates", exact=True):
            with contextlib.suppress(Exception):
                click(w, "Clear All")
                click(w, "OK")

    # -- context menus --------------------------------------------------
    def _tree_item(self, text: str, occurrence: int = 0):
        items = [t for t in self.main().descendants(control_type="TreeItem") if t.window_text() == text]
        if len(items) <= occurrence:
            raise StudioError(f"tree item '{text}' not found")
        return items[occurrence]

    def context_menu(self, node: str, item_prefix: str, attempts: int = 4):
        """Right-click a tree node and pick a menu item. Right after Studio opens an application the
        menus are not populated yet, so retry a few times."""
        from pywinauto import keyboard
        want = _norm(item_prefix)
        for attempt in range(attempts):
            main = self.main()
            main.set_focus()
            self._tree_item(node).click_input(button="right")
            time.sleep(1.0)
            for w in _windows("uia"):
                try:
                    for mi in w.descendants(control_type="MenuItem"):
                        if _norm(mi.window_text()).startswith(want):
                            mi.click_input()
                            time.sleep(1.0)
                            return
                except Exception:  # noqa: BLE001
                    continue
            keyboard.send_keys("{ESC}")
            time.sleep(3)
        raise StudioError(f"context menu item '{item_prefix}' not found on '{node}' after {attempts} attempts")

    def expand(self, node: str):
        it = self._tree_item(node)
        with contextlib.suppress(Exception):
            it.expand()
        time.sleep(0.8)

    def display_names(self) -> list[str]:
        gfx = SE_PROJECTS / self.app_name / "GFX"
        if not gfx.is_dir():
            gfx = SE_PROJECTS / self.app_name / "Gfx"
        return sorted(p.stem for p in gfx.glob("*.gfx"))

    # -- graphics -------------------------------------------------------
    def _open_gfx_wizard(self, node="Displays"):
        for w in dialogs("Graphics Import Export Wizard"):
            with contextlib.suppress(Exception):
                click(w, "Cancel")
        self.context_menu(node, "Import and Export")
        return wait_dialog("Graphics Import Export Wizard", 30)

    def import_displays(self, batch_file: str | Path, mode: str = "create", node: str = "Displays",
                        timeout: float = 600) -> Path:
        """Import a batch file. mode: 'create' (new objects; existing names are skipped by View)
        or 'update' (update objects with matching names). Returns the import log path."""
        if mode not in ("create", "update"):
            raise StudioError(f"mode must be 'create' or 'update', got {mode!r} (for a full replace, delete "
                              "the display and use 'create'; see ftv_studio_replace_display)")
        batch = Path(batch_file).resolve()
        if not batch.is_file():
            raise StudioError(f"batch file not found: {batch}")
        log_path = batch.parent / "DisplaysImport.txt"
        if log_path.exists():
            log_path.unlink()
        self.ensure()
        with preserved_registry({"XmlImportMultiBatchXmlFile": str(batch.parent)}):
            w = self._open_gfx_wizard(node)
            click(w, "Import graphic information into displays")
            click(w, "Next >")
            w = wait_dialog("Graphics Import Export Wizard - Backup", 15)
            click(w, "No")
            click(w, "Next >")
            w = wait_dialog("Import File Type", 15)
            click(w, "Multiple displays batch import file")
            click(w, "Next >")
            w = wait_dialog("Multiple Import File", 15)
            click(w, "...", settle=1.5)
            file_dialog("Select Multiple Displays Batch Import File", str(batch))
            w = wait_dialog("Multiple Import File", 10)
            got = visible_children(w, "Edit")[0].window_text()
            if Path(got).resolve() != batch:
                raise StudioError(f"wizard shows batch file {got!r}, expected {batch}")
            click(w, "Create new objects on the display" if mode == "create"
                  else "Update existing objects on the display")
            click(w, "Finish", settle=2)
            wait(lambda: log_done(log_path) and not dialogs("Graphics Import Export Wizard"), timeout,
                 "import to finish (DisplaysImport.txt complete)", interval=0.5)
        close_log_window("DisplaysImport.txt")
        return log_path

    def export_displays(self, out_dir: str | Path, node: str = "Displays", timeout: float = 600) -> list[Path]:
        """Export every display of the application to <out_dir>/<display>.xml."""
        out = Path(out_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        log_path = out / "DisplaysExport.txt"
        if log_path.exists():
            log_path.unlink()
        started = time.time()
        self.ensure()
        with preserved_registry({"XmlExportLocation": str(out)}):
            w = self._open_gfx_wizard(node)
            click(w, "Export graphic information from displays")
            click(w, "Next >")
            w = wait_dialog("Displays to Export", 15)
            click(w, "Next >")
            w = wait_dialog("Export Folder", 15)
            got = visible_children(w, "Edit")[0].window_text()
            if Path(got).resolve() != out:
                raise StudioError(f"export folder shows {got!r}, expected {out}; aborting before Finish")
            click(w, "Finish", settle=2)
            wait(lambda: log_done(log_path) and not dialogs("Graphics Import Export Wizard"), timeout,
                 "export to finish (DisplaysExport.txt complete)", interval=0.5)
        close_log_window("DisplaysExport.txt")
        return sorted(p for p in out.glob("*.xml") if p.stat().st_mtime >= started - 1)

    def delete_display(self, name: str, node: str = "Displays"):
        """Delete one display (or global object) from the application: select it in the Explorer
        tree and press Delete (about 3 s; Studio has no multi-select delete), falling back to the
        context menu."""
        self.ensure()
        self.expand(node)
        ext = "*.ggfx" if node == "Global Objects" else "*.gfx"
        folder = SE_PROJECTS / self.app_name

        def gone():
            return not any(p.stem.lower() == name.lower() for p in folder.rglob(ext))

        main = self.main()
        main.set_focus()
        item = self._tree_item(name)
        item.click_input()
        time.sleep(0.3)
        item.type_keys("{DEL}", set_foreground=True)
        self._answer_confirmations(until=gone)
        if gone():
            return
        self.log(f"Delete key did not remove '{name}'; using the context menu")
        self._tree_item(name).click_input(button="right")
        time.sleep(1.0)
        hit = None
        for w in _windows("uia"):
            try:
                for mi in w.descendants(control_type="MenuItem"):
                    if _norm(mi.window_text()) == "delete":
                        hit = mi
                        break
            except Exception:  # noqa: BLE001
                continue
            if hit:
                break
        if hit is None:
            raise StudioError(f"no Delete menu item for '{name}'")
        hit.click_input()
        self._answer_confirmations(until=gone)
        wait(gone, 30, f"'{name}' to be deleted")

    def _answer_confirmations(self, settle: float = 1.5, until=None, timeout: float = 30):
        """Click Yes/OK on View's confirmation boxes (deleting a global object asks twice:
        delete? and 'This action will break the link to any reference objects'). Returns when
        `until()` is true, or after `settle` seconds without a new box."""
        end = time.time() + settle
        hard = time.time() + timeout
        while time.time() < min(end, hard):
            if until is not None and until():
                return
            for w in dialogs("FactoryTalk View Studio", exact=True):
                for t in ("Yes", "OK"):
                    with contextlib.suppress(StudioError, Exception):
                        click(w, t)
                        end = time.time() + settle
                        break
            time.sleep(0.25)
        """Click Yes/OK on View's confirmation boxes (deleting a global object asks twice:
        delete? and 'This action will break the link to any reference objects')."""
        end = time.time() + settle
        while time.time() < end:
            boxes = dialogs("FactoryTalk View Studio", exact=True)
            for w in boxes:
                for t in ("Yes", "OK"):
                    with contextlib.suppress(StudioError, Exception):
                        click(w, t)
                        end = time.time() + settle
                        break
            time.sleep(0.5)

    # -- tags -----------------------------------------------------------
    def _tag_wizard(self):
        for w in dialogs("Tag Import and Export Wizard", exact=True):
            with contextlib.suppress(Exception):
                click(w, "Cancel")
        main = self.main()
        main.set_focus()
        main.child_window(title="Tools", control_type="MenuItem").wrapper_object().click_input()
        time.sleep(1.0)
        for w in _windows("uia"):
            try:
                for mi in w.descendants(control_type="MenuItem"):
                    if _norm(mi.window_text()).startswith("tag import and export"):
                        mi.click_input()
                        return wait_dialog("Tag Import and Export Wizard", 60, exact=True)
            except Exception:  # noqa: BLE001
                continue
        raise StudioError("Tag Import and Export Wizard menu item not found")

    def _tag_page(self, predicate, what: str, timeout: float = 30):
        """The tag wizard rebuilds its window between pages, so re-find it and wait until the
        expected page is showing (stale handles caused intermittent InvalidWindowHandle errors)."""
        def probe():
            for d in dialogs("Tag Import and Export Wizard", exact=True):
                if predicate(d):
                    return d
            return None
        return wait(probe, timeout, f"tag wizard {what}", interval=0.25)

    def _tag_operation(self, w, op_prefix: str):
        u = _desktop("uia").window(handle=w.handle)
        cb = u.descendants(control_type="ComboBox")[0]
        cb.expand()
        time.sleep(0.5)
        item = [i for i in cb.descendants(control_type="ListItem") if _norm(i.window_text()).startswith(_norm(op_prefix))]
        if not item:
            raise StudioError(f"tag wizard operation '{op_prefix}' not found")
        item[0].select()
        time.sleep(0.5)

    def _tag_project(self, w, sed: Path):
        u = _desktop("uia").window(handle=w.handle)
        cb = wait(lambda: u.descendants(control_type="ComboBox")[0], 10, "project type")
        cb.expand()
        time.sleep(0.5)
        [i for i in cb.descendants(control_type="ListItem") if "Site" in i.window_text()][0].select()
        time.sleep(0.5)
        ed = [e for e in visible_children(w, "Edit")][0]
        type_verified(w, ed, str(sed))

    def _retry_once(self, fn, what: str):
        """Wizard windows are recreated between pages; a stale handle (InvalidWindowHandle) was seen
        in 1 of 3 runs. Cancel any open tag wizard and retry once (tag operations are idempotent)."""
        try:
            return fn()
        except StudioError:
            raise
        except Exception as e:  # noqa: BLE001 - pywinauto raises many types
            self.log(f"{what}: {type(e).__name__}: {e}; retrying once")
            for w in dialogs("Tag Import and Export Wizard", exact=True):
                with contextlib.suppress(Exception):
                    click(w, "Cancel")
            for w in dialogs("FactoryTalk View - Database Import", exact=True):
                with contextlib.suppress(Exception):
                    w.close()
            time.sleep(3)
            return fn()

    def export_tags(self, out_dir: str | Path, sed: str | Path | None = None, timeout: float = 180) -> dict:
        """Export HMI tags + legacy HMI tag alarms of an SE project (.sed; read-only) to CSV."""
        return self._retry_once(lambda: self._export_tags(out_dir, sed, timeout), "tag export")

    def import_tags(self, tags_csv: str | Path, alarms_csv: str | Path | None = None,
                    sed: str | Path | None = None, timeout: float = 180, on_exists: str = "update") -> dict:
        """Import HMI tag CSV (and optionally alarm CSV) into the application's tag database."""
        if on_exists not in ("update", "skip"):
            raise StudioError("on_exists must be 'update' or 'skip'")
        return self._retry_once(lambda: self._import_tags(tags_csv, alarms_csv, sed, timeout, on_exists),
                                "tag import")

    def _export_tags(self, out_dir, sed, timeout) -> dict:
        sed = Path(sed or self.sed)
        out = Path(out_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        tags, alarms = out / f"{sed.stem}-Tags.CSV", out / f"{sed.stem}-Alarms.CSV"
        for p in (tags, alarms):
            if p.exists():
                p.unlink()
        self.ensure()
        w = self._tag_wizard()
        self._tag_operation(w, "Export FactoryTalk View tag database")
        click(w, "Next >")
        w = self._tag_page(_is_project_page, "project page")
        self._tag_project(w, sed)
        click(w, "Next >")
        w = self._tag_page(lambda d: len(visible_children(d, "Edit")) >= 2 and _has_button(d, "Finish"),
                           "destination files page")
        eds = visible_children(w, "Edit")
        type_verified(w, eds[0], str(tags))
        type_verified(w, eds[1], str(alarms))
        click(w, "Finish", settle=1)
        wait(lambda: tags.exists() and not dialogs("Tag Import and Export Wizard", exact=True), timeout,
             "tag export to finish", interval=0.5)
        return {"tags": str(tags), "alarms": str(alarms) if alarms.exists() else None}

    def _import_tags(self, tags_csv, alarms_csv, sed, timeout, on_exists) -> dict:
        sed = Path(sed or self.sed)
        if sed.resolve() != self.sed.resolve():
            raise StudioError("tag import is only allowed into the configured application")
        self.ensure()
        w = self._tag_wizard()
        self._tag_operation(w, "Import FactoryTalk View tag CSV files")
        click(w, "Next >")
        w = self._tag_page(_is_project_page, "project page")
        self._tag_project(w, sed)
        click(w, "Next >")
        w = self._tag_page(lambda d: _has_button(d, "Tags"), "import files page")
        return self._tag_import_pages(w, Path(tags_csv), Path(alarms_csv) if alarms_csv else None,
                                      timeout, on_exists)

    def _tag_import_pages(self, w, tags_csv: Path, alarms_csv: Path | None, timeout, on_exists):
        """Walk the tag import pages observed in View v12: Import files (Tags [+ Alarms] checkboxes
        and paths) -> Import options (Skip existing / Update existing) -> summary (source and
        destination, verified) -> Finish -> 'Database Import' result window + <app>.DBI log."""
        seen = []
        log = RSVIEW_TEMP / f"{self.app_name}.DBI"
        before = log.stat().st_mtime if log.exists() else 0
        for _ in range(8):
            w = wait_dialog("Tag Import and Export Wizard", 15, exact=True)
            texts = [_norm(c.window_text()) for c in visible_children(w)]
            seen.append([t for t in texts if t][:12])
            buttons = {_norm(b.window_text()): b for b in visible_children(w, "Button")}
            eds = visible_children(w, "Edit")
            if "tags" in buttons and eds:  # Import files page
                files = [("tags", tags_csv), ("alarms", alarms_csv)]
                i = 0
                for kind, path in files:
                    if kind not in buttons:
                        continue
                    b = buttons[kind]
                    if (b.get_check_state() == 1) != (path is not None):
                        b.click()
                        time.sleep(0.4)
                    if path is not None:
                        eds = visible_children(w, "Edit")
                        type_verified(w, eds[i], str(path))
                    i += 1
            if "update existing (slower)" in buttons or "skip existing (fastest)" in buttons:
                buttons["update existing (slower)" if on_exists == "update" else "skip existing (fastest)"].click()
                time.sleep(0.4)
            if "finish" in buttons and buttons["finish"].is_enabled():
                dest = [e.window_text() for e in eds if e.window_text().lower().endswith(".sed")]
                if not dest or Path(dest[0]).resolve() != self.sed.resolve():
                    raise StudioError(f"tag import destination is {dest}, expected {self.sed}; not pressing Finish")
                buttons["finish"].click()
                break
            if "next >" in buttons and buttons["next >"].is_enabled():
                buttons["next >"].click()
                time.sleep(1.5)
                continue
            raise StudioError(f"tag import wizard stuck; pages seen: {seen}")
        wait(lambda: log.exists() and log.stat().st_mtime > before
             and "Database import complete" in log.read_text(errors="replace"), timeout,
             "tag import to finish (.DBI log)", interval=0.5)
        time.sleep(1)
        for r in dialogs("FactoryTalk View - Database Import", exact=True):
            with contextlib.suppress(Exception):
                r.close()
        return {"log": str(log), **parse_dbi(log)}
