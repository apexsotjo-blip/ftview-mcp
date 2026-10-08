"""HMI project discovery (read-only), safe writes, and View Studio import-log parsing."""
from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
from pathlib import Path

from . import gfx as G

SE_PROJECTS = Path(os.environ.get(
    "FTVIEW_SE_PROJECTS", r"C:\Users\Public\Documents\RSView Enterprise\SE\HMI Projects"))

_FOLDERS = {"displays": ("Gfx", "*.gfx"), "global_objects": ("Global Objects", "*.ggfx"),
            "images": ("Images", "*.*"), "parameters": ("PAR", "*.par"), "macros": ("Mcr", "*.mcr")}


def list_projects() -> list[dict]:
    if not SE_PROJECTS.is_dir():
        return []
    out = []
    for d in sorted(SE_PROJECTS.iterdir()):
        if d.is_dir() and (d / "Gfx").is_dir():
            sed = next(d.glob("*.sed"), None)
            out.append({"name": d.name, "path": str(d), "displays": len(list((d / "Gfx").glob("*.gfx"))),
                        "sed": sed.name if sed else None})
    return out


def project_dir(project: str) -> Path:
    p = Path(project)
    if not p.is_absolute():
        p = SE_PROJECTS / project
    if not (p / "Gfx").is_dir():
        raise FileNotFoundError(f"not an SE HMI project folder (no Gfx\\): {p}")
    return p


def project_contents(project: str) -> dict:
    p = project_dir(project)
    out: dict = {"path": str(p)}
    for key, (sub, pat) in _FOLDERS.items():
        d = p / sub
        out[key] = sorted(f.stem if key != "images" else f.name for f in d.glob(pat) if f.is_file()) \
            if d.is_dir() else []
    return out


def display_names(project: str) -> list[str]:
    return project_contents(project)["displays"]


# ---------------------------------------------------------------------------
# Safe writing
# ---------------------------------------------------------------------------

def _forbidden(p: Path) -> str | None:
    rp = str(p.resolve()).lower()
    if rp.startswith(str(SE_PROJECTS.resolve()).lower()):
        return ("refusing to write inside the live HMI Projects folder; write XML elsewhere and "
                "import it with View Studio")
    if p.suffix.lower() != ".xml":
        return f"output must be a .xml file, got {p.suffix!r}"
    return None


def backup(path: Path) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = path.parent / "_backups"
    bdir.mkdir(exist_ok=True)
    dst = bdir / f"{path.stem}.{stamp}{path.suffix}"
    shutil.copy2(path, dst)
    return dst


def safe_write(doc: G.GfxDoc, out_path: str | Path, overwrite: bool = False,
               baseline: G.GfxDoc | None = None) -> dict:
    """Validate then atomically write. Refuses if the edit introduces schema errors
    or duplicate names that the baseline did not already have."""
    out = Path(out_path).expanduser().resolve()
    why = _forbidden(out)
    if why:
        raise PermissionError(why)
    if not out.parent.is_dir():
        raise FileNotFoundError(f"output folder does not exist: {out.parent}")
    errs = doc.validate()
    base_errs = set(_strip_line(e) for e in baseline.validate()) if baseline is not None else set()
    new_errs = [e for e in errs if _strip_line(e) not in base_errs]
    if new_errs:
        raise G.GfxError("edit rejected, schema errors introduced:\n  " + "\n  ".join(new_errs[:20]))
    dups = set(doc.duplicate_names()) - set(baseline.duplicate_names() if baseline is not None else [])
    if dups:
        raise G.GfxError(f"edit rejected, duplicate object names introduced: {sorted(dups)}")
    result: dict = {"path": str(out)}
    if out.exists():
        if not overwrite:
            raise FileExistsError(f"{out} exists; pass overwrite=true (a backup is made first)")
        result["backup"] = str(backup(out))
    data = doc.to_bytes()
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(data)
    G.GfxDoc(tmp.read_bytes())  # re-parse what is on disk before committing
    os.replace(tmp, out)
    result["bytes"] = len(data)
    result["schema_errors_preexisting"] = len(errs)
    return result


def _strip_line(e: str) -> str:
    return re.sub(r"^line \d+: ", "", e)


# ---------------------------------------------------------------------------
# Batch import file and import-log parsing
# ---------------------------------------------------------------------------

def write_batch_import(xml_files: list[str], out_path: str | Path) -> Path:
    """Write a View Studio multi-display batch import file (UTF-16 LE, like View writes it).
    File names are stored relative to the batch file, so keep them in the same folder."""
    out = Path(out_path).resolve()
    why = _forbidden(out)
    if why:
        raise PermissionError(why)
    lines = ["<gfxImport>"]
    for f in xml_files:
        fp = Path(f).resolve()
        if not fp.is_file():
            raise FileNotFoundError(fp)
        G.GfxDoc.load(fp)  # must be a display export
        rel = os.path.relpath(fp, out.parent)
        lines.append(f'\t<import importFile="{G._xml_attr(rel)}"/>')
    lines.append("</gfxImport>")
    out.write_bytes(b"\xff\xfe" + ("\r\n".join(lines) + "\r\n").encode("utf-16-le"))
    return out


_LOG_START = re.compile(r"Importing file '(?P<file>[^']+)' into display '(?P<display>[^']+)'")
_LOG_SKIP = re.compile(r"The '(?P<name>[^']+)' element already exists in the Display and will be ignored")


def parse_import_log(path: str | Path) -> dict:
    p = Path(path)
    raw = p.read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    displays: list[dict] = []
    cur = None
    other: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _LOG_START.search(s)
        if m:
            cur = {"file": m.group("file"), "display": m.group("display"), "skipped_existing": [],
                   "errors": [], "result": None}
            displays.append(cur)
            continue
        if cur is None:
            other.append(s)
            continue
        m = _LOG_SKIP.search(s)
        if m:
            cur["skipped_existing"].append(m.group("name"))
        elif s.lower().startswith("import completed"):
            cur["result"] = s
        elif s.lower().startswith("line number"):
            continue
        elif any(w in s.lower() for w in ("error", "invalid", "fail", "not ")):
            cur["errors"].append(s)
    for d in displays:
        n = len(d["skipped_existing"])
        d["skipped_count"] = n
        d["skipped_existing"] = d["skipped_existing"][:30]
        d["applied"] = n == 0 and not d["errors"] and bool(d["result"]) and "success" in d["result"].lower()
        if n:
            d["diagnosis"] = (f"{n} objects already existed and were NOT updated. The import merged into the "
                              "existing display instead of replacing it. Re-import choosing the option that "
                              "replaces the display/existing objects (or delete the display first), then "
                              "re-export and diff against the intended XML.")
    return {"log": str(p), "displays": displays, "other": other[:20]}
