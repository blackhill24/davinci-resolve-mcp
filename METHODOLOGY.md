# How this project is organized (ICM — "folders over agents")

Context is layered in plain markdown so any agent reads a small router first, then only
what a task needs. **Cost of a task ≈ router + one CONTEXT.md + the routed files** — repo
size drops out.

## Layers

1. **Router** — `CLAUDE.md` at root, auto-loaded. Answers *"where does this task live?"*
2. **Context** — `<workspace>/CONTEXT.md`, read on entry. Answers *"what do I read, skip,
   and use here?"* via its routing table.
3. **Workspace** — the actual files, read only as routed.

Rules of record (source-media safety, media-analysis defaults, domain routing, commands)
live in `AGENTS.md`, which stays canonical and is read by every agent — the router points
to it rather than duplicating it.

## Working here

Read `CLAUDE.md` → open the one relevant `CONTEXT.md` → follow its routing table. Don't
read the whole repo — that's the point.

## Writing docs here (the rules that keep this cheap)

- **Only encode surprise.** Never write what a filename or the tree already shows.
- **Prefer renaming a file to documenting it** — a good name never goes stale.
- **Bind routing to paths and purposes, never content summaries** — structure changes
  slowly, contents constantly. Stable docs are the cheapest freshness strategy.
- Router ≲60 lines, CONTEXT.md ≲40. Overflow → push detail down a layer.

## Keeping it fresh

- **Upkeep (do this):** added/removed/renamed files? Fix the affected CONTEXT.md (and the
  router if top-level layout changed) in the same session, then run
  `python3 .icm/drift-check.py --update` from the project root. Content-only edits
  usually need no doc change.
- **Detection:** `.icm/manifest.json` fingerprints each workspace; `.icm/drift-check.py`
  (run manually, or automatically via a SessionStart hook where the icm skill is installed)
  prints an `[icm]` warning when code drifted from docs — *structural* drift (files
  added/removed: routing tables genuinely stale) vs *content-only* (routing usually fine,
  re-verify source for load-bearing work). The checker ships in-repo, so no skill install
  is needed to run it.
- **Repair:** `/icm sync` fixes only what the warning names. `/icm expand` adds layers
  (3→4→5) if the project grows into them.
