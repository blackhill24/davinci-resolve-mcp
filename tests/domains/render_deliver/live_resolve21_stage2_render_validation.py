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

Both are on the destructive-confirm gate (#138/#139), so each is exercised as a
two-call dance and BOTH halves are assertions: the first call must refuse and
create nothing (that refusal is the whole point of the gate), the second call
must carry the token and actually run. Asserting only the second half would let
an ungated build pass; asserting only the first would never reach Resolve.
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


def report(name, ok, detail="", *, skipped=False):
    """PASS / FAIL / SKIP. SKIP means the feature is not installed on this box —
    an Extra that was never downloaded is not a defect in this repo, but it is
    also not a pass, so it is counted and printed on its own line rather than
    folded into either."""
    status = "SKIP" if skipped else ("PASS" if ok else "FAIL")
    line = f"  [{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return "skip" if skipped else bool(ok)


def is_unsupported(out):
    """True when Resolve declined the op for lack of the build/Extra rather than
    for lack of a token — the 21+ guard fires BEFORE the confirm gate, so this
    has to be told apart from a gate refusal or the refusal check reads as red on
    a box that simply lacks the feature."""
    err = str(out.get("error", "")) if isinstance(out, dict) else ""
    return "21+" in err or bool(out.get("unavailable"))


def confirm_dance(label, call, count_clips, results):
    """Run a gated op through both calls, appending an assertion for each half.

    `call` takes a confirm_token (None for the first call).
    """
    before = count_clips()
    first = call(None)
    if is_unsupported(first):
        results.append(report(f"{label} — confirm gate refuses first call", None,
                              f"🔬 not available on this box — {first.get('error')}", skipped=True))
        record(label, first, results)
        return

    token = first.get("confirm_token")
    refused = (first.get("status") == "confirmation_required" and bool(token)
               and count_clips() == before)
    results.append(report(f"{label} — confirm gate refuses first call", refused,
                          f"token={'yes' if token else 'no'}, "
                          f"status={first.get('status')!r}, "
                          f"clips {before}→{count_clips()} (must be unchanged)"))
    if not token:
        # No token means there is nothing to re-call with; running the op
        # unconfirmed would defeat the gate this harness exists to prove.
        results.append(report(label, False, f"no confirm_token issued: {first!r}"))
        return

    t0 = time.time()
    second = call(token)
    print(f"  confirmed call returned after {time.time() - t0:.1f}s: {second!r}")
    record(label, second, results)


def record(label, out, results):
    """Classify a confirmed call: ran / not available on this box / failed."""
    if out is None:  # confirm_dance already recorded why there was no second call
        return
    if out.get("success") is True:
        results.append(report(label, True, f"created {out.get('new')!r}"))
    elif is_unsupported(out):
        # The Extra / voice models are not installed, or the build predates the
        # method. Not a defect in this repo, and not a pass either.
        results.append(report(label, None, f"🔬 not available on this box — {out.get('error')}",
                              skipped=True))
    else:
        results.append(report(label, False, f"got {out!r}"))


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

        def count_clips():
            """Root-folder clip count — the observable both ops move when they run,
            and therefore the proof that the refused first call did nothing."""
            return len(mp.GetRootFolder().GetClipList() or [])

        # ─── 2.2 generate_speech (AI text-to-speech) ───
        print("Calling GenerateSpeech (AI text-to-speech)...")
        confirm_dance(
            "project.generate_speech",
            lambda tok: gproj.generate_speech(text_input="Stage two live validation test.",
                                              confirm_token=tok),
            count_clips, results,
        )

        # ─── 2.4/2.7 remove_motion_blur (GPU deblur render) ───
        print("Calling RemoveMotionBlur (GPU deblur render)...")
        confirm_dance(
            "media_pool_item.remove_clip_motion_blur",
            lambda tok: gclip.remove_clip_motion_blur(clip_id, deblur_option={}, confirm_token=tok),
            count_clips, results,
        )

        print()
        print("=" * 70)
        skipped = sum(1 for x in results if x == "skip")
        ran = [x for x in results if x != "skip"]
        passed = sum(1 for x in ran if x)
        summary = f"Stage 2 render-class live validation: {passed}/{len(ran)} passed"
        if skipped:
            summary += f", {skipped} skipped (feature not installed)"
        print(summary)
        print("=" * 70)
        return 0 if passed == len(ran) else 1

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
    from tests.preflight import gate
    gate("open")
    sys.exit(main())
