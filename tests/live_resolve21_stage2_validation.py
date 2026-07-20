"""Live validation harness for Stage 2 (issue #20) — the Resolve 21.0 API delta.

Imports the granular tool functions directly and calls them against a live
DaVinci Resolve. Reports pass/fail for each Stage 2 addition.

Run with: .venv/bin/python tests/live_resolve21_stage2_validation.py

Tools validated (the SAFE battery — no GPU render, no AI audio generation):
  - resolve_control.disable_background_tasks_for_current_session
  - folder/media_pool_item: perform_audio_classification, clear_audio_classification
  - folder/media_pool_item: analyze_for_intellisearch, analyze_for_slate
    (🔬 hardware/Extra-gated — reports "requires ... Extra" as an expected
    result, not a failure, when the Extra isn't installed)
  - folder/media_pool_item: transcribe_audio(use_speaker_detection=...) passthrough
  - project.set_super_scale / media_pool_item.set_clip_super_scale (plain +
    '2x Enhanced' 4-arg form), verified by GetSetting/GetClipProperty readback

Deliberately NOT run here (real GPU render / AI audio generation; the box has
documented history of Fairlight/ALSA-duplex render hangs requiring a Resolve
restart — see memory "resolve-headless-render-hang"): Project.GenerateSpeech,
Folder/MediaPoolItem.RemoveMotionBlur. These need a separate, explicitly
confirmed run — see live_resolve21_stage2_render_validation.py.
"""

import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def make_synthetic_media(work_dir):
    """One synthetic .mov with a tone (for audio-classification/transcription) via ffmpeg."""
    path = os.path.join(work_dir, "stage2_synthetic.mov")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:d=3:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-shortest", "-y", path,
    ], check=True)
    return path


