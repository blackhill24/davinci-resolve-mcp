---
name: resolve-extension-authoring
description: Fuse tool, DCTL/ACES LUT, and Resolve-page script authoring in the DaVinci Resolve MCP. Apply when generating, validating, installing, or removing a Fuse template, DCTL/ACES DCTL, or a Resolve-page script, or when checking whether a newly installed extension needs a refresh or a Resolve restart. Routes to the live extension lifecycle tools; there is no offline counterpart.
---

# Resolve Extension Authoring — Claude Code Skill

Thin router; depth stays in the kernel.

- **Live tool mechanics** — `docs/kernels/extension-authoring-kernel.md` (the
  `script_plugin` cross-extension lifecycle layer).

## One server — author and install live

| Job | Server | Tools |
|---|---|---|
| Generate/validate/install/remove Fuse, DCTL, or Resolve-page script extensions | `davinci-resolve` (Python, live) | `script_plugin` (`extension_capabilities`, `probe_fuse_lifecycle`, `probe_dctl_lifecycle`, `probe_script_lifecycle`, `safe_install_extension`, `safe_remove_extension`, `refresh_or_restart_required`, `extension_boundary_report`), `fuse_plugin`, `dctl` |

There is no offline authoring path for this domain yet — extensions are
generated and installed directly through the live lifecycle layer.

**Granular (`--full`).** `script_plugin` is kernel-only — no one-per-method
granular twin; `src/granular/resolve_control.py` carries adjacent
layout/preset CRUD but not extension install/validate itself. **Prompt** —
`extension_authoring_workflow` (`src/server.py`). **Resource** —
`capabilities://installed_tools` (native-dep/build-tool detection for DCTL
compile checks).

## Lifecycle Map

| Surface | Install Target | Live Pickup | Restart |
|---|---|---|---|
| Fuse | Fusion Fuses directory | UI-reloadable from Inspector for existing Fuses; MCP can't trigger that reload. | Required for new Fuse registration. |
| Regular DCTL | LUT directory | `project_settings.refresh_luts` picks it up. | Not required. |
| ACES IDT/ODT DCTL | ACES Transforms IDT/ODT | Not picked up by LUT refresh. | Required. |
| Resolve-page script | Fusion/Scripts category directory | Workspace Scripts menu refreshes when opened. | Not required. |
| Inline Python/Lua | Temp file subprocess / `fusion.RunScript` bridge | Captured synchronously. | Not required. |

## Gotchas

- Safe install/remove default to requiring an `_mcp_` marker in the source —
  pass `require_marker=False` only when you intentionally manage non-MCP files.
- Installed Lua execution through `fusion.RunScript(path)` is unreliable; use
  `script_plugin.run_inline(language="lua")` when captured stdout/return values
  matter.
- Template validation is structural/parser-level — it does not prove a Fuse
  renders correctly after restart or that a DCTL compiles on every GPU backend.

Never modify/transcode/derive source media (AGENTS.md).
