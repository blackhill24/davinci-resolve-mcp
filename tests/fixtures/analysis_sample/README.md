# `analysis_sample` — a real analysis root, checked in

Two `analysis.json` reports copied **verbatim** out of a real
`~/Documents/davinci-resolve-mcp-analysis` run, laid out exactly as the
analyzer writes them (`<root>/clips/<clip_dir>/analysis.json`):

| clip dir | shape it covers |
|---|---|
| `interview.mov-f04bb94d0e28` | full report: transcription with 2 segments / 30 word rows |
| `broll_a.mov-54dadac5801b` | full report, **no** transcript — the empty-words path |

Both describe synthetic pilot media the repo's own harnesses generate under
`/tmp` — no real footage, no personal data, nothing machine-specific.

## Why it is checked in

`test_round_trip_real_sample_roots` and `test_backfill_real_sample_roots`
guard ingest→export exactness against reports the analyzer actually emits,
not a handwritten stand-in. They used to look only at a hardcoded path under
`~/Documents`, so on every machine without that exact directory they skipped —
which was every machine. A guard that skips is a guard that is not running.

With this fixture they always have real input, so they always run. Any
analyzed roots present under `~/Documents/davinci-resolve-mcp-analysis` are
added on top as extra coverage.

## Editing

Don't hand-edit these files. The round-trip assertion is byte-exact after a
canonical JSON dump; an edit that drops or reshapes a key weakens the guard
silently. To refresh, copy a whole newer `analysis.json` over the old one.
