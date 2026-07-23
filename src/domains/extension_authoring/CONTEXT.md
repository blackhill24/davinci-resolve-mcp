# Extension Authoring — Context (ICM Layer 3)

Authoring or installing Fuse tools, DCTL/ACES LUTs, or Resolve-page scripts as
generated extensions, or checking whether a new extension needs a refresh or
restart. Prompt: `/extension_authoring_workflow`.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Fuse plugin authoring/install | `actions.py` (`fuse_plugin`), `utils/fuse_templates.py` | `granular/` | writes to the Fuses install dir Resolve actually scans (`utils/extension_authoring_live_probe.py` found it live) |
| DCTL/ACES LUT authoring/install | `actions.py` (`dctl`), `utils/dctl_templates.py` | — | |
| Resolve-page script authoring/install + lifecycle | `actions.py` (`script_plugin`), `utils/script_templates.py` | — | refresh/restart-needed detection lives here |
| Live capability probe | `utils/extension_authoring_live_probe.py` | — | regenerates kernel counts; also the source of truth for the real Fuses path |

## Key files (only where the name doesn't say enough)

- `actions.py` — 3 `@mcp.tool()`s: `fuse_plugin`, `dctl`, `script_plugin`.

## Conventions & gotchas

- Extensions are authored/installed directly through the live lifecycle layer —
  there is no offline (resolve-advanced) counterpart yet.
- A newly installed extension may need Resolve refreshed/restarted before it's
  visible — `script_plugin`'s status actions surface that, don't assume install
  == immediately usable.
- Live: `fuse_plugin`, `dctl`, `script_plugin`. Offline: none yet.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/extension-authoring-kernel.md`. Skill: `.claude/skills/extension-authoring.md`.
> Keep this file ≲40 lines.
