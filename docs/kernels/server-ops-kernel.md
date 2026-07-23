# Server Ops Kernel

The connector's own "is it alive and configured correctly" surface — live-server
infrastructure that every domain sits on top of, not a Resolve-content domain
itself. No offline (`resolve-advanced`) counterpart exists for this layer.

## Compound tools

### `setup` — conversational defaults

| Action | Purpose |
| --- | --- |
| `schema` | Report every configurable default, its allowed values, and storage path. |
| `get_defaults` | Read current defaults (falls back to documented defaults for unset keys). |
| `set_defaults` | Write one or more defaults (media-analysis behavior, MCP update policy). |
| `clear_defaults` | Clear specific keys or all defaults back to built-in behavior. |

Defaults persist to a JSON preferences file (path reported by `schema`) and
govern `media_analysis`'s default behavior (vision/transcription/marker
writeback/persistence policy) plus the MCP self-update policy — see
`resolve_control`'s `mcp_update_status` family below.

### `resolve_control` — connect, verify, panel, updates

| Action | Purpose |
| --- | --- |
| `launch` | Connect to a running Resolve, or start it if not running. Call first in a new session; safe to call when already connected. |
| `get_version` / `get_page` / `open_page` | Read product/version info; read/switch the current Resolve page. |
| `get_keyframe_mode` / `set_keyframe_mode` | Read/write the timeline keyframe mode. |
| `quit` | Quit Resolve — a real process kill, treat as a confirmed, explicit action. |
| `open_control_panel` / `control_panel_status` / `close_control_panel` | Lifecycle for the local browser control panel (`src/control_panel.py`, `src/dashboard/`). |
| `save_state` / `restore_state` | Persist/restore control-panel UI state across restarts. |
| `api_truth` | Cross-check claimed vs. live-verified API coverage. |
| `verification_stats` | Summarize live-test pass/fail/skip counts. |
| `env_audit` | Report `RESOLVE_SCRIPT_API`/`RESOLVE_SCRIPT_LIB`/`PYTHONPATH` and other environment prerequisites. |
| `job_status` / `list_jobs` | Cross-domain job status (shared with `orchestrate`/`auto_edit` job records). |
| `mcp_update_status` / `set_mcp_update_policy` / `ignore_mcp_update` / `snooze_mcp_update` / `clear_mcp_update_preferences` | MCP self-update check and policy (defaults live under `setup`). |
| `get_fairlight_presets` | List Fairlight mixer presets (read-only; see `resolve-audio-fairlight` for bus routing). |
| `set_high_priority` / `disable_background_tasks_for_current_session` | Process-priority and background-task tuning for the current Resolve session. |

### `timeline_versioning` — snapshot / rollback safety net

| Action | Purpose |
| --- | --- |
| `begin_run` / `end_run` | Bracket a guarded operation with an auto-snapshot run. |
| `list_runs` | List recorded runs for the current project. |
| `archive_current` | Snapshot the current timeline state on demand. |
| `list_versions` / `get_history` | List/inspect timeline version snapshots. |
| `diff_versions` / `diff_timelines` | Structural diff between two snapshots, or two timelines. |
| `rollback` | Restore a prior snapshot — the mechanism other domains' "reversible stage" guarantees (e.g. `resolve-orchestration`'s `rollback_stage`) are built on. |
| `prune` | Drop old snapshots past a retention policy. |
| `registry` | Read the snapshot registry (namespacing, counts, storage location). |
| `media_pool_changes` | Diff media pool state between two points. |

`timeline_versioning` is not itself a content-editing tool — it's the
underlying safety net other domains call into for reversible stages and
"undo my last guarded action" requests.

## Compound vs. granular (`--full`) surface toggle

The server ships two modes from the same codebase:

- **Compound (default)** — `src/server.py`, one guarded tool per domain
  (`timeline`, `media_pool`, `render`, …). This is what every skill above
  routes through.
- **Granular (`--full`)** — `src/resolve_mcp_server.py` / `src/granular/*.py`,
  one tool per underlying Scripting API method. Reach for it only when a
  compound action doesn't expose the specific call you need (each domain
  skill notes its own granular pointers, or their absence).

See `docs/SKILL.md`'s tool-count table for the current compound/granular
totals — do not hand-copy the numbers here; they drift and are guarded by
`tests/test_doc_tool_counts.py`.

## Diagnostics

- **`scripts/doctor.py`** — environment/dependency doctor: checks
  `RESOLVE_SCRIPT_API`/`RESOLVE_SCRIPT_LIB`/`PYTHONPATH`, Python version, and
  common misconfigurations before you even try to launch.
- **Advanced server's `capabilities` tool** — reports which optional native
  deps (`better-sqlite3`, `sharp`, ffmpeg/ffprobe) are installed on the
  offline side, with install hints.
- **`capabilities://install_guidance`** resource — the live-server equivalent:
  what's missing and how to install it, without a tool call.
- **`src/batch_cli.py`** — headless batch runner for scripted/CI use, no MCP
  client needed.

## Resources (no tool call needed)

| Resource | Reports |
| --- | --- |
| `status://mcp_version` | Server package version. |
| `status://resolve_connection` | Whether Resolve is reachable right now. |
| `status://current_project` | Current project name/state. |
| `status://current_timeline` | Current timeline name/state. |
| `status://caps_preset` | Active capability/tool-surface preset. |
| `analysis://recent_reports` | Recently written media-analysis reports. |
| `capabilities://installed_tools` | Detected optional native deps on this machine. |
| `capabilities://install_guidance` | How to install whatever's missing. |

## Live Probe

Not yet run against a live Resolve instance in this repo's history — this
kernel doc was authored from static code review (#69). When run, the smoke
should cover: `resolve_control(action="launch")`'s connection check, reading
all 8 resources above, opening then closing the control panel, and
`python scripts/doctor.py`. Record the transcript on the tracking issue.
