---
name: resolve-project-lifecycle
description: Project, database, and archive lifecycle work in the DaVinci Resolve MCP. Apply when creating/exporting/importing/archiving/restoring projects, switching databases, managing layout or render presets, or snapshotting project settings — live in a running Resolve OR offline against the project DB. Routes to the live project_manager tools and the offline project_db patcher.
---

# Resolve Project / Database / Archive — Claude Code Skill

Thin router; depth stays in the kernel.

- **Live tool mechanics** — `docs/kernels/project-lifecycle-kernel.md` (the
  `project_manager` lifecycle/settings/database/preset/archive boundary).
- **Offline DB reads** — `resolve-advanced/README.md` → `project_read`.
- **Offline DB patches** — `resolve-advanced/README.md` → `project_db`.

## Two servers — patch offline (project closed), apply live

| Job | Server | Tools |
|---|---|---|
| Create/export/import/archive/restore projects, switch databases, manage presets on a **running** Resolve | `davinci-resolve` (Python, live) | `project_manager`, `project_manager_folders`, `project_manager_database` (`project_capabilities`, `probe_project_lifecycle`, `probe_project_settings`, `safe_project_create/export/import/archive/restore/delete`, `safe_set_project_settings`, `project_settings_snapshot`, `database_capabilities`, `safe_set_current_database`, `preset_lifecycle_probe`, `project_boundary_report`) |
| Save/load/export/import UI layout presets | `davinci-resolve` (Python, live) | `layout_presets` |
| Blackmagic Cloud projects | `davinci-resolve` (Python, live) | `project_manager_cloud` |
| **Read** the project DB with **no Resolve open** | `davinci-resolve-advanced` (Node) | `project_read` |
| **Patch** the project DB with **no Resolve open** | `davinci-resolve-advanced` (Node) | `project_db` |

## Read the DB before you patch it

`project_read` is the **read-only, zero-risk** half of the offline DB pair —
no Resolve needed, nothing is written, so it never requires the
project-closed + quit/relaunch ceremony that `project_db` does. It gives
offline introspection the scripting API cannot:

- `introspect` — project version, timeline names (+ which carry an offline
  ref), media-pool folders, clip count.
- `timeline_clips` — any timeline's clips, **including cross-project or
  closed-project timelines** the live API can't reach.
- `report` (analytics) · `audit` (QC checks) · `tables` (schema + row counts) ·
  `query` (guarded SELECT) · `diff` (two DBs).

Needs the optional `better-sqlite3` dep — call the advanced `capabilities`
tool for status. Prefer it over `project_db` for any question that is only a
question; reach for `project_db` only when you actually have to write.

## The two live sub-tools

- **`layout_presets`** — `save`, `load`, `update`, `export`, `import_preset`,
  `delete` for Resolve UI layout presets. (`resolve_control` and
  `render_presets` own the *render*/burn-in preset side.)
- **`project_manager_cloud`** — `create`, `load`, `import_project`, `restore`
  for Blackmagic Cloud projects; `import_project`/`restore` report a
  readback-based `verified`. See the cloud caveat under Gotchas: these are
  shape-only validated and need real cloud infrastructure to execute.

## Safety Rules

- Safe project create/import/restore/delete require `_mcp_`-prefixed names
  unless `allow_non_mcp_name=True`.
- Safe export/import/archive/restore paths must sit under the system temp
  directory unless `require_temp_path=False`.
- Safe project delete refuses to delete the currently open project unless
  `close_current=True`.
- Safe database switching is a **dry-run** unless both `allow_switch=True` and
  `dry_run=False` are given — a real switch closes open projects.
- Safe archive defaults all media/cache/proxy flags to false and rejects any
  true flag unless `allow_media_archive=True`.

**Granular (`--full`).** `src/granular/project.py` (`archive_project`,
`import_project_from_file`, `export_project_to_file`, database/preset
get-set, cloud-project import) and `src/granular/resolve_control.py`
(layout/render/burn-in preset CRUD) — one method per API call beneath the
guarded `safe_*` compound actions. Layout presets and cloud projects do NOT
need `--full`: use the `layout_presets` / `project_manager_cloud` compound
tools above. **Prompt** — `project_lifecycle_workflow`
(`src/server.py`). **Resources** — `status://current_project`,
`capabilities://installed_tools`.

## Gotchas

- `ProjectManager.ArchiveProject` and `RestoreProject` have returned `false`
  against exported DRPs/archives in probes even with every guard flag off —
  `ImportProject` is the proven path for temp DRP round-trips.
- `Project.GetRenderSettings` isn't on the live Project object — render
  settings stay owned by the Render/Deliver domain's guarded actions.
- Cloud project create/load/import/restore methods are shape-only validated;
  they need Resolve cloud infrastructure to actually execute.

`project_db` patches need the project **CLOSED** + a full quit/relaunch, like
other DB-level advanced-server work. Never modify/transcode/derive source
media (AGENTS.md).
