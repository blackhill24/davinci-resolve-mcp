---
name: resolve-project-lifecycle
description: Project, database, and archive lifecycle work in the DaVinci Resolve MCP. Apply when creating/exporting/importing/archiving/restoring projects, switching databases, managing layout or render presets, or snapshotting project settings — live in a running Resolve OR offline against the project DB. Routes to the live project_manager tools and the offline project_db patcher.
---

# Resolve Project / Database / Archive — Claude Code Skill

Thin router; depth stays in the kernel.

- **Live tool mechanics** — `docs/kernels/project-lifecycle-kernel.md` (the
  `project_manager` lifecycle/settings/database/preset/archive boundary).
- **Offline DB patches** — `resolve-advanced/README.md` → `project_db`.

## Two servers — patch offline (project closed), apply live

| Job | Server | Tools |
|---|---|---|
| Create/export/import/archive/restore projects, switch databases, manage presets on a **running** Resolve | `davinci-resolve` (Python, live) | `project_manager`, `project_manager_folders`, `project_manager_database` (`project_capabilities`, `probe_project_lifecycle`, `probe_project_settings`, `safe_project_create/export/import/archive/restore/delete`, `safe_set_project_settings`, `project_settings_snapshot`, `database_capabilities`, `safe_set_current_database`, `preset_lifecycle_probe`, `project_boundary_report`) |
| Patch the project DB with **no Resolve open** | `davinci-resolve-advanced` (Node) | `project_db` |

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
