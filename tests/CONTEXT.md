# tests — Context (ICM Layer 2)

~170 Python tests, flat. Split by whether they need a running Resolve: `test_*` run offline;
`live_*` require Resolve (and often Studio) connected.

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Add an offline unit test | `test_<area>.py` near the feature; `_error_envelope_helpers.py` | `live_*` | — |
| Add a live-Resolve validation | `live_<domain>_validation.py` examples | `test_*` | `docs/process/release-process.md` |
| Cloud-project live test setup | `cloud-test-setup.md`, `live_cloud_project_validation.py` | `test_*` | issue #25 |
| Smoke-check imports/wiring | `test_import.py` | `live_*` | — |
| Auto-edit pipeline tests | `test_auto_edit.py`, `test_auto_edit_tool.py`, `test_auto_edit_polish.py` (Phase-2 polish decision layer), `test_cut_ir_words.py`, `test_music_analysis.py`, `test_drt_diff.py` (export-diff differ) | `live_auto_edit_validation.py`, `live_auto_edit_twosource_polish.py` (#13 cross-dissolve), `live_auto_edit_ducking_probe.py` unless live | — |
| resolve-advanced bridge tests | `test_advanced_bridge.py` (read-only panel), `test_advanced_bridge_ops.py` (drt/drp write ops; skips w/o node) | `live_*` | — |
| Audio/subtitle export-diff RE probes (#22) | `live_pan_probe.py`, `live_audio_fx_probe.py`, `live_channel_format_probe.py` (drp `VirtualAudioTracksBA`), `live_subtitle_probe.py` (drt `SubtitleTrackVec`; text = protobuf-in-zstd `EffectFiltersBA`, timing = plain XML; adds a `roundtrip` phase); setup/diff/cleanup phases around a manual GUI edit | `test_*` | — |
| SRT import codec probe (#22, 3.2.5) | `live_srt_import_probe.py` (oracle/author/import): validated subtitle-text codec (protobuf tree + BMD length cascade + BMD-exact zstd framing) authoring arbitrary-length cue text; `oracle` phase self-checks offline against 2 embedded ground-truth blobs. Needs `zstandard` (not a repo dep) | `test_*` | — |
| Subtitle codec + import_srt tool (#30) | `test_subtitle_codec.py` (offline oracle + style), `live_import_srt_tool_probe.py` (tool + synthetic-template feasibility) | — | codec: `src/utils/subtitle_codec.py` |
| Stage-3-tail live probes (#30) | `live_retime_probe.py` (set_clip_speed/fit_to_fill), `live_render_in_place_probe.py` (gates `idle`), `live_multicam_drt_probe.py` (setup/diff around a manual GUI multicam step) | `test_*` | — |
| Benchmark the server | `benchmark_server.py` | `live_*` | `scripts/measure_bridge_cost.py` |
| Set up a test timeline | `create_test_timeline.py` | — | — |

## Key files (only where the name doesn't say enough)

- `_error_envelope_helpers.py` — shared assertions for the action-dispatch error envelope;
  reuse when asserting tool responses.
- `preflight.py` — pre-run Resolve status gate (closed / open_no_project / open_project);
  `--require open|project|timeline`, `--json`; exit 0 ready, 2 not ready, 3 no scripting.
  Every `live_*` `__main__` calls `gate()` — new live harnesses must too.
- `test-after-restart.sh` / `.bat` — post-restart validation harness (`.sh` calls preflight).

## Conventions & gotchas

- `live_*` tests are excluded from offline CI — they connect to a real Resolve; follow the
  live-validation guidance in `docs/process/release-process.md`. Harness `gate()` calls
  set `DAVINCI_MCP_NO_AUTOLAUNCH=1` so a closed Resolve fails fast, not launches.
- For Resolve-behavior changes, update focused tests rather than broad ones.

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root. Content-only
> edits usually need no doc change. Keep this file ≲40 lines.
