# DaVinci Resolve Scripting API — Limitations & Feedback

<!-- GENERATED FILE — do not edit by hand.
     Source: src/core/api_truth.py (entries tagged `submit`).
     Regenerate: venv/bin/python scripts/gen_api_limitations.py -->

This is a curated, behaviorally-verified list of DaVinci Resolve scripting
API gaps and bugs encountered while building this MCP server, intended for
submission to Blackmagic Design's developer feedback. Every item was
observed against live Resolve; each entry notes the current workaround (or
that none exists).

**Verified on:** DaVinci Resolve Studio 21.0.0

**Totals:** 18 missing capabilities, 13 bugs / unreliable behaviors.

The authoritative source is the runtime-queryable `api_truth` ledger
(`resolve_control api_truth "<query>"`); this document is generated from
it and stays in sync via a drift guard.

### Scope & completeness

This list is **not guaranteed exhaustive.** It combines (a) issues hit
while building this MCP server, (b) a `dir()` surface audit of the live
Resolve API objects (ProjectManager, Project, MediaPool, MediaPoolItem,
Timeline, TimelineItem, Graph) diffed against Resolve's UI feature set,
and (c) a live mutating harness (`tests/live_api_gap_verification.py`)
that attempts each operation against a disposable project built from
synthetic media and confirms it fails while a related control succeeds.
That catches absent methods and documented constraints, but not subtler
issues: parameters that exist yet misbehave, version-specific regressions,
or capabilities we simply never exercised. New findings are added as
`submit`-tagged `api_truth` entries and this document is regenerated.

Note: `hasattr()`/`getattr()` cannot be used to probe this API — the
Python bridge fabricates a callable for any attribute name (see the
`hasattr` bug below). Method existence here was checked with `dir()`.

## Missing Capabilities (please add)

Functionality that exists in the Resolve UI but has no scripting API
equivalent, blocking full automation.

### Timeline.GetTimelineByName

- **Object:** `Project`
- **Behavior:** Does not exist. Timelines are looked up by index.
- **Workaround / current handling:** Iterate GetTimelineByIndex(1..GetTimelineCount()).
- **Tags:** missing-method, timeline

### Resolve.GetUIManager

- **Object:** `Resolve`
- **Behavior:** Does not exist. There is no scripting API to open the Project Settings or Preferences dialogs, save/load UI layouts via a UIManager object, or enumerate layout presets from disk (layout presets are not stored under Presets/UILayouts). Tools built on this fabricated surface could never succeed and were removed (2026-07-20 audit).
- **Workaround / current handling:** Use the real layout-preset API instead: Resolve.SaveLayoutPreset / LoadLayoutPreset / ExportLayoutPreset / ImportLayoutPreset / DeleteLayoutPreset / UpdateLayoutPreset. Dialogs cannot be opened programmatically at all.
- **Tags:** missing-method, ui, layout-presets

### Source Track Selector / destination track for Insert*IntoTimeline

