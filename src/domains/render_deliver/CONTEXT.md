# Delivery / Deliverable QC — Context (ICM Layer 3)

Preparing render jobs, validating render settings, QCing a finished render vs
spec, building render manifests, expanding texted/textless/stems deliverables,
verifying media ingest, or producing a provenance/episode report. Prompt:
`/delivery_workflow`.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Render queue, settings, proxies, build_proxies | `actions.py` (`render`) | `granular/` | render queue REFUSES the system temp dir — render to a real media dir |
| Render/deliverable presets | `actions.py` (`render_presets`) | — | |
| Live capability probe | `utils/render_deliver_live_probe.py` | — | regenerates kernel counts |

## Conventions & gotchas

- Resolve's render queue silently no-ops (`AddRenderJob` returns falsy) when the
  target dir is the system temp dir — `build_proxies` defaults
  `require_temp_target=False` for this reason; keep that default when touching
  render-target validation.
- `ExportAudio=False` avoids the headless-render stall documented in `core/proc.py`.
- Live: `render`. Offline (resolve-advanced): `deliverable`.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/render-deliver-kernel.md`. Skill: `.claude/skills/resolve-render-deliver/SKILL.md`.
> Keep this file ≲40 lines.
