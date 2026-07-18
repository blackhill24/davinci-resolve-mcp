# tests — Context (ICM Layer 2)

~170 Python tests, flat. Split by whether they need a running Resolve: `test_*` run offline;
`live_*` require Resolve (and often Studio) connected.

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Add an offline unit test | `test_<area>.py` near the feature; `_error_envelope_helpers.py` | `live_*` | — |
| Add a live-Resolve validation | `live_<domain>_validation.py` examples | `test_*` | `docs/process/release-process.md` |
| Smoke-check imports/wiring | `test_import.py` | `live_*` | — |
| Benchmark the server | `benchmark_server.py` | `live_*` | `scripts/measure_bridge_cost.py` |
| Set up a test timeline | `create_test_timeline.py` | — | — |

## Key files (only where the name doesn't say enough)

- `_error_envelope_helpers.py` — shared assertions for the action-dispatch error envelope;
  reuse when asserting tool responses.
- `test-after-restart.sh` / `.bat` — post-restart validation harness.

## Conventions & gotchas

- `live_*` tests are excluded from offline CI — they connect to a real Resolve; follow the
  live-validation guidance in `docs/process/release-process.md`.
- For Resolve-behavior changes, update focused tests rather than broad ones.

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root. Content-only
> edits usually need no doc change. Keep this file ≲40 lines.
