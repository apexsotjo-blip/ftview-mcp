# Testing ftview-mcp (runbook for people and AI agents)

There are two layers. Run both before trusting a change.

## 1. Offline unit tests (about 5 s, no View Studio needed)

```
cd ftview-mcp
python -m unittest discover -s tests -v
```

They cover:
- Byte-identical round trips of View's tag CSVs and, optionally, of your own display exports: set
  `FTVIEW_TEST_CORPUS` to a folder of display XML exported by View Studio (default `..\xml`).
- Schema validation, templates, the generator, the expression evaluator and simulation.
- Lint rules, including the importer rules listed in section 3.
- Tag CSV, parameter files, macros, global-object instances, the round-trip comparison rules and log
  parsing.

## 2. Live end-to-end tests against FactoryTalk View Studio

These import into View Studio itself, through View's own wizards, then export back and compare. This
is the only proof that View accepts what we write.

### Requirements
- An interactive Windows desktop session. The harness uses UI Automation and takes keyboard and mouse
  focus while it runs, so don't type or click during a run.
- An SE local application named `FTVMCP_SANDBOX` (or set `FTVIEW_SANDBOX_APP`), created only for
  these tests. Never point it at a production application.
- View Studio either closed, or open on `FTVMCP_SANDBOX`. If it shows any other application, the
  harness stops without touching it.

### Run

```
cd ftview-mcp
python -m ftview_mcp.e2e                 # all scenarios, about 2.5 min (batched)
python -m ftview_mcp.e2e generate tags   # selected scenarios
```

From an MCP client, use `ftv_e2e_run` (starts in the background), then poll `ftv_e2e_status`.
Clean up test displays or global objects with `ftv_studio_delete` (sandbox only). HMI tags cannot be
deleted through the wizard; leftover sandbox tags are harmless.

### Error semantics for MCP clients
- A refused or invalid request is a tool error (`is_error`) with a `[REFUSED]`, `[INVALID]`, `[EXISTS]`,
  `[GFX]` or `[NOT FOUND]` prefix. This covers writes blocked by validation (tags, `.par`, `.mcr`) and
  `ftv_generate_display` with lint errors while `strict=true` (the default).
- Successful calls can still report problems as data, so always check them:
  - `ftv_lint`: `summary.error`
  - import tools: every display's `applied`
  - `ftv_studio_tags`: `errors` and `failures`
  - `ftv_studio_replace_display`: `verified`
`ftv_studio_status` shows whether Studio is up and which application is open.

