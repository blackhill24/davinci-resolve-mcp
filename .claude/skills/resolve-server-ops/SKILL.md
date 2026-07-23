---
name: resolve-server-ops
description: Connector infrastructure in the DaVinci Resolve MCP — connecting to or verifying Resolve, choosing compound vs granular (--full) tools, opening/closing the local control panel, reading MCP resources, or diagnosing environment/dependency problems. Apply when the control panel won't open, a tool call fails to connect, you need to know which tools --full exposes, or something needs a doctor/capabilities check. Routes to the live setup/resolve_control/timeline_versioning tools — no offline counterpart.
---

# Resolve Server Ops — Claude Code Skill

Thin router; depth stays in the kernel. This is the connector's own
"is it alive and configured correctly" layer, not a Resolve-content domain —
so there is no offline/live split to route between.

- **Live tool mechanics** — `docs/kernels/server-ops-kernel.md` (`setup`,
  `resolve_control`, `timeline_versioning` action tables).

## One server — infrastructure, not content

| Job | Server | Tools |
|---|---|---|
| Connect/verify, control panel, conversational defaults, snapshot/rollback on a **running** Resolve | `davinci-resolve` (Python, live) | `setup`, `resolve_control`, `timeline_versioning` |

There is no offline counterpart — this layer only exists relative to a live
(or launchable) Resolve process.

## First call in any new session

`resolve_control(action="launch")` — connects to a running Resolve, or starts
it if not running. Safe to call even when already connected; call it before
any other domain tool in a fresh session.

## Compound vs. granular (`--full`)

Two modes from the same codebase: **compound** (default, `src/server.py`,
one guarded tool per domain — what every other skill routes through) vs.
**granular** (`--full`, `src/resolve_mcp_server.py` / `src/granular/*.py`,
one tool per underlying Scripting API method). Reach for `--full` only when a
compound action doesn't expose the specific call you need. Current
compound/granular tool counts live in `docs/SKILL.md` — don't hand-copy them,
they drift and are guarded by `tests/test_doc_tool_counts.py`.

## Diagnostics, when something's wrong

- **Control panel won't open** — `resolve_control(action="open_control_panel")`
  then `control_panel_status`; the panel itself lives in `src/control_panel.py`
  + `src/dashboard/`.
- **Tool call can't reach Resolve** — `resolve_control(action="env_audit")`
  (checks `RESOLVE_SCRIPT_API`/`RESOLVE_SCRIPT_LIB`/`PYTHONPATH`), or run
  `python scripts/doctor.py` directly.
- **Missing optional dependency** (ffmpeg, better-sqlite3, sharp, …) — read
  the `capabilities://install_guidance` resource, or the advanced server's
  `capabilities` tool for the offline-side equivalent.
- **"Did my last guarded action actually land?"** — `timeline_versioning`
  (`list_versions`, `diff_versions`, `rollback`) is the snapshot/rollback
  layer other domains' reversible-stage guarantees are built on
  (e.g. `resolve-orchestration`'s `rollback_stage`).

## Resources (no tool call needed)

`status://mcp_version`, `status://resolve_connection`, `status://current_project`,
`status://current_timeline`, `status://caps_preset`, `analysis://recent_reports`,
`capabilities://installed_tools`, `capabilities://install_guidance`. Cheaper
than a tool call when you just need a read.

## Prompt

`server_ops_workflow` (`src/server.py`) routes any of the above from any
MCP client, not just Claude Code.

Never modify/transcode/derive source media (AGENTS.md) — this domain doesn't
touch media at all, but the rule is universal across every skill here.
