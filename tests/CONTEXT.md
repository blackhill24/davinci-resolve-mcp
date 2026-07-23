# tests — Context (ICM Layer 2)

~220 Python tests, mirroring `src/`: `tests/domains/<domain>/`, `tests/core/`,
`tests/dashboard/` (restructure epic #52, Phase 6 / #48). Cross-cutting tests that don't
belong to one domain (drift guards, repo-wide smoke tests, top-level-script tests) stay
flat in `tests/`. Split by whether they need a running Resolve: `test_*` run offline;
`live_*` require Resolve (and often Studio) connected — same convention, now within each
folder.

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Add/find a domain's tests | `domains/<domain>/test_*.py`, `domains/<domain>/live_*.py` | other domains | matching `.claude/skills/` |
| Add/find a core-infra test | `core/test_*.py` | domain folders | — |
| Add/find a dashboard test | `dashboard/test_*.py` | domain folders | — |
| Add a repo-wide/drift-guard test | root `test_*.py` (e.g. `test_import.py`, `test_action_list_drift.py`) — only when it genuinely spans every domain | domain folders | — |
| Cloud-project live test setup | `cloud-test-setup.md`, `domains/project_lifecycle/live_cloud_project_validation.py` | — | issue #25 |
| Benchmark / set up a test timeline | `benchmark_server.py`, `create_test_timeline.py` (both root — shared across domains) | — | `scripts/measure_bridge_cost.py` |

## Key files (only where the name doesn't say enough)

- `_error_envelope_helpers.py` (root) — shared assertions for the action-dispatch error
  envelope, imported across many domains; reuse when asserting tool responses.
- `preflight.py` (root) — pre-run Resolve status gate (closed / open_no_project /
  open_project); `--require open|project|timeline`, `--json`; exit 0 ready, 2 not ready,
  3 no scripting. Every `live_*` `__main__` calls `gate()` — new live harnesses must too.
- `test-after-restart.sh` / `.bat` (root) — post-restart validation harness.

## Conventions & gotchas

- `live_*` tests are excluded from offline CI — they connect to a real Resolve; follow the
  live-validation guidance in `docs/process/release-process.md`.
- Files under `domains/`/`core/`/`dashboard/` are 2 directories deeper than the old flat
  layout — any `__file__`-relative repo-root path (`Path(__file__).resolve().parent...`,
  `parents[N]`, `sys.path.insert`) needs adjusting for that when adding new cross-references.
- Cross-test imports use the full dotted path (`from tests.domains.media_analysis.test_x
  import y`), never a bare `from tests.test_x import y` unless `test_x` stays at root.

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root. Content-only
> edits usually need no doc change. Keep this file ≲40 lines.
