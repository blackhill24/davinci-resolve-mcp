# Color / Grade — Context (ICM Layer 3)

Grading, correcting, shot matching, developing looks, or applying/modifying LUTs,
CDLs, DRX grades, or copied grades. Prompt: `/color_grade_workflow`.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Per-clip color ops (CDL, LUT, node graph, still) | `actions.py` (`timeline_item_color`) | `granular/` | biggest tool here |
| Gallery / power-grade stills | `actions.py` (`gallery`, `gallery_stills`) | — | |
| Node-graph read/inspect | `actions.py` (`graph`) | — | read-only structural probe |
| Color groups (pre/post clips) | `actions.py` (`color_group`) | — | |
| CDL parsing/validation | `utils/cdl.py` | — | shared by `timeline_item_color`'s `set_cdl` |
| Safe LUT/export temp paths | `utils/lut_paths.py` | — | never invent ad hoc temp paths for Resolve-written files |
| Live capability probe | `utils/color_grade_live_probe.py` | — | run against a live Resolve to regenerate kernel counts |
| Offline `.drx` grade authoring | `resolve-advanced/` `drx` surface | this domain | no Resolve running |

## Key files (only where the name doesn't say enough)

- `actions.py` — 5 `@mcp.tool()`s: `timeline_item_color`, `gallery`, `gallery_stills`,
  `graph`, `color_group`.

## Conventions & gotchas

- Frame-referenced color work (grading based on a specific frame) needs the safety
  rules in root `AGENTS.md` — read those before any node-graph mutation.
- Live: `timeline_item_color`. Offline (resolve-advanced): `drx`.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/color-grade-kernel.md` + `docs/guides/color-decision-guide.md`.
> Skill: `.claude/skills/resolve-color-grade/SKILL.md`. Keep this file ≲40 lines.
