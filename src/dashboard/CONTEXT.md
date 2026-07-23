# dashboard — Context (ICM Layer 2)

Local single-user analysis control panel (browser UI + its Python backend).
Runs standalone via `python -m src.dashboard.main`, or spawned by `server.py`'s
`open_control_panel` tool as a background process. Split out of the 15.7k-line
`src/analysis_dashboard.py` monolith (restructure epic #52, Phase 4 / #45).

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Panel UI (HTML/CSS/JS) | `static/panel.html` | `*.py` | one file, browser-side only; `PANEL_LABELS`/`SUBPAGE_LABELS` drift-checked against `docs/guides/control-panel.md` |
| Resolve read-only connection | `resolve_helpers.py` | — | `_connect_resolve_read_only`, AI-console op dispatch (`_run_resolve_ai_op`) |
| Media-pool inventory scan | `media_inventory.py` | — | folder walk, exclude-bins, cached path-existence probing |
| Project-folder discovery | `project_context.py` | — | Resolve project-manager folder tree, dashboard active-context bookkeeping |
| V2 Review API (per-clip) | `clip_review.py` | — | analyzed-clip list/detail, transcript read/correct, semantic search |
| Timeline-version / edit-plan endpoints | `timeline_versions.py` | — | C6 version chain, edit-plan payloads |
| DashboardState, install/update/transport | `state.py` | — | `DashboardState` class, MCP install/uninstall, self-update, advanced-bridge lineage |
| HTTP routing | `handler.py` | — | `Handler(BaseHTTPRequestHandler)`; wires every module above to an HTTP path |
| Entry point / CLI args | `main.py` | — | `parse_args`, `main()`, inventory cache warm-up |

## Conventions & gotchas

- Cross-file imports inside this package are module-level (`from src.dashboard.X
  import name`) except where that would cycle — `clip_review.py` ↔
  `timeline_versions.py` has one lazy, function-local import to break a cycle.
- `state._repo_root()` is `os.path.dirname(__file__)/../..` (this package is one
  level deeper than the old top-level `analysis_dashboard.py` was — a path bug
  here silently breaks self-update/install/doc-reader features).
- Not an MCP domain (no `@mcp.tool()` here) — it's a plain HTTP server; the
  granular/compound domain split in `src/domains/` doesn't apply.

> Upkeep: when files here change (add/remove/rename), fix the table above +
> root `CLAUDE.md`'s workspace table in the same session, then run `python3
> .icm/drift-check.py --update` from the root. Keep this file ≲40 lines.
