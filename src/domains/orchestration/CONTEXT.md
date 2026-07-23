# Orchestrate (ingest → deliver) — Context (ICM Layer 3)

A task spans multiple domains (edit AND grade AND deliver) and needs to survive
a context reset across sessions — a full ingest-to-delivery post job. Prompt:
`/orchestrate_workflow`.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Resumable multi-stage conductor (run/resume/status/gates) | `actions.py` (`orchestrate`), `utils/orchestrate.py` | `granular/` | single tool; dispatches into every other domain's compound tools by stage |
| Pause/resume around an offline-compute stage | `utils/orchestrate.py` (`_orchestrate_*` helpers) | — | offline-op pause/resume orchestrated around Resolve-closed advanced ops (#39) |

## Key files (only where the name doesn't say enough)

- `actions.py` — 1 `@mcp.tool()`: `orchestrate`. Nearly all logic lives in
  `utils/orchestrate.py`; `actions.py` is a thin dispatch shell.
- `utils/orchestrate.py` — the resumable stage-state machine (persists progress
  so a session reset picks up mid-job); imports across almost every other
  domain's compound tools to run each stage.

## Conventions & gotchas

- This is the one domain expected to import broadly across siblings — that's
  its job (conducting them), not a smell.
- Offline compute-then-apply (running stages with Resolve closed) is a
  deferred follow-on epic, not yet built beyond the pause/resume wiring.
- Live: `orchestrate`. Offline: none yet.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/orchestration-kernel.md`. Skill: `.claude/skills/orchestration.md`.
> Keep this file ≲40 lines.