def report(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def report_gated(name, out, gated_hint="Extra"):
    """For AI-Extra-gated ops: success, or a clean 'requires ... Extra' error, both PASS."""
    if out.get("success") is True:
        return report(name, True, "ran (Extra installed)")
    err = str(out.get("error", ""))
    if gated_hint in err or "21+" in err:
        return report(name, True, f"🔬 gated — {err}")
    return report(name, False, f"got {out!r}")


def main():
    from src.granular.common import get_resolve
    from src.granular import folder as gfolder
    from src.granular import media_pool_item as gclip
    from src.granular import project as gproj

    print("=" * 70)
    print("Stage 2 (issue #20) live validation — safe battery")
    print("=" * 70)

    r = get_resolve()
    if r is None:
        print("FATAL: cannot connect to DaVinci Resolve. Is it running?")
        return 2
    print(f"Connected to Resolve {r.GetVersionString()}")

    pm = r.GetProjectManager()
    project_name = f"stage2_r21_delta_{int(time.time())}"
    if not pm.CreateProject(project_name):
        print(f"FATAL: failed to create disposable project '{project_name}'")
        return 2
    print(f"Created disposable project: {project_name}")

    work_dir = tempfile.mkdtemp(prefix="stage2_r21_delta_")
    print(f"Synthetic media in: {work_dir}")

    try:
        clip_path = make_synthetic_media(work_dir)

        proj = pm.GetCurrentProject()
        mp = proj.GetMediaPool()
        imported = mp.ImportMedia([clip_path])
        if not imported:
            print("FATAL: failed to import synthetic media")
            return 2
        clip = imported[0]
        clip_id = clip.GetUniqueId()
        print(f"Imported clip. id: {clip_id}")

        results = []

        # ─── 2.1 disable_background_tasks_for_current_session ───
        from src.granular import resolve_control as gctrl
        out = gctrl.disable_background_tasks_for_current_session()
        results.append(report(
            "resolve_control.disable_background_tasks_for_current_session",
            out.get("success") is True,
            f"got {out!r}",
        ))

        # ─── 2.3/2.6 perform/clear_audio_classification (folder + clip) ───
        out = gfolder.folder_perform_audio_classification()
        results.append(report_gated("folder.perform_audio_classification", out))
        out = gfolder.folder_clear_audio_classification()
        results.append(report_gated("folder.clear_audio_classification", out))
        out = gclip.perform_clip_audio_classification(clip_id)
        results.append(report_gated("media_pool_item.perform_audio_classification", out))
        out = gclip.clear_clip_audio_classification(clip_id)
        results.append(report_gated("media_pool_item.clear_audio_classification", out))

        # ─── 2.5/2.8 analyze_for_intellisearch / analyze_for_slate (folder + clip) ───
        out = gfolder.folder_analyze_for_intellisearch(identify_faces=False, is_better_mode=False)
        results.append(report_gated("folder.analyze_for_intellisearch", out))
        # AnalyzeForSlate detects an actual slate/clapperboard in frame; our
        # synthetic solid-color clip has none, so a clean {"success": False}
        # (no error) is the CORRECT content-dependent result, same as
        # IntelliSearch's True is correct (it only needs any visual content).
        out = gfolder.folder_analyze_for_slate(marker_color="Blue")
        results.append(report(
            "folder.analyze_for_slate",
            "error" not in out,
            f"got {out!r} (False is expected — no slate in synthetic clip)",
        ))
        out = gclip.analyze_clip_for_intellisearch(clip_id, identify_faces=False, is_better_mode=False)
        results.append(report_gated("media_pool_item.analyze_for_intellisearch", out))
        out = gclip.analyze_clip_for_slate(clip_id, marker_color="Blue")
        results.append(report(
            "media_pool_item.analyze_for_slate",
            "error" not in out,
            f"got {out!r} (False is expected — no slate in synthetic clip)",
        ))

        # ─── 2.9 transcribe_audio(use_speaker_detection=...) passthrough ───
        # These are the by-name granular tools that gained the Resolve 21+
        # useSpeakerDetection arg (transcribe_folder_audio / transcribe_audio);
        # the Dict-returning folder_transcribe_audio/transcribe_clip_audio
        # siblings intentionally don't take this param.
        out = gfolder.transcribe_folder_audio(folder_name="Master", use_speaker_detection=True)
        results.append(report(
            "folder.transcribe_folder_audio(use_speaker_detection=True)",
            isinstance(out, str) and out.startswith("Successfully"),
            f"got {out!r}",
        ))
        out = gclip.transcribe_audio(clip_name=clip.GetName(), use_speaker_detection=True)
        results.append(report(
            "media_pool_item.transcribe_audio(use_speaker_detection=True)",
            isinstance(out, str) and out.startswith("Successfully"),
            f"got {out!r}",
        ))

        # ─── 2.10 set_super_scale (project) — plain + enhanced ───
        out = gproj.set_super_scale(mode=3)
        results.append(report(
            "project.set_super_scale(mode=3)",
            out.get("success") is True,
            f"got {out!r}",
        ))
        readback = proj.GetSetting("superScale")
        results.append(report(
            "project.set_super_scale readback via GetSetting('superScale')",
            str(readback) == "3",
            f"readback={readback!r}",
        ))
        out = gproj.set_super_scale(mode=2, sharpness=0.5, noise_reduction=0.3)
        results.append(report(
            "project.set_super_scale(mode=2, enhanced)",
            out.get("success") is True and out.get("enhanced") is True,
            f"got {out!r}",
        ))

        # ─── 2.10 set_clip_super_scale (media_pool_item) — plain + enhanced ───
        out = gclip.set_clip_super_scale(clip_id, mode=2)
        results.append(report(
            "media_pool_item.set_clip_super_scale(mode=2)",
            out.get("success") is True,
            f"got {out!r}",
        ))
        readback = clip.GetClipProperty("Super Scale")
        results.append(report(
            "media_pool_item.set_clip_super_scale readback via GetClipProperty",
            str(readback) == "2",
            f"readback={readback!r}",
        ))
        out = gclip.set_clip_super_scale(clip_id, mode=2, sharpness=0.4, noise_reduction=0.2)
        results.append(report(
            "media_pool_item.set_clip_super_scale(mode=2, enhanced)",
            out.get("success") is True and out.get("enhanced") is True,
            f"got {out!r}",
        ))

        # ─── 2.11 render_with_quick_export enable_upload dict-forwarding (no real render) ───
        out = gproj.render_with_quick_export(
            preset_name="__stage2_nonexistent_preset__",
            target_dir=work_dir,
            custom_name="stage2_test",
            enable_upload=False,
        )
        results.append(report(
            "project.render_with_quick_export (enable_upload param accepted, no real render)",
            isinstance(out, dict) and "preset_name" in out,
            f"got {out!r}",
        ))

        print()
        print("=" * 70)
        passed = sum(1 for x in results if x)
        total = len(results)
        print(f"Stage 2 safe-battery live validation: {passed}/{total} passed")
        print("=" * 70)
        return 0 if passed == total else 1

    finally:
        try:
            projects = pm.GetProjectListInCurrentFolder() or []
            other = next((p for p in projects if p != project_name), None)
            if other:
                pm.LoadProject(other)
            pm.DeleteProject(project_name)
            print(f"Cleaned up disposable project: {project_name}")
        except Exception as exc:
            print(f"WARN: cleanup failed (delete '{project_name}' manually): {exc}")
        try:
            import shutil
            shutil.rmtree(work_dir)
            print(f"Cleaned up temp media: {work_dir}")
        except Exception as exc:
            print(f"WARN: temp media cleanup failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
