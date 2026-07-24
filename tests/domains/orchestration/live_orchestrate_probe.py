#!/usr/bin/env python3
"""Live end-to-end validation for the orchestrate conductor (Phase 4 closer).

Requires DaVinci Resolve Studio running. Creates a DISPOSABLE project with
synthetic media (same DNxHR-in-.mov recipe as live_auto_edit_validation.py —
Linux Resolve cannot decode libx264/AAC), then drives the real `orchestrate`
tool end-to-end for a talking-head job:

  start_job -> run_stage(ingest) -> run_stage(analysis) [fused with the edit
  stage's brief pipeline] -> approve_gate(G1) [adopts auto_edit.approve_cut]
  -> run_stage(edit) [build_timeline] -> run_stage(conform) -> a forced
  grade FAILURE + rollback_stage (validates the reversible-stage model) ->
  run_stage(grade) [no-op retry] -> run_stage(audio) [no-op] ->
  approve_gate(G2) [real look at an extracted frame] -> approve_gate(G3) ->
  run_stage(deliver) [render + verify output] -> run_stage(review) ->
  finish_job [verify output + purge].

Known gap: the bring-your-own-timeline (non-talking-head) path is only
offline-tested so far (tests/test_orchestrate_run_stage_tool.py) — it would
need a second disposable project and a manually-cut timeline to validate
live, deferred to keep this probe's runtime bounded.

Never touches source media destructively; the pilot project is deleted at
the end (best effort). The user's current project is restored.

Run: .venv/bin/python tests/live_orchestrate_probe.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

PILOT = f"orchestrate_pilot_{time.strftime('%H%M%S')}"
from tests.render_scratch import cleanup_render_dir, make_render_dir

MEDIA_DIR = tempfile.mkdtemp(prefix="drm-orchestrate-media-")
RENDER_DIR = make_render_dir("drm-orchestrate-render-")

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# Same DNxHR recipe as live_auto_edit_validation.py: Linux Resolve cannot
# decode libx264/AAC, so synth media must be pro intermediates.
_DNXHR = ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"]


def _piper_voice() -> str | None:
    import glob
    env = os.environ.get("PIPER_VOICE")
    if env and os.path.isfile(env):
        return env
    candidates = sorted(glob.glob(os.path.expanduser("~/.cache/piper/*.onnx")))
    return candidates[0] if candidates else None


def _speech_audio(base: str, text: str) -> str | None:
    """Spoken audio via `say`, then piper-tts — plan_cut needs REAL
    transcribable speech; a sine tone produces no words to transcribe and
    plan_cut correctly (refuse-not-fabricate) refuses with no speech source."""
    if shutil.which("say"):
        out = base + ".aiff"
        subprocess.run(["say", "-o", out, text], check=True)
        return out
    voice = _piper_voice()
    if voice:
        out = base + ".wav"
        proc = subprocess.run(
            [sys.executable, "-m", "piper", "-m", voice, "-f", out],
            input=text, text=True, capture_output=True)
        if proc.returncode == 0 and os.path.isfile(out):
            return out
    return None


def synth_talk_clip(name: str, *, text: str, duration: float) -> str:
    out = os.path.join(MEDIA_DIR, f"{name}.mov")
    speech = _speech_audio(os.path.join(MEDIA_DIR, name), text)
    if speech:
        audio_in = ["-i", speech]
        afilter = "[1:a]apad[a]"
    else:
        audio_in = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
        afilter = "[1:a]anull[a]"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=1280x720:rate=24",
        *audio_in,
        "-filter_complex", afilter,
        "-map", "0:v", "-map", "[a]",
        *_DNXHR, "-c:a", "pcm_s16le", "-t", str(duration),
        out,
    ], check=True, capture_output=True)
    return out


def synth_broll_clip(name: str, *, duration: float) -> str:
    out = os.path.join(MEDIA_DIR, f"{name}.mov")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"smptebars=duration={duration}:size=1280x720:rate=24",
        *_DNXHR, "-an", out,
    ], check=True, capture_output=True)
    return out


def synth_music(name: str, *, duration: float) -> str:
    out = os.path.join(MEDIA_DIR, f"{name}.wav")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}",
        "-af", "volume=0.4", out,
    ], check=True, capture_output=True)
    return out


def extract_frame(video_path: str, *, at_seconds: float = 1.0) -> str:
    out = os.path.join(MEDIA_DIR, "frame.jpg")
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(at_seconds), "-i", video_path, "-frames:v", "1", out,
    ], check=True, capture_output=True)
    return out


def ffprobe_duration(path: str) -> float | None:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True)
    try:
        return float(proc.stdout.strip())
    except (TypeError, ValueError):
        return None


async def _confirm_round_trip(s, action: str, params: dict) -> dict:
    """Every gate/build/render step here is confirm-token gated: first call
    mints a token, second call (with it) executes. Mirrors the exact
    two-call pattern the offline tool tests exercise."""
    gate = await s.orchestrate(action, params)
    if gate.get("status") != "confirmation_required":
        return gate
    return await s.orchestrate(action, {**params, "confirm_token": gate["confirm_token"]})


async def run_pipeline(s) -> int:
    talk = synth_talk_clip(
        "interview",
        text="Welcome to the pilot. Um, this is the, this is the main interview "
             "line. You know, it has fillers on purpose. The closing thought "
             "arrives after a long pause.",
        duration=20.0)
    broll = synth_broll_clip("broll", duration=10.0)
    music = synth_music("music", duration=25.0)
    frame = extract_frame(talk)

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — aborting")
        return 2
    pm = r.GetProjectManager()
    previous_project = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None
    proj = pm.CreateProject(PILOT)
    check("disposable project created", proj is not None, PILOT)
    if proj is None:
        return 2

    try:
        # 1) start_job
        started = await s.orchestrate("start_job", {
            "files": [talk, broll], "music": music,
            "target_duration_seconds": 15, "title_text": "Orchestrate Pilot",
        })
        check("start_job", bool(started.get("success")), str(started.get("error") or started.get("job_id")))
        if not started.get("success"):
            return 1
        job_id = started["job_id"]

        # 2) ingest
        ingest = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "ingest"})
        check("run_stage(ingest)", bool(ingest.get("success")), str(ingest.get("error") or ""))

        # 3) analysis — fused with the edit stage's brief pipeline; poll
        # until it's no longer waiting (transcription is the load-bearing
        # artifact; bounded like live_auto_edit_validation.py's own poll).
        deadline = time.time() + 600
        analysis_out: dict = {}
        while time.time() < deadline:
            analysis_out = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "analysis"})
            if "error" in analysis_out or not analysis_out.get("waiting_on"):
                break
            await asyncio.sleep(5)
        check("run_stage(analysis) completes",
              "error" not in analysis_out and not analysis_out.get("waiting_on"),
              str(analysis_out.get("error") or analysis_out.get("brief_state") or ""))
        if "error" in analysis_out:
            return 1

        # 4) G1 — adopts auto_edit.approve_cut verbatim (confirm-token round trip)
        g1 = await _confirm_round_trip(s, "approve_gate", {"job_id": job_id, "gate": "G1"})
        check("approve_gate(G1)", bool(g1.get("success")), str(g1.get("error") or ""))

        # 5) edit — build_timeline is its own confirm-token gate, separate
        # from G1's approve_cut adoption.
        edit_out = await _confirm_round_trip(s, "run_stage", {"job_id": job_id, "stage": "edit"})
        check("run_stage(edit)", bool(edit_out.get("success")), str(edit_out.get("error") or ""))

        # 6) conform (accept incidental gaps — the focus here is delegation,
        # not editorial QC of synthetic content)
        conform = await s.orchestrate("run_stage", {
            "job_id": job_id, "stage": "conform", "accept_gaps": True, "accept_missing": True})
        check("run_stage(conform)", bool(conform.get("success")), str(conform.get("error") or ""))

        # 7) reversible-stage model: force a grade FAILURE (bad drx path),
        # confirm the snapshot survives it, roll back, retry clean.
        bad_grade = await s.orchestrate("run_stage", {
            "job_id": job_id, "stage": "grade", "grade": {"drx_path": "/nonexistent.drx"}})
        check("forced grade failure", not bad_grade.get("success"), str(bad_grade.get("error") or ""))
        status_after_fail = await s.orchestrate("job_status", {"job_id": job_id})
        failed_stage = (status_after_fail.get("job") or {}).get("stages", {}).get("grade", {})
        check("failed stage keeps its snapshot",
              bool(failed_stage.get("snapshot_ids")), str(failed_stage.get("snapshot_ids")))
        rolled_back = await s.orchestrate("rollback_stage", {"job_id": job_id, "stage": "grade"})
        check("rollback_stage", bool(rolled_back.get("success")), str(rolled_back.get("error") or ""))
        grade = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "grade"})
        check("run_stage(grade) clean retry (no-op)", bool(grade.get("success")), str(grade.get("error") or ""))

        # 8) G2 — mandatory vision handoff (a real extracted frame). Gates
        # every stage downstream of grade, so it must land before audio.
        g2 = await _confirm_round_trip(s, "approve_gate", {
            "job_id": job_id, "gate": "G2",
            "vision_assessment": "Synthetic testsrc color-bar pattern; no real grade "
                                  "applied — probe frame only, checking the mechanism not the look.",
            "preview_frame_path": frame,
        })
        check("approve_gate(G2)", bool(g2.get("success")), str(g2.get("error") or ""))

        # 9) audio (no-op — no options supplied)
        audio = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "audio"})
        check("run_stage(audio)", bool(audio.get("success")), str(audio.get("error") or ""))

        # 10) G3 + deliver — special-cased render, verified output
        g3 = await _confirm_round_trip(s, "approve_gate", {"job_id": job_id, "gate": "G3"})
        check("approve_gate(G3)", bool(g3.get("success")), str(g3.get("error") or ""))
        deliver = await s.orchestrate("run_stage", {
            "job_id": job_id, "stage": "deliver",
            "render": {"target_dir": RENDER_DIR, "custom_name": "orchestrate_pilot_cut"},
        })
        output_path = (deliver.get("render") or {}).get("output_path")
        check("run_stage(deliver)", bool(deliver.get("success")), str(deliver.get("error") or output_path))
        exists = bool(output_path and os.path.isfile(output_path))
        check("output exists", exists, str(output_path))
        if exists:
            dur = ffprobe_duration(output_path)
            check("output plays (ffprobe)", dur is not None, f"duration={dur}")

        # 11) review + finish_job (verify output + purge)
        review = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "review"})
        check("run_stage(review)", bool(review.get("success")), str(review.get("error") or ""))
        finished = await s.orchestrate("finish_job", {"job_id": job_id})
        check("finish_job", bool(finished.get("success")), str(finished.get("error") or ""))
        check("finish_job verifies output", bool(finished.get("output_verified")),
              str(finished.get("output_path")))

        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        try:
            # LoadProject(name) fails when `previous_project` was an unsaved
            # default (e.g. "Untitled Project") with no real database entry —
            # recreate it instead so the pilot doesn't get stuck as current
            # (Resolve refuses to delete the currently-loaded project).
            restored = bool(previous_project and pm.LoadProject(previous_project))
            if not restored:
                pm.CreateProject(previous_project or "Untitled Project")
            pm.DeleteProject(PILOT)
        except Exception as exc:
            print(f"cleanup: {type(exc).__name__}: {exc}")


def main() -> int:
    import src.server as s
    code = asyncio.run(run_pipeline(s))
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    shutil.rmtree(MEDIA_DIR, ignore_errors=True)
    cleanup_render_dir(RENDER_DIR)
    return code


if __name__ == "__main__":
    from tests.preflight import gate
    gate("idle")
    sys.exit(main())
