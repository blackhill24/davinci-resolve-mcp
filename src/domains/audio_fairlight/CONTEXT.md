# Audio / Fairlight — Context (ICM Layer 3)

Setting audio properties, syncing audio, isolating voice, generating subtitles,
planning Fairlight tracks/buses, checking loudness, routing buses, or
splitting/trimming/converting audio. Prompt: `/audio_workflow`.

**No tools of its own** — `actions.py` holds only private (`_`-prefixed) helpers.
Every entry point is `timeline`/`timeline_ai` in `timeline_edit`, which lazily
imports and dispatches into this file's helpers (e.g. `_timeline_set_clip_volume_impl`,
`_safe_auto_sync_audio`, `_safe_create_subtitles`). `auto_edit` also lazily calls
`_safe_create_subtitles` for its render step.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Clip volume/pan, audio mapping, voice isolation | `actions.py` (`_timeline_set_clip_*_impl`, `_voice_isolation_capabilities`, `_audio_mapping_report`) | `granular/` | called from `timeline_edit.actions.timeline`/`timeline_ai` |
| Auto-sync audio | `actions.py` (`_safe_auto_sync_audio`, `_normalize_auto_sync_settings`) | — | |
| Subtitle generation / SRT | `actions.py` (`_safe_create_subtitles`, `_transcription_capabilities`), `utils/subtitle_codec.py` (`.drt` codec) | — | oracle-validated var-length codec; see issue #22/#30 history |
| Live capability probe | `utils/audio_fairlight_live_probe.py` | — | regenerates kernel counts |

## Key files (only where the name doesn't say enough)

- `utils/subtitle_codec.py` — the reverse-engineered `.drt` `SubtitleTrackVec` codec
  (protobuf-in-zstd text + plain-XML timing); backs `import_srt`.

## Conventions & gotchas

- New audio/subtitle behavior is added here but WIRED into `timeline`/`timeline_ai`
  in `timeline_edit/actions.py` — don't expect a standalone entry point.
- Live: `timeline` (audio-plan actions). Offline (resolve-advanced): `audio_plan`.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/audio-fairlight-kernel.md`. Skill: `.claude/skills/audio-fairlight.md`.
> Keep this file ≲40 lines.
