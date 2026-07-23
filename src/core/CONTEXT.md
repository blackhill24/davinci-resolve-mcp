# core — Context (ICM Layer 3)

Cross-domain shared infrastructure: no domain-specific Resolve semantics, or
used by 2+ domains (restructure epic #52, Phase 1 / #44). Extracted from
`src/utils/` and `server.py` before per-domain files move — see
`docs/decisions/0001-domain-taxonomy.md` for the sibling naming ADR and
`#44` for the move rationale/criterion.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Governance / AI-ops gating | `resolve_ai_governance.py`, `resolve_ai_ledger.py` | domain `utils/` | ledger backs every destructive-op audit trail |
| Action-dispatch envelope | `contracts.py` | — | shared `validate()`; reuse before adding per-tool validation |
| Process/platform spawn helpers | `proc.py`, `platform.py`, `app_control.py` | — | `resolve_spawn_env`/`sanitized_spawn_env` — raw-hw ALSA fix lives here |
| Background job runner | `background_jobs.py` | — | used by batch/analysis job tools across domains |
| Brain DB / edit history | `timeline_brain_db.py`, `brain_edits.py`, `timeline_versioning.py` | — | shared SQLite-backed edit ledger, not domain-owned |
| API-limitation tracking | `api_truth.py` | — | regenerates `docs/reference/api-limitations.md` via `scripts/gen_api_limitations.py` |
| MCP transport/stdio wiring | `mcp_stdio.py`, `mcp_transport.py` | — | server bootstrap, not tool logic |
| Destructive-op registry | `destructive_hook.py` | — | drift-checked against `tests/test_destructive_registry_drift.py` |
| Resolve connection bootstrap | `resolve_connection.py` | `granular/` | connector core; both live servers depend on it |
| Busy/lock gating | `resolve_busy.py`, `page_lock.py` | — | long-Resolve-op busy gate + UI page lock |
| Misc infra | `actor_identity.py`, `analysis_runs.py`, `bridge_metrics.py`, `failure_tracker.py`, `object_inspection.py`, `readback.py`, `structural_diff.py`, `update_check.py` | — | one concern each; grep before adding a new core file |

## Conventions & gotchas

- A file belongs here only if it has no domain-specific Resolve semantics, or
  is used by 2+ domains (the Phase 1 criterion) — otherwise it stays in a
  domain's `utils/` (Phase 2, #47).
- Internal cross-imports use `from src.core import X`; a domain file importing
  one of these uses the same absolute form, never a relative import across
  the `core`/domain boundary.

> Upkeep: when files here change (add/remove/rename), fix the table above +
> `src/CONTEXT.md` in the same session, then run `python3 .icm/drift-check.py
> --update` from the root. Keep this file ≲40 lines.