The exit code is 0 only if every check passed. Each run writes a folder
`..\ftview-e2e\runs\<timestamp>\` containing:

| File | Content |
|---|---|
| `report.md`, `report.json` | Every check with pass/fail and details |
| `run.log` | Timeline |
| `in*/`, `out*/` | XML sent to View and XML exported back |
| `compare_*.json` | Full comparison per display, including normalised differences |
| `DisplaysImport*.txt`, `TagImport.DBI` | View's own import logs |
| `*_error.png` | Screenshot taken when a scenario fails |

### Scenarios

| Scenario | What it proves |
|---|---|
| `generate` | Every ISA-101 template, the example screen and a faceplate pass lint, import with an empty View log, and export back unchanged |
| `edit` | Edits through the API: update mode applies changes to existing objects; replace (delete display, then create) applies inserts and deletes exactly |
| `corpus` | Optional: a real display export (`FTVIEW_E2E_CORPUS=path\to\Display.xml`) survives import and export. Skipped when the variable is not set |
| `tags` | A tag CSV written by `tags.py` (memory analog, digital and string, plus a device tag) imports with no errors and exports back identical |
| `global` | A global object with parameter definitions and a display with two parameterised instances import, keep their links and values, and export back identical |
| `headless` | No UI. Closes Studio (sandbox only), edits a display with `headless.update_display`, verifies the result and the preserved VBA sections, and checks that an XML adding objects is refused and leaves the display unchanged. It can run while someone is using the desktop: `python -m ftview_mcp.e2e headless`, about 2 min |

### Reading a failure
1. Open `report.md` and look at the failed check's details: `differences` lists `path`, `attribute`,
   `sent` and `exported`.
2. If View rejected something, `DisplaysImport*.txt` or `TagImport.DBI` has the exact message.
3. If the harness itself got stuck, look at `*_error.png` to see the dialog that was showing.
4. Decide whether the difference is:
   - **our bug:** fix the builder or template, then add a lint rule;
   - **a harmless View normalisation:** add a rule to `verify._classify`, citing the run that showed it;
   - **a real View limitation:** add a lint warning and document it below.

## 3. View v12 importer behaviour learned from live runs

| Behaviour | How the tool handles it |
|---|---|
| `wallpaper` on an object inside a group is rejected | Never emitted inside groups; lint error |
| `titleBarText` is rejected when `titleBar="false"` | Omitted; lint error |
| An expression that is only a parenthesised tag, e.g. `({x})`, is corrupted to `{({{x}})}` | Never emitted; lint error |
| `button` confirmation (`confirmAction`) is always reset to off | Lint warning; set it in View Studio after import |
| `<line>` `lineWidth` is reset to 1, and a 2-point polyline becomes a `<line>` | Thick straight lines are emitted as filled rectangles; lint warning |
| Numeric and string displays ignore `fontSize` and size themselves from `charHeight`, `charWidth` and the field length | Builders emit `charHeight`/`charWidth` and fit the box |
| Fractional font sizes are rounded | Builders round |
| Update mode cannot create or delete objects ("element does not exist ... cannot be updated"), and cannot create a display that does not exist ("The display ... does not exist") | Use replace: `ftv_studio_replace_display` |
| Create mode on an existing display skips objects whose names exist ("already exists ... ignored") | `ftv_check_import_log` flags it; use replace |
| A device analog tag with an empty initial value fails ("Initial value is invalid") | `tags.py` always writes it |
| Arial Unicode MS becomes Arial on button captions and texts, `sizeToFit` texts are re-measured, tab order is renumbered, down-state captions are cleared | Treated as normalisations by `verify` |
| Graphics wizard logs (`DisplaysImport.txt`, `DisplaysExport.txt`) are written progressively, and the last line marks completion | The harness waits for that line |

## 4. Headless (no UI) access: what works and what does not

Investigated 7 Oct 2026 by reading View's COM type libraries and trying them in the sandbox.

| Operation | Headless? | How |
|---|---|---|
| List displays of an application | Yes, about 5 s | View's own Gfx-to-XML engine (`RocketConversionManager.RktMgr`) through 32-bit PowerShell: `ftv_live_list_displays` |
| Export displays to XML | Yes, about 10 s for 6 displays | Same engine, `ConvertDisplays`: `ftv_live_export`. Output matches the wizard's, except that the default `imageReference="noImage"` is omitted |
| Compare a live display with an XML file | Yes | `ftv_live_diff` |
| Update existing objects of a display (move, resize, colours, captions, tooltips, tag bindings, animations) | **Yes**, about 1 min, no UI | `ftv_live_update_display` / `headless.update_display`. See "How headless update works" below |
| Add new objects, add new displays | No | View's headless engine opens and saves displays but creates no objects. `ImportXML` create mode, `IADDisplay.XMLAddData` and an empty-template display were all tried; each reports success and adds nothing. Use the wizard (`ftv_studio_replace_display`) |
| Delete objects or displays | No | `IADDisplay.XMLDeleteData` rejects every format tried. Use the wizard |

### How headless update works (found 8 Oct 2026)

1. View's conversion engine (`RocketConversionManager.RktMgr`) opens the display and passes it to
   ViewPoint's exporter, CLSID `{30703590-...}`.
2. In our own 32-bit process only, `headless_update.ps1` registers a class factory for that CLSID
   (`CoRegisterClassObject`; nothing is written to the registry). The engine therefore hands the
   opened display and the graphics server to us.
3. We run View's own importer (`IGfxImportWithVBA`, update mode) on the display. The
   display's `IComponent.Save` then writes the project's `.gfx`, including the language string table
   `ls00000409`.
4. That save omits the VBA project (`DSI_VBA`) and `GOPerTables`. `headless.py` copies them back
   from the backup, then checks that every section is present and that the VBA streams are
   byte-identical. Verified 8 Oct 2026 through View Studio's own export: the VBA was unchanged by a
   headless edit of a display with VBA.

Safety rules:
- Sandbox only, unless `app=` and `confirm=true` are given.
- Refused while the application is open in View Studio.
- Refused when the XML adds objects.
- The `.gfx` is backed up to `..\ftview-backups\<app>\`.
- The display is exported again afterwards and compared with the XML. On any difference, the backup
  is restored automatically.

One run on 8 Oct 2026 demonstrated the last rule: a mismatch caused an automatic restore.

**Never export headless while the application is open in View Studio.** The engine finds the running
Studio through the Windows Running Object Table, opens the displays inside Studio and leaves them open.
On 7 Oct 2026 this made the next wizard imports fail with "Unable to perform an import when the display
is open". `headless.py` now checks the Running Object Table (no UI) and refuses in that case. The live
tests and `ftv_studio_replace_display` therefore keep using the wizard export.

`ftv_live_diff` is a quick way to audit a production application: it shows which edits in your XML
files never reached the live displays (for example after a Create-mode import that skipped them).

## 5. Independent agent test (7 Oct 2026)

A separate AI agent ran this runbook with no prior knowledge of the code: offline tests, the full live
suite three times, and its own task (a DAF screen from real plant PLC tags, applied to the sandbox with
replace and verification, plus a tag round trip). Every refusal case behaved correctly.

It found 7 issues. All are fixed, and each has a regression test in `tests/test_phase3.py`
(`AgentFindings`) or the live `tags` scenario:
1. Device digital tags had no Initial Digital value, so View rejected them. Now written as the Off
   label, and validated.
2. Misspelled keys inside `tags`, `bar`, `faceplate` and status `states` were ignored. Now rejected;
   `ftv_template_catalog` lists `tag_roles` for each template.
3. Any import `mode` other than `create` silently ran an update. Now only `create` and `update` are
   accepted.
4. `ftv_studio_replace_display` named the display after the file. It now has a `display_name`
   parameter and reports `replaced_existing`.
5. Pipes doubled back through pumps and valves. The router now leaves and enters each port with a
   stub and never reverses it.
6. The tag wizard occasionally hit a stale window handle. It now retries once; tag imports are
   idempotent.
7. Generation with bad PLC tags still wrote the file. It is now refused unless `strict=false`.

## 6. Safety rules built into the harness
- It automates only `FTVMCP_SANDBOX`, unless `allow_app` and `confirm=true` are given.
- It never switches, closes or saves another application.
- It reads back every typed path before pressing a wizard's Finish button. Example: the export folder
  defaulted to `..\xml`, which would have overwritten your files; the read-back check stopped it.
- It restores View's remembered wizard folders (`HKCU\...\DisplaysImportExport`) after each run.
- Tag export from any project (`ftv_studio_tags action=export sed=...`) is read-only. Tag import is
  allowed only into the sandbox unless explicitly confirmed.