- **Object:** `Timeline`
- **Behavior:** There is no API to read or set the Source/Auto Track Selector (the Edit-page patch panel that picks the destination track). InsertTitleIntoTimeline, InsertFusionTitleIntoTimeline, InsertGeneratorIntoTimeline, InsertFusionGeneratorIntoTimeline, InsertOFXGeneratorIntoTimeline and InsertFusionCompositionIntoTimeline take no trackIndex and always drop the clip on the selector's current target (V1 in practice). Locking lower video tracks does NOT redirect the insert — verified live on 21.0.0: locking V1 makes the insert FAIL rather than land on V2. Titles/generators also can't be moved afterward (no MediaPoolItem, so AppendToTimeline clipInfo and MoveClips don't apply). Re-probed on 21.0.2.4 (issue #23, 3.3.5): dir(Timeline) still exposes NO selector/target-track/patch method — the gap is unchanged in 21.0.
- **Workaround / current handling:** Placement layer shipped (issue #23, 3.3.5): timeline safe_place_overlay wraps the Insert*IntoTimeline calls — it pre-checks the V1 lock (so a locked target fails loudly with V1_LOCKED instead of silently misfiring), warns when a non-V1 target is requested (unreachable), and confirms the insert landed by counting V1 items. Live-verified on 21.0.2.4 (tests/domains/timeline_edit/live_track_selector_probe.py). Still V1-only and still unmovable afterward. For clips that DO have a MediaPoolItem, target a track with MediaPool.AppendToTimeline's clipInfo 'trackIndex' (media_pool append_to_timeline clip_infos). See issue #74.
- **Reference:** [issue #74](https://github.com/samuelgursky/davinci-resolve-mcp/issues/74)
- **Tags:** missing-method, timeline, title, generator, track

### Native multicam clip creation

- **Object:** `MediaPool`
- **Behavior:** There is no scripting method to create a native multicam clip from a set of angles (the R21 API doc has zero multicam mentions). Angles can be stacked onto tracks programmatically (media_pool setup_multicam_timeline), but the multicam-clip conversion itself is a UI-only step. The on-disk encoding IS captured (tests/domains/timeline_conform_interchange/live_multicam_drt_probe.py, export-diff of the UI conversion on 21.0.2.4): the multicam clip is its own Sm2SequenceContainer (a new SeqContainer/<uuid>.xml, NOT an MpVideoClip) with one Sm2TiTrack per angle in VideoTrackVec + a mirrored AudioTrackVec; each angle track carries a protobuf FieldsBlob (NumLayers, AngleId='Camera N'), UserDefinedName 'Angle N', a shared <Sequence> uuid, and an Items->Sm2TiVideoClip pointing at the source MediaRef/MediaFilePath with Start/Duration set from the sync (timecode). Media-pool side: the source clips are relocated into a new '000_Original Clips' MpFolder and the master gains a STANDARD_CLIP '<first> Multicam' entry whose VirtualAudioTracksBA references the container.
- **Workaround / current handling:** Live: not possible -- prepare a stacked timeline (media_pool setup_multicam_timeline) and finish the multicam-clip conversion in the Resolve UI. Offline: authorable in principle via resolve-advanced/vendor/drp-format SeqContainer machinery, but NOT implemented -- it is a heavy new vendor op (author a fresh Sm2SequenceContainer + per-angle Sm2TiTrack, re-encode the zstd-compressed protobuf FieldsBlobs -- same codec family cracked in 3.2.4 -- relocate originals into a subfolder, add the '<first> Multicam' master clip, and thread it through a full-project .drp export->edit->reimport). See issue #30 (3.1.7).
- **Reference:** [issue #30](https://github.com/samuelgursky/davinci-resolve-mcp/issues/30)
- **Tags:** missing-method, media-pool, multicam, drp, offline, investigated-not-implemented

### Transition create / copy / clone

- **Object:** `Timeline / TimelineItem`
- **Behavior:** The scripting API exposes no method to add, read, copy, or clone an edit transition (cross-dissolve, etc.). Transitions applied in the UI are invisible to and unmodifiable by scripts.
- **Workaround / current handling:** Apply/duplicate transitions in the Resolve UI, or use timeline(action='add_transition'|'list_transitions') — offline drt surgery via resolve-advanced (drp-format place_transition), landing on a NEW timeline (Stage 3.1, issue #21).
- **Tags:** missing-method, timeline, transition

### Cloud project enumeration / export / user management

- **Object:** `ProjectManager`
- **Behavior:** Only CreateCloudProject, LoadCloudProject, ImportCloudProject and RestoreCloudProject exist. There is no GetCloudProjectList (list available cloud projects), no ExportToCloud, and no Add/RemoveUserToCloudProject — so cloud collaboration can't be fully automated.
- **Workaround / current handling:** Drive cloud project listing, export, and collaborator management from the Resolve UI; only create/load/import/restore are scriptable.
- **Tags:** missing-method, project, cloud

### TimelineItem trim / move / re-time (no position setters)

- **Object:** `TimelineItem`
- **Behavior:** TimelineItem exposes GetStart, GetEnd, GetDuration, GetLeftOffset, GetRightOffset and GetSourceStart/EndFrame, but NO matching setters. A clip cannot be trimmed, slipped, slid, rolled, moved to another time/track, or have its duration changed once it is on the timeline. Verified via dir() on Resolve 21.0.0 (getters only).
- **Workaround / current handling:** Do edit-point adjustments in the Resolve UI, or use timeline(action='trim_clip'|'move_clip'|'slide_clip'|'slip_clip') — offline drt surgery via resolve-advanced (drp-format), landing on a NEW timeline (Stage 3.1, issue #21). slip_clip supports both directions since issue #30 (3.1.5): a single vendor slip primitive shifts the source in-point by +/- frames, live-bounds-checked at BOTH ends (GetLeftOffset/GetRightOffset headroom) before export.
- **Tags:** missing-method, timeline, edit, trim

### Razor / blade / split a timeline item

- **Object:** `Timeline / TimelineItem`
- **Behavior:** There is no method to split/cut/blade a clip at a given frame. Verified absent on Timeline and TimelineItem (dir(), 21.0.0).
- **Workaround / current handling:** Split in the Resolve UI, construct the cut up-front by appending two clipInfos with the desired in/out points, or use timeline(action='split_clip') — offline drt surgery via resolve-advanced (drp-format), landing on a NEW timeline (Stage 3.1, issue #21).
- **Tags:** missing-method, timeline, edit

### Clip speed / retime ratio and speed ramps

- **Object:** `TimelineItem`
- **Behavior:** SetProperty exposes only retime *quality* (RetimeProcess, MotionEstimation) and transform/crop/composite/opacity keys — not the speed value itself. There is no way to set a clip to a given % speed, reverse it, or author a speed ramp. Verified against the documented SetProperty key list AND by live mutating attempt on 21.0.0: SetProperty('Speed'|'PlaybackSpeed'|'RetimeSpeed'|'ClipSpeed', 50) all return False, while SetProperty('RetimeProcess', 1) returns True.
- **Workaround / current handling:** Workaround shipped (issue #30, 3.1.5): timeline(action='set_clip_speed') — offline drt surgery that swaps the clip's MediaTimemapBA blob (constant speed, reset to 1x, or a variable-speed ramp via record/source keyframes) and rescales the record Duration, landing on a NEW timeline. fit_to_fill_edit derives the speed from a target duration; ramp new_duration auto-derives from the last keyframe's record_sec x fps. The codec (vendor retime-clip.js) round-trips live-captured 50% and dynamic-ramp blobs byte-for-byte. retime_linked_audio=True retimes the linked audio in the same pass (pitch changes); default leaves audio untouched.
- **Tags:** missing-method, timeline, retime, speed, workaround

### Color node graph editing and primary grade values

- **Object:** `Graph / TimelineItem`
- **Behavior:** The Graph object exposes node enable/label/count, LUT get/set, cache mode, ResetAllGrades, ApplyGradeFromDRX and ApplyArriCdlLut; TimelineItem adds SetCDL, CopyGrades and color versions. But you cannot add, delete, or connect nodes, and you cannot read or write primary grade values (lift/gamma/gain/offset/contrast/curves/qualifiers/power windows). Grading is limited to CDL, whole-grade DRX/LUT application and copying.
- **Workaround / current handling:** Build node trees and dial grades in the Resolve UI, or author them OFFLINE and apply live: the resolve-advanced `drx` codec now writes node trees + primaries (issue #23, 3.3.1), applied via ApplyGradeFromDRX. Per-parameter grade control is still not scriptable through the LIVE API (no live read/write of primaries or node edits).
- **Tags:** missing-method, color, grade, node

### Fairlight audio levels / pan / EQ / automation / FairlightFX

- **Object:** `TimelineItem / Timeline`
- **Behavior:** There is no API to set clip or track volume, pan, EQ, audio automation, or to add/configure FairlightFX. SetProperty covers video transform only; the audio surface is read-only (GetSourceAudioChannelMapping, GetAudioMapping, voice isolation). Verified via dir() + SetProperty docs AND by live mutating attempt on 21.0.0: SetProperty('Volume'|'Level'|'Gain'|'AudioVolume', 0) all return False (note 'Pan' is the VIDEO transform key, not audio pan, so it misleadingly succeeds).
- **Workaround / current handling:** Mix in the Fairlight UI; only voice-isolation state and channel-mapping reads are scriptable. Offline .drt-surgery workarounds exist for SOME of these -- see the paired entries below: clip volume (issue #14) and clip pan (issue #22) are solved; track/channel EQ is confirmed to have no .drt workaround; clip-level FairlightFX round-trips but isn't decoded yet.
- **Tags:** missing-method, audio, fairlight

### Proxy / optimized-media generation

- **Object:** `MediaPoolItem`
- **Behavior:** Only LinkProxyMedia, UnlinkProxyMedia and LinkFullResolutionMedia exist (attach/detach EXISTING proxies). There is no method to generate proxies or optimized media. Verified via MediaPoolItem dir() (21.0.0).
- **Workaround / current handling:** Workaround shipped (issue #23, 3.3.4): render build_proxies composes the render queue with LinkProxyMedia — renders the target clips as INDIVIDUAL clips into a proxy dir (ExportAudio=False to dodge the headless Fairlight/PipeWire 0%-stall), matches each output to its source by filename, LinkProxyMedia, and verifies via the clip's 'Proxy Media Path' readback. Live-verified on 21.0.2.4 (tests/domains/render_deliver/live_proxy_build_probe.py). GOTCHA: Resolve's render queue refuses to write into the system temp dir (AddRenderJob fails), so proxy_dir must be a real media location — the action defaults require_temp_target=False for that reason. Optimized media (as distinct from proxies) still has no generate or link API and stays UI-only.
- **Reference:** [issue #23](https://github.com/samuelgursky/davinci-resolve-mcp/issues/23)
- **Tags:** missing-method, media-pool, proxy

### Insert / Overwrite / Replace / Fit-to-Fill edit modes

- **Object:** `MediaPool / Timeline`
- **Behavior:** MediaPool.AppendToTimeline (with optional recordFrame positioning) is the only programmatic placement. The standard edit modes — insert (ripple), overwrite, replace, fit-to-fill, place-on-top — have no API. Verified via dir() (21.0.0).
- **Workaround / current handling:** Position clips with AppendToTimeline clipInfo recordFrame, or use the composed workarounds: timeline(action='insert_edit'|'replace_edit'|'place_on_top_edit') (Stage 3.1, issue #21) and timeline(action='fit_to_fill_edit') (issue #30, 3.1.5 — retimes the clip so its source segment fills a target duration via the MediaTimemapBA codec).
- **Tags:** missing-method, timeline, edit

### Render in Place / bake a timeline clip to new media

- **Object:** `Timeline / TimelineItem / MediaPool`
- **Behavior:** There is no scripting method for the Edit-page clip context-menu action 'Render in Place', which bakes a clip (including its Fusion composition and effects) into a NEW rendered media file and drops that file back on the timeline at the same position, replacing the source clip. No Render*/Bake*/Freeze* method exists on Timeline, TimelineItem or MediaPool in the Resolve scripting API reference (BMD docs) or a dir() audit. NOTE the frequently-confused-but-distinct sibling: the render *cache* (a temporary, non-destructive cache of a clip's Color/Fusion output that reduces playback load WITHOUT creating a new media file) IS scriptable — TimelineItem.SetColorOutputCache / SetFusionOutputCache ('Render Cache Color/Fusion Output' menu actions) and Graph.SetNodeCacheMode. Render in Place is the permanent, media-producing bake; the render cache is the transient one.
- **Workaround / current handling:** If the goal is only to reduce playback/render load, use the render cache — exposed as timeline_item get_color_cache/set_color_cache/get_fusion_cache/set_fusion_cache and the Color-page graph node cache_mode (no new media, fully reversible). For a baked media file, the composed workaround shipped in issue #30 (3.1.6): timeline(action='render_in_place') — single-clip render of the clip's record range (MarkIn/MarkOut) into a real media dir, import, and same-position replace. isolate=True (default) disables the other video tracks during the render (restored after) so the bake matches the UI's isolated-clip semantics; isolate=False bakes the track composite and warns when other tracks overlap. See issue #86.
- **Reference:** [issue #86](https://github.com/samuelgursky/davinci-resolve-mcp/issues/86)
- **Tags:** missing-method, timeline, render, cache, render-in-place, bake

### Smart Bins / Power Bins creation

- **Object:** `MediaPool`
- **Behavior:** Only AddSubFolder (a regular bin) exists. Smart Bins (rule-based) and Power Bins (cross-project) cannot be created or configured. Verified via MediaPool dir() (21.0.0).
- **Workaround / current handling:** Create Smart/Power Bins in the Resolve UI; only regular bins are scriptable. Offline-authoring verdict (issue #23, 3.3.2): see the paired DB-representation entry below.
- **Tags:** missing-method, media-pool, bins

### Subtitle track styling and presets

- **Object:** `TimelineItem / Timeline / Project`
- **Behavior:** There is no API method to set or query subtitle font family, font size, text color, background color, outline, shadow, position, alignment, or to apply/query subtitle style presets. TimelineItem.GetProperty() on subtitle items returns only transform/composite keys. Timeline.GetSetting() and Project.GetSetting() return None for all probed subtitle-style keys (e.g. 'subtitleFontName', 'subtitleFontSize', 'subtitleTextColor', 'subtitleBackgroundColor', 'subtitlePosition', 'subtitleAlignment', 'subtitlePreset', 'subtitleStyle'). Verified via dir(), GetProperty(), and GetSetting() on Resolve 21.0.0.48.
- **Workaround / current handling:** Per-cue styling IS writable offline (issue #30, 3.2.6): the cue's EffectFiltersBA BMD leaf carries, after the text, [u32-LE len][FontName UTF-16LE][float32-LE size][u32-LE len]['#rrggbb' UTF-16LE] — font swaps ride the validated length cascade, size is a fixed float overwrite, color a string swap (src/domains/audio_fairlight/utils/subtitle_codec.py read_cue_style/author_cue_effblob; exposed as the style option of timeline import_srt). Track-level presets and the remaining attributes (background, outline, shadow, alignment) stay UI-only pending further RE.
- **Tags:** missing-method, subtitle, style, preset, workaround

### Speech recognition engine selection

- **Object:** `Timeline`
- **Behavior:** Timeline.CreateSubtitlesFromAudio(autoCaptionSettings) always uses the built-in Resolve speech recognition engine. There is no API parameter to select an alternative provider (e.g. whisper-cli, Google Speech, AWS Transcribe). The language selection via resolve.AUTO_CAPTION_LANGUAGE_* is the only customization; the engine itself cannot be changed.
- **Workaround / current handling:** No workaround for provider selection. To use an external ASR engine, transcribe outside Resolve and bring the result in as a subtitle track (see the SRT-import entry).
- **Tags:** missing-method, subtitle, transcription, speech-recognition, asr

### Media Pool folder rename

- **Object:** `MediaPool`
- **Behavior:** MediaPool exposes AddSubFolder(name), DeleteSubFolders([names]), and MoveFolders([names], targetFolder) but no RenameSubFolder(oldName, newName) method. Folders can be created, deleted, and moved, but their names cannot be changed through the API. Verified via dir() on Resolve 21.0.0.
- **Workaround / current handling:** Two workarounds shipped (issue #23, 3.3.3). LOSSLESS (preferred): close Resolve and rename offline via the advanced server's project_db rename_folder — a direct Sm2MpFolder.Name UPDATE (backup + schema guard). LIVE fallback: media_pool rename_folder does a delete-recreate that PRESERVES clips + subfolders but LOSES the ColorTag, the folder UniqueId (references break) and manual clip ordering — confirm-gated, with a dry_run preview. Both live-verified on 21.0.2.4 (tests/domains/color_grade/live_folder_rename_probe.py, test/project-db-rename-roundtrip.test.mjs).
- **Reference:** [issue #23](https://github.com/samuelgursky/davinci-resolve-mcp/issues/23)
- **Tags:** missing-method, media-pool, folder

## Bugs / Unreliable Behavior (please fix)

Methods that exist but misbehave — silent failures, unreliable return
values, or automation-hostile modal prompts.

### MediaPool.AutoSyncAudio

- **Object:** `MediaPool`
- **Signature:** `(clips, settings) -> bool`
- **Behavior:** The boolean return does not reflect whether clips actually linked, and string enum keys in `settings` are silently rejected (the call returns False).
- **Workaround / current handling:** Resolve the AUDIO_SYNC_* enum constants via the live resolve handle, and verify by reading each clip's 'Synced Audio' property (see verify_by_readback).
- **Tags:** unreliable-return, silent-failure, audio, enum

### Timeline.CreateSubtitlesFromAudio

- **Object:** `Timeline`
- **Signature:** `(autoCaptionSettings) -> bool`
- **Behavior:** Same failure mode as AutoSyncAudio: the autoCaptionSettings dict is keyed by resolve.SUBTITLE_* enum constants with resolve.AUTO_CAPTION_* enum values, so plain string keys like {'language': 'korean'} are silently rejected (returns False, no subtitle track created). The boolean is also unreliable.
- **Workaround / current handling:** Resolve the SUBTITLE_*/AUTO_CAPTION_* constants via the live resolve handle (server._normalize_auto_caption_settings) and verify by reading the timeline's subtitle track count before/after (server._safe_create_subtitles).
- **Tags:** unreliable-return, silent-failure, subtitle, enum

### ProjectManager CloudProject family (Create/Load/Import/RestoreCloudProject)

- **Object:** `ProjectManager`
- **Signature:** `(..., cloudSettings) -> Project | bool`
- **Behavior:** All four take an enum-keyed {cloudSettings} dict (resolve.CLOUD_SETTING_* keys, resolve.CLOUD_SYNC_* sync-mode values). Plain string keys are silently rejected, so a settings dict built from human-readable keys yields no project / False.
- **Workaround / current handling:** Resolve the CLOUD_SETTING_*/CLOUD_SYNC_* constants via the live resolve handle (server._normalize_cloud_settings) before calling, and treat the bool return from Import/RestoreCloudProject as advisory — verify by reading ProjectManager.GetProjectListInCurrentFolder() back (server._verify_cloud_import_restore, mirrored by cloud_operations._verify_cloud_mutation for the granular surface) rather than trusting it.
- **Tags:** silent-failure, project, cloud, enum

### Timeline.Export

- **Object:** `Timeline`
- **Signature:** `(fileName, exportType, exportSubtype) -> bool`
- **Behavior:** exportType/exportSubtype must be resolve.EXPORT_* enum *values* resolved from the live handle. A JSON/MCP caller cannot pass a live enum, and a plain string ('fcpxml', or even the constant name 'EXPORT_FCPXML_1_10') is silently rejected with no file written.
- **Workaround / current handling:** Map a friendly format/subtype to the EXPORT_* constant and resolve it against the live handle (server._timeline_export_spec) before calling; verify the output file exists afterward.
- **Tags:** silent-failure, timeline, export, enum

### ProjectManager.DeleteProject

- **Object:** `ProjectManager`
- **Signature:** `(projectName) -> bool`
- **Behavior:** Returns False (no deletion) when the target project is, or recently was, the current project, and is flaky on the first attempt — so a single bool() call leaves the project undeleted with no useful error. Guard re-verified live on Studio 21.0.2.4 (Stage 4, #24): delete_project_safely deleted the disposable project in 1 attempt across multiple runs, so the switch-away-then-retry mitigation still holds.
- **Workaround / current handling:** Load/close away from the target first, then retry; use src/domains/project_lifecycle/utils/project_cleanup.py:delete_project_safely.
- **Tags:** unreliable-return, project, flaky

### Composition.Paste

- **Object:** `Fusion Composition`
- **Behavior:** Passing tool.SaveSettings()'s in-memory table to Paste() / LoadSettings() fails across the Python bridge with an OrderedDict/null-argument error and creates no node, while reporting nothing useful.
- **Workaround / current handling:** Duplicate via AddTool(RegID) + SaveSettings(path)/LoadSettings(path) through a temp .setting FILE, which round-trips reliably. Identify the new node by name diff.
- **Tags:** fusion, bridge, silent-failure

### FlowView.SetPos / FlowView.GetPosTable

- **Object:** `Fusion FlowView (comp.CurrentFrame.FlowView)`
- **Behavior:** Node positions are read/written through the FlowView, not the tool. SetPos returns nothing reliable; GetPosTable returns a 1-indexed table (or dict/tuple depending on bridge). comp.CurrentFrame is only populated while the Fusion page is active — resolving a comp via timeline scope (no page switch) gives CurrentFrame=None, so FlowView is unavailable until resolve.OpenPage('fusion') runs first. Round-trip re-verified live on Resolve Studio 21.0.2.4 (server.fusion_comp set_position/get_position via copy_tool's new node): position sets and reads back within ~0.001 float tolerance once the Fusion page is open.
- **Workaround / current handling:** Use comp.CurrentFrame.FlowView.SetPos(tool, x, y); confirm with GetPosTable and a liberal position parser. Call resolve.OpenPage('fusion') before get_position/set_position when working via timeline scope (server._fusion_flow_view now returns a wrong_page error with that remediation).
- **Tags:** fusion, unreliable-return, wrong-page

### MediaPoolItem.GetClipProperty('Transcription')

- **Object:** `MediaPoolItem`
- **Behavior:** Returns a PREVIEW of the transcription that ends in an ellipsis when the full transcript is longer than the property exposes. Not re-triggered live on 21.0.2.4 during Stage 4 (#24): reproducing the truncation needs real speech content long enough to overflow the property, which synthetic silent/tone clips can't provide; the `truncated`-flag guard code is unchanged since it was added. Re-probe with real dictation media to get a live 21.0.2.4 stamp on this entry.
- **Workaround / current handling:** Treat a trailing ellipsis as truncation (see media_pool_item get_transcription's `truncated` flag).
- **Tags:** transcription, truncation

### ProjectManager.CreateProject (with a dirty Untitled project)

- **Object:** `ProjectManager`
- **Behavior:** Returns None and pops a modal 'Save Current Project' dialog when the current unsaved/Untitled project blocks the switch. SaveProject() on an Untitled project re-triggers the same modal. Not live-retriggered on 21.0.2.4 during Stage 4 (#24) by deliberate choice: reproducing it means leaving a dirty Untitled project current and calling raw CreateProject, which risks popping a real blocking modal in the operator's live Resolve session with no scripted way to dismiss it. The recommended workaround is documentation-only (no shipped server.py guard currently routes 'create' through CloseProject first), so there is no code path to verify here — callers/agents must follow it manually.
- **Workaround / current handling:** CloseProject(current) to discard the untitled project without a prompt, then CreateProject; restore with LoadProject afterward.
- **Tags:** project, modal, silent-failure

### hasattr() / getattr() on Resolve API objects (attribute fabrication)

- **Object:** `(all Resolve scripting objects)`
- **Behavior:** getattr(obj, name) returns None (not an AttributeError) for ANY attribute name, so hasattr(obj, 'TotallyMadeUpMethod') is always True even though the attribute doesn't exist and the returned value isn't callable. This makes capability detection by hasattr impossible — re-verified on Resolve Studio 21.0.2.4: getattr(mediaPool, 'SetStart'/'Razor'/'AddNode'/'GenerateProxy') all returned None (callable() is False), yet hasattr() for the same names was True and none appeared in dir(). Only dir() lists the real methods.
- **Workaround / current handling:** Never probe method existence with hasattr/getattr; test membership against dir(obj) instead. Calling the fabricated attribute (obj.MadeUpMethod()) raises TypeError: 'NoneType' object is not callable, rather than returning False silently.
- **Tags:** bridge, introspection, silent-failure

### MediaPoolItem.SetClipProperty('Reel Name', ...)

- **Object:** `MediaPoolItem`
- **Signature:** `(propertyName, propertyValue) -> bool`
- **Behavior:** Setting the 'Reel Name' clip property returns True but the value is silently dropped on read-back when the project is configured to derive reel names automatically (General Options > 'Assist using reel names from the:' set to source clip file / embedding / filename pattern). The same True-but-unpersisted behavior occurs via SetMetadata('Reel Name', ...). Other clip properties on the same clip (e.g. 'Comments') write and persist normally, so this is field-specific, not a bridge/permission failure. Verified on Resolve 21.0.0; reported as issue #77. Re-verified live on Studio 21.0.2.4 (Stage 4, #24): on a disposable project's default gate setting, SetClipProperty actually returned False outright rather than True-with-drop this time — the guard doesn't trust either return value and independently confirms via read-back, so it caught the failure regardless of which shape Resolve reports.
- **Workaround / current handling:** After writing 'Reel Name', read it back with GetClipProperty('Reel Name') and refuse to report success on mismatch; surface the project-setting gate to the caller (server._verify_clip_property_writeback).
- **Reference:** [issue #77](https://github.com/samuelgursky/davinci-resolve-mcp/issues/77)
- **Tags:** unreliable-return, silent-failure, metadata, reel-name

### Project.GenerateSpeech

- **Object:** `Project`
- **Signature:** `({speechGenerationSettings}, timecode) -> MediaPoolItem`
- **Behavior:** Live-tested on Resolve Studio 21.0.2.4 with a plain {'TextInput': '...'} dict (this is NOT an enum-keyed dict — unlike AutoSyncAudio/CreateSubtitlesFromAudio, there is no resolve.SPEECH_*/VOICE_* constant defined on the live resolve handle at all, confirmed via dir(resolve)). The call returns None in well under a second — too fast to be an actual synthesis attempt — consistent with the AI Speech Generator Extra not being installed on this machine, though the API gives no error string to confirm that diagnosis; a box with the Extra installed is needed to verify the settings-dict shape end to end.
- **Workaround / current handling:** Treat a None return as 🔬 hardware/package-gated (missing AI Speech Generator Extra) rather than a settings-shape bug; re-probe on a machine with the Extra installed before assuming the plain-string-key shape is complete (e.g. VoiceModel value formats are still unverified).
- **Reference:** [issue #20](https://github.com/samuelgursky/davinci-resolve-mcp/issues/20)
- **Tags:** ai, speech, extra-gated, resolve-21, unverified-live, silent-failure

### Folder.RemoveMotionBlur

- **Object:** `MediaPool Folder`
- **Signature:** `(clipList) -> [{1: origMediaPoolItem, 2: newMediaPoolItem}, ...]`
- **Behavior:** The batch, folder-level RemoveMotionBlur returns a list of int-keyed dicts per processed clip — {1: orig, 2: new} — not the [orig, new] 2-tuples/lists the obvious call shape suggests. `orig, new = pair` on such a dict silently unpacks its KEYS (1, 2) instead of the values, so orig/new end up as bare ints; calling .GetName() on them threw AttributeError that a broad except then swallowed — folder-level deblur reported success=True with an always-empty `created` list, silently dropping every result. Live-verified on Resolve Studio 21.0.2.4 (issue #20). MediaPoolItem.RemoveMotionBlur (single-clip, non-batch) already returns the new clip directly and is unaffected.
- **Workaround / current handling:** Detect the dict shape explicitly: (pair[1], pair[2]) if isinstance(pair, dict) else pair. Fixed in src/granular/folder.py and server.py's folder() compound action; the offline test stub (Folder21.RemoveMotionBlur in test_resolve21_actions.py) previously returned the wrong tuple shape too and was updated to the live-verified dict shape so regressions are caught offline.
- **Reference:** [issue #20](https://github.com/samuelgursky/davinci-resolve-mcp/issues/20)
- **Tags:** unreliable-return, undocumented-shape, media-pool, silent-failure, resolve-21
