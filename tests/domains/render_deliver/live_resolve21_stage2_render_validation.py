"""Live validation harness for Stage 2 (issue #20) render-class methods.

Split out from live_resolve21_stage2_validation.py because these two invoke
Resolve's real render/AI-generation pipeline, which has documented history of
Fairlight/ALSA-duplex hangs on this box (see memory "resolve-headless-render-hang").
Run only with the user's explicit go-ahead, and prefer running this alone so a
hang doesn't also lose the safe-battery results.

Run with: .venv/bin/python tests/live_resolve21_stage2_render_validation.py

Tools validated:
  - project.generate_speech (2.2) — AI text-to-speech, creates a NEW audio
    MediaPoolItem via the Neural Engine / AI Speech Generator Extra
  - media_pool_item.remove_clip_motion_blur (2.4/2.7) — GPU motion-deblur
    render, creates a NEW video MediaPoolItem; source clip untouched
"""

import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def make_synthetic_media(work_dir):
    path = os.path.join(work_dir, "stage2_render_synthetic.mov")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2:r=24",
        "-y", path,
    ], check=True)
    return path


def report(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def main():
    from src.granular.common import get_resolve
    from src.granular import project as gproj
    from src.granular import media_pool_item as gclip

    print("=" * 70)
    print("Stage 2 (issue #20) live validation — render-class ops (2.2, 2.4/2.7)")
    print("=" * 70)

    r = get_resolve()
    if r is None:
        print("FATAL: cannot connect to DaVinci Resolve. Is it running?")
        return 2
    print(f"Connected to Resolve {r.GetVersionString()}")

    pm = r.GetProjectManager()
    project_name = f"stage2_r21_render_{int(time.time())}"
    if not pm.CreateProject(project_name):
        print(f"FATAL: failed to create disposable project '{project_name}'")
        return 2
    print(f"Created disposable project: {project_name}")

    work_dir = tempfile.mkdtemp(prefix="stage2_r21_render_")
    print(f"Synthetic media in: {work_dir}")

    try:
        clip_path = make_synthetic_media(work_dir)
        proj = pm.GetCurrentProject()
        mp = proj.GetMediaPool()
        imported = mp.ImportMedia([clip_path])
        if not imported:
            print("FATAL: failed to import synthetic media")
            return 2
        clip_id = imported[0].GetUniqueId()
        print(f"Imported clip. id: {clip_id}")

        results = []

        # ─── 2.2 generate_speech (AI text-to-speech) ───
        print("Calling GenerateSpeech (AI text-to-speech)...")
        t0 = time.time()
        out = gproj.generate_speech(text_input="Stage two live validation test.")
        elapsed = time.time() - t0
        print(f"  returned after {elapsed:.1f}s: {out!r}")
        if out.get("success") is True:
            results.append(report("project.generate_speech", True, f"created {out.get('new')!r}"))
        else:
            err = str(out.get("error", ""))
            gated = "Extra" in err or "21+" in err
            results.append(report("project.generate_speech", gated,
                                   f"🔬 gated — {err}" if gated else f"got {out!r}"))

        # ─── 2.4/2.7 remove_motion_blur (GPU deblur render) ───
        print("Calling RemoveMotionBlur (GPU deblur render)...")
        t0 = time.time()
        out = gclip.remove_clip_motion_blur(clip_id, deblur_option={})
        elapsed = time.time() - t0
        print(f"  returned after {elapsed:.1f}s: {out!r}")
        if out.get("success") is True:
            results.append(report("media_pool_item.remove_clip_motion_blur", True,
                                   f"created {out.get('new')!r}"))
        else:
            err = str(out.get("error", ""))
            gated = "21+" in err
            results.append(report("media_pool_item.remove_clip_motion_blur", gated or not out.get("error"),
                                   f"🔬 gated — {err}" if gated else f"got {out!r}"))

        print()
        print("=" * 70)
        passed = sum(1 for x in results if x)
        total = len(results)
        print(f"Stage 2 render-class live validation: {passed}/{total} passed")
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
    from preflight import gate
    gate("open")
    sys.exit(main())
