# Project / Database / Archive — Context (ICM Layer 3)

Creating, exporting, importing, archiving, or restoring projects, switching
databases, managing layout/render presets, or snapshotting project settings.
Prompt: `/project_lifecycle_workflow`.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Project create/export/import/archive/delete | `actions.py` (`project_manager`) | `granular/` | delete routes through `utils/project_cleanup.py`'s safe helper, never a raw `DeleteProject` |
| Project folders (browse/create/move) | `actions.py` (`project_manager_folders`) | — | |
| Cloud projects (Blackmagic Cloud) | `actions.py` (`project_manager_cloud`), `utils/cloud_operations.py` | — | `_build_cloud_settings` normalizer; account-gated, see epic #26 stage 5 |
| Database switch/connect | `actions.py` (`project_manager_database`) | — | |
| Project settings snapshot/restore, layout presets | `actions.py` (`project_settings`, `layout_presets`) | — | |
| Safe project deletion | `utils/project_cleanup.py` | — | `delete_project_safely`; retries + leftover detection |
| Project spec / lint | `utils/project_spec.py`, `utils/project_lint.py` | — | structural validation of a project's settings/timeline shape |
| Live capability probes | `utils/project_lifecycle_live_probe.py`, `utils/cloud_project_live_probe.py` | — | regenerate kernel counts |

## Key files (only where the name doesn't say enough)

- `actions.py` — 6 `@mcp.tool()`s: `layout_presets`, `project_manager`,
  `project_manager_folders`, `project_manager_cloud`, `project_manager_database`,
  `project_settings`.

## Conventions & gotchas

- Any project deletion MUST go through `utils/project_cleanup.delete_project_safely`
  — a raw `DeleteProject` call was a shipped bug class (see destructive-op tests).
- Live: `project_manager`, `project_manager_folders`, `project_manager_cloud`,
  `project_manager_database`, `project_settings`, `layout_presets`.
  Offline (resolve-advanced): `project_db`.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/project-lifecycle-kernel.md`. Skill: `.claude/skills/project-lifecycle.md`.
> Keep this file ≲40 lines.
