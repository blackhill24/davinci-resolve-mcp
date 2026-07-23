# Fusion Composition — Context (ICM Layer 3)

Building or editing Fusion comps — titles, motion graphics, VFX, merges, masks,
trackers. Prompt: `/fusion_workflow`.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Fusion node graph (add/copy/position tools, masks, trackers) | `actions.py` (`fusion_comp`) | `granular/` | biggest tool here |
| Timeline-item-level Fusion clip ops | `actions.py` (`timeline_item_fusion`) | — | |
| Group-grade settings splice/parse | `utils/fusion_group_settings.py` | — | `FUSION_COMMIT_CHECKLIST`/`FUSION_GROUP_GUARDRAILS`, used cross-domain from `color_grade` |
| Live capability probe | `utils/fusion_composition_live_probe.py` | — | regenerates kernel counts |

## Conventions & gotchas

- Fusion page must be open (`resolve.OpenPage("fusion")`) before most node-graph
  mutations — check existing tool guards before adding a new one.
- Live: `fusion_comp`, `timeline_item_fusion`. Offline (resolve-advanced): `fusion`.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/fusion-composition-kernel.md`. Skill: `.claude/skills/resolve-fusion-composition/SKILL.md`.
> Keep this file ≲40 lines.
