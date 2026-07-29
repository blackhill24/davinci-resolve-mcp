---
name: resolve-fusion-composition
description: Fusion composition work in the DaVinci Resolve MCP. Apply when building or editing Fusion comps — titles, motion graphics, VFX, merges, masks, trackers — on a timeline item live in a running Resolve OR authoring a .comp declaratively offline. Routes to the live fusion_comp tools and the offline fusion authoring tool.
---

# Resolve Fusion — Claude Code Skill

Thin router; depth stays in the kernel.

- **Live tool mechanics** — `docs/kernels/fusion-composition-kernel.md` (the
  `fusion_comp` boundary).
- **Offline authoring** — `resolve-advanced/README.md` → the `fusion` tool.

## Two servers — author offline, apply live

| Job | Server | Tools |
|---|---|---|
| Build/edit a comp on a **running** timeline item | `davinci-resolve` (Python, live) | `fusion_comp` (`probe_fusion_comp`, `safe_add_tool`, `safe_set_inputs`, `safe_connect_tools`, `fusion_boundary_report`) |
| Manage the comp *stack* on a timeline item (add/name/import/export/delete) | `davinci-resolve` (Python, live) | `timeline_item_fusion` |
| Author a `.comp` from a spec/template with **no Resolve open** | `davinci-resolve-advanced` (Node) | `fusion` (`generate`, `generate_from_template`, `list_templates`, `to_api_calls`) |

## Two live tools, different levels

`fusion_comp` works **inside** a comp — tools, inputs, connections.
`timeline_item_fusion` manages the **comps themselves** on a timeline item
(keyed by `track_type`/`track_index`/`item_index`, `item_index` 0-BASED):
`add_comp`, `get_comp_count`, `get_comp_names`, `get_comp_by_name`,
`get_comp_by_index`, `rename_comp`, `delete_comp`, `load_comp`,
`import_comp`/`export_comp` (round-trip a `.comp` file — the natural landing
point for the offline `fusion(action="generate")` output), and
`get_cache_enabled`/`set_cache`.

So: `timeline_item_fusion.add_comp` or `import_comp` to get a comp onto the
item, then `fusion_comp` to build inside it.

## Flow

1. Author/verify offline: `fusion(action="generate"|"generate_from_template")` →
   a `.comp`, or `fusion(action="to_api_calls")` → the ordered tool/input/
   connection calls.
2. Apply live: `fusion_comp` `safe_add_tool` → `safe_set_inputs` →
   `safe_connect_tools`. The `to_api_calls` output maps directly onto those.
3. Probe first (`probe_fusion_comp` / `probe_fusion_tool`) — tool availability
   and input readability vary by Resolve/Fusion build; some inputs coerce or are
   write-only. Bulk mutation needs timeline scope, not the active Fusion page.

**Granular (`--full`).** `fusion_comp`'s actions are kernel-only — no
one-per-method granular twin, same posture as `resolve-timeline-conform-interchange`
and `resolve-render-deliver`'s `render`; `src/domains/fusion_composition/actions.py`
is the sole live implementation. **Prompt** — `fusion_workflow` (`src/server.py`).
**Resources** — `status://current_timeline`, `capabilities://installed_tools`.

Never modify/transcode/derive source media (AGENTS.md).
