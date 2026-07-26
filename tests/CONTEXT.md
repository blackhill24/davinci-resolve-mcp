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
| Benchmark the bridge | `scripts/measure_bridge_cost.py` | — | — |

## Key files (only where the name doesn't say enough)

- `bridge_double.py` (root) — the ONE faithful `PyRemoteObject` double. Use it for any
  Resolve object, never `MagicMock`: `_has_method` tests `dir()`, and a mock's `dir()`
  lists only the children a test touched, so every unconfigured method reads as absent
  and the test silently exercises the capability-missing branch (#119). Pinned by
  `core/test_bridge_double_fidelity.py`; hand-rolled doubles must not re-implement its
  fabrication behaviour (`test_hand_rolled_double_audit.py`).
- `conftest.py` (root) — repo root on `sys.path` + `bridge_double` / `resolve_double`
  fixtures. Collection rules live in `pytest.ini` at the repo root, including the
  `test_*.py` / `live_*.py` split that keeps Resolve-requiring harnesses out of an
  offline run.
- `test_live_harness_exit_codes.py` (root) — every `live_*.py` must be able to FAIL:
  status propagated, a reachable nonzero exit, no computed-then-discarded result, no
  unguarded `input()`. #119 §5 found two harnesses that always exited 0.
- `test_mutation_gate.py` (root) + `scripts/mutation_gate.py` — mutation testing for the
  bridge helpers, run on every publish. Re-introduces defects that have shipped and
  fails if the suite stays green.
- `_error_envelope_helpers.py` (root) — shared assertions for the action-dispatch error
  envelope, imported across many domains; reuse when asserting tool responses.
- `preflight.py` (root) — pre-run Resolve status gate (closed / open_no_project /
  open_project); `--require open|project|timeline`, `--json`; exit 0 ready, 2 not ready,
  3 no scripting. Every `live_*` `__main__` calls `gate()` — new live harnesses must too.
- `test_live_harness_naming.py` (root) — enforces the split below: no `test_*.py` may import
  `DaVinciResolveScript` or open a live handle, and every `live_*.py` must reference
  preflight. #111: two misnamed harnesses broke `pytest tests/` at COLLECTION on every
  Resolve-less CI runner, so publish never ran the suite.
- `live_api_probe.py` / `live_resolve20_api.py` (root) — the two harnesses that rename
  fixed (was `test_live_api.py` / `test_resolve20_api.py`). The first is a read-only API
  surface probe (`--allow-mutation` opts into a scratch timeline); the second is mutating.
- `fixtures/analysis_sample/` — a real analysis root (2 verbatim `analysis.json`) checked in
  so the media-analysis round-trip/backfill guards always have real input and can never
  skip; see its README before touching. Discovery helpers live in
  `domains/media_analysis/test_analysis_store.py` (`real_sample_roots`).

## Conventions & gotchas

- `live_*` tests are excluded from offline CI by pytest's default globs (`test_*.py` /
  `*_test.py`) — the FILENAME is the only thing keeping them out, so a live harness named
  `test_*` is collected and run. Follow the live-validation guidance in
  `docs/process/release-process.md`.
- Files under `domains/`/`core/`/`dashboard/` are 2 directories deeper than the old flat
  layout — any `__file__`-relative repo-root path (`Path(__file__).resolve().parent...`,
  `parents[N]`, `sys.path.insert`) needs adjusting for that when adding new cross-references.
- Cross-test imports use the full dotted path (`from tests.domains.media_analysis.test_x
  import y`), never a bare `from tests.test_x import y` unless `test_x` stays at root.

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root. Content-only
> edits usually need no doc change. Keep this file ≲40 lines.
