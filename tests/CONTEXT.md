# tests — Context (ICM Layer 2)

Python tests mirroring `src/`: `domains/<domain>/`, `core/`, `dashboard/` (restructure epic
#52, Phase 6 / #48). Cross-cutting tests (drift guards, repo-wide smoke, top-level scripts)
stay flat in `tests/`. `test_*` run offline; `live_*` need Resolve (often Studio) connected.

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Add/find a domain's tests | `domains/<domain>/test_*.py`, `domains/<domain>/live_*.py` | other domains | matching `.claude/skills/` |
| Add/find a core-infra test | `core/test_*.py` | domain folders | — |
| Add/find a dashboard test | `dashboard/test_*.py` | domain folders | — |
| Stand in for a Resolve object | `GUARDS.md` → "the one faithful double", `bridge_double.py` | `MagicMock` — never for a Resolve object | — |
| Add a repo-wide guard / drift test, or a `live_*` harness | `GUARDS.md`, then root `test_*.py` — only when it genuinely spans every domain | domain folders | — |
| Cloud-project live test setup | `cloud-test-setup.md`, `domains/project_lifecycle/live_cloud_project_validation.py` | — | issue #25 |
| Benchmark the bridge | `scripts/measure_bridge_cost.py` | — | — |

## Key files (only where the name doesn't say enough)

- `GUARDS.md` — doubles, meta-tests, isolation, live-harness and coverage rules (Layer 3).
  Every root-level guard and `live_*` convention is documented there, not here.
- `conftest.py` (root) — repo root on `sys.path`, `bridge_double` / `resolve_double`
  fixtures, autouse env-leak guard. Collection rules are in `pytest.ini` at the repo root.
- `_error_envelope_helpers.py` (root) — shared assertions for the action-dispatch error
  envelope; `assert_error_mentions` pins one to the SPECIFIC cause. Reuse when asserting
  tool responses.
- `fixtures/analysis_sample/` — a real analysis root (2 verbatim `analysis.json`) so the
  media-analysis guards can never skip; read its README first. Discovery helpers:
  `domains/media_analysis/test_analysis_store.py`.

## Conventions & gotchas

- Files under `domains/`/`core/`/`dashboard/` are 2 directories deeper than the old flat
  layout — any `__file__`-relative repo-root path (`Path(__file__).resolve().parent...`,
  `parents[N]`, `sys.path.insert`) needs adjusting when adding new cross-references.
- Cross-test imports use the full dotted path (`from tests.domains.media_analysis.test_x
  import y`), never a bare `from tests.test_x import y` unless `test_x` stays at root.

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session (guard/harness depth goes in `GUARDS.md`), then run
> `python3 .icm/drift-check.py --update` from the root. Keep this file ≲40 lines.
