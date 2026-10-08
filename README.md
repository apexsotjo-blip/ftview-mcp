# ftview-mcp

MCP server for viewing and editing **FactoryTalk View SE** displays (v12 schema; SE1–SE13 schemas
are picked from the file's `xsi:noNamespaceSchemaLocation`).

## Setup

Requires Windows with FactoryTalk View Studio SE (v12 tested) installed, since the XSD schemas and,
for the live tools, View's own COM components are loaded from the installation. Python 3.12:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Register it with your MCP client, e.g. in `.mcp.json`:

```json
{ "mcpServers": { "ftview": { "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
                              "args": ["C:\\path\\to\\ftview-mcp\\ftview_server.py"] } } }
```

## How it works

The offline tools edit display XML exported by View Studio. They never edit the binary `.gfx` files
or anything under `HMI Projects\`. The only exception is `ftv_live_update_display`, which applies an
update headless to a backed-up `.gfx` and verifies the result (sandbox only unless confirmed; see
TESTING.md, section 4).

```
View Studio ── export XML ──> ftv_* tools (read / lint / render / edit → new validated XML)
     ^                                                         │
     └──── Graphics Import Export Wizard (batch file) <────────┘
                         └─ DisplaysImport.txt ──> ftv_check_import_log
```

Every edit is checked before it is written:
- The output is validated against Rockwell's `Gfx-SE*.xsd` (a patched in-memory copy, because the
  shipped schema has regexes that libxml2 rejects). An edit that adds schema errors is refused.
- An edit that adds duplicate object names is refused. View Studio silently skips objects whose names
  already exist, so inserted objects are renamed automatically.
- Writes are atomic. Overwriting needs `overwrite=true`, and the old file is copied to `_backups\` first.
- Unchanged content is written back byte-identical. The tests check this on every export in the corpus folder (`FTVIEW_TEST_CORPUS`),
  including that a move followed by the reverse move gives an identical file.

## Tools

| Area | Tools |
|---|---|
| Project | `ftv_list_projects`, `ftv_project_contents` |
| Read | `ftv_display_info`, `ftv_list_objects`, `ftv_get_object`, `ftv_find_tags`, `ftv_find_commands` |
| Check | `ftv_validate`, `ftv_lint` (schema, names, layout, navigation, PLC tags), `ftv_crosscheck_plc`, `ftv_diff` |
| View | `ftv_render`: PNG preview through headless Edge, with highlighting and region crop |
| Edit | `ftv_set_attributes`, `ftv_replace` (tag renames; dry-run by default), `ftv_move_objects`, `ftv_scale_objects`, `ftv_delete_objects`, `ftv_insert_objects`, `ftv_copy_objects`, `ftv_new_display` |
| Import | `ftv_make_batch_import`, `ftv_check_import_log` |
| Design | `ftv_template_catalog`, `ftv_generate_display` (JSON spec → validated, linted XML + preview), `ftv_generate_faceplate`, `ftv_add_template` |
| Upgrade | `ftv_style_audit` (ISA-101 score and findings), `ftv_recolor` (static graphics only by default; dry-run first) |
| Help | `ftv_schema` (allowed attributes, enums and children for any element), `ftv_search_plc_tags` |
| HMI tags | `ftv_tags_read`, `ftv_tags_write` (CSV in View's ###002 format; device addresses checked against the L5X), `ftv_tags_validate` |
| Parameters, macros | `ftv_parameter_file`, `ftv_macro`, `ftv_read_text_component` |
| Global objects | `ftv_global_object_define` (parameter definitions), `ftv_global_object_place` (linked instance with values) |
| View Studio | `ftv_studio_status`, `ftv_studio_import_displays`, `ftv_studio_export_displays`, `ftv_studio_replace_display` (delete, import, export and verify), `ftv_studio_tags` (export from any project read-only; import into the sandbox), `ftv_studio_screenshot` |
| Self-test | `ftv_e2e_run`, `ftv_e2e_status`; see [TESTING.md](TESTING.md) |
| Live, no UI (read-only) | `ftv_live_list_displays`, `ftv_live_export`, `ftv_live_diff`: through View's own conversion engine, for any local application that is **not** open in View Studio. See TESTING.md section 4 |

### ISA-101 templates

`pump`, `blower`, `mixer`, `motor`, `valve`, `tank`, `analog` (with a moving analog indicator bar),
`setpoint`, `status`, `button`, `alarm`, `mode`, `label`, `panel`, `pipe`, `header`, `nav`.

Style rules, which can be overridden per spec through `style`:
- Grey background, no gradients.
- Equipment state shown by fill: running or open is dark, stopped or closed is light, travelling is mid
  grey.
- Colour is used only for abnormal states. Alarm indicators combine colour, shape and character:
  P1 red square, P2 orange triangle, P3 yellow inverted triangle.
- Only abnormal modes are shown (an "H" box when not in auto). Live values are dark blue.

Templates emit the full attribute sets that View Studio itself exports. Unknown spec parameters are
rejected, so a typo raises an error instead of being silently ignored.

### Spec format and previews

See `examples/equalization_hp.json` (an equalization tank with duty/standby pumps, mixer and
analysers).
- Pipes connect to equipment ports (`"from": "TK101.bottom@0.5", "to": "P101A.in"`) and are routed
  orthogonally.
- Faceplates are opened with `Display "FP_MOTOR" /T<run>,<fault>[,<auto>],<cmd>[,<reset>]`.

`ftv_render` (and the generator) take `simulate={"values": {"P101A_RUN": 1, "LIT101": 63}}`. This
evaluates the visibility, colour, fill and position animations for that tag state, which checks the
animation logic without a runtime client.

The PLC cross-check resolves `{[Shortcut]Tag.Member[i].bit}` against a Logix L5X. It covers
controller and program scope, UDT, AOI and built-in members, array bounds, integer bit numbers and
aliases. I/O module tags come back as `unverified`.

## Applying changes to a View application

Verified in View Studio v12 (see [TESTING.md](TESTING.md), section 3):

| Wizard mode | New display | Changes to existing objects | Added objects | Removed objects |
|---|---|---|---|---|
| Create new objects | created | **skipped** ("already exists ... ignored") | added | kept |
| Update existing objects | - | applied | **rejected** ("does not exist ... cannot be updated") | kept |
| **Replace** (delete display, then create) | created | applied | added | removed |

Use replace for anything beyond small property changes. `ftv_studio_replace_display` does it
automatically: delete, import, export back, compare. By hand:
1. Delete the display in View Studio.
2. Import the batch file from `ftv_make_batch_import` with "Create new objects on the display".
3. Run `ftv_check_import_log` on `DisplaysImport.txt`. It must report `applied: true` with no messages.

Importing with Create onto existing displays is a common trap: View reports success, but every
object that already exists is skipped, so none of the edits are applied.

## Tests

- Offline: `python -m unittest discover -s tests -v`, run from the repository folder.
- Live, against View Studio in the `FTVMCP_SANDBOX` application: `python -m ftview_mcp.e2e`.
  See [TESTING.md](TESTING.md).

## Roadmap

- [x] Phase 1: display read, lint, render, edit, PLC cross-check, import verification
- [x] Phase 2: ISA-101 templates, screen and faceplate generation from a spec, simulated previews,
      style audit and recolour, schema introspection, PLC tag search
- [x] Generated displays verified by importing into View Studio (sandbox) and exporting back
- [x] Phase 3: HMI tags (CSV), global objects and instances, parameter files, macros, View Studio
      automation, live end-to-end suite
- [ ] Deploy macros and parameter files into an application through View Studio ("Add Component
      into Project"); for now they are written as files
- [ ] FactoryTalk Alarms & Events (tag-based alarm server) import/export; legacy HMI tag alarms are
      exported with the tags but are not edited
- [ ] Headless import through View's COM classes, as an alternative to driving the wizards
- [ ] Render project images (`Images\`) in previews instead of placeholders
