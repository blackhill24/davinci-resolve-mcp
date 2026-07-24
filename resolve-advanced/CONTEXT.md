# advanced-server — Context (ICM Layer 2)

Node "beyond-the-API" MCP that authors Resolve **files** (`.drp`/`.drt`/`.drx`) and applies
DB/XML edits with **no Resolve running**. Also usable as a library (`server/lib.mjs`).

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Add/change a file-authoring tool | `server/<tool>.mjs` or `server/tools/<domain>.mjs`, `server/index.mjs` (wiring) | `vendor/`, `test/` | matching `docs/kernels/*-kernel.md` |
| Change a codec (.drp/.drt/.drx, Fusion, audio) | `vendor/<domain>/` | `server/` tools | `docs/authoring/` |
| Expose something as library API | `server/lib.mjs`, `package.json` exports/main | `vendor/` | — |
| Understand tool catalog / capabilities | `server/tool-catalog.mjs`, `server/capabilities.mjs` | `vendor/` | — |
| Run/execute a contract | `server/runner.mjs`, `server/runner-apply-contract.mjs` | `vendor/` | — |
| Bridge from the Python live server | `scripts/drp-bridge.mjs` (write ops on scratch .drt/.drp), `scripts/panel-bridge.mjs` (read-only) | `vendor/` | Python side: `src/core/advanced_bridge.py` |

## Key files (only where the name doesn't say enough)

- `server/index.mjs` — MCP entry that wires every `server/*.mjs` tool.
- `server/lib.mjs` / `server/libs.mjs` — public library surface (see README "As a library").
- `vendor/` — bundled codecs by domain (`drx-codec`, `drp-format`, `fusion-codec`, …);
  large, low-churn — read only the one domain a task touches.
- `scripts/preflight-native.mjs` — `pretest` gate; turns better-sqlite3/sharp ABI drift into
  one `npm rebuild` message instead of dozens of ERR_DLOPEN_FAILED test failures (#104).

## Conventions & gotchas

- Two `bin` entrypoints ship from one npm package (`bin/*.mjs` at repo root); this server is
  `davinci-resolve-advanced-mcp.mjs`.
- File tools never touch a running Resolve — that separation is the point; keep it.
- Bridge scripts: stdout is a ONE-JSON contract. Never `console.log` to stdout from a codec
  (corrupts it + MCP stdio) — `drp-bridge` routes console.log→stderr; Python side decodes
  UTF-8 (codec progress logs a `→`). `drp-bridge` dispatches `drp`/`drt`/`drx`.
- `npm test` is per-glob already (bare-dir args to `node --test` are rejected on Node 24+), and
  its `pretest` runs the native preflight. Native optional deps (better-sqlite3, sharp) are
  ABI-pinned: after a Node major upgrade, `npm rebuild <dep>` — the preflight says which.

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root. Content-only
> edits usually need no doc change. Keep this file ≲40 lines.
