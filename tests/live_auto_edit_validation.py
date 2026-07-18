#!/usr/bin/env python3
"""Live end-to-end validation for the auto_edit pipeline (Phase 1 closer).

Requires DaVinci Resolve Studio running (Linux is the target platform for the
epic; macOS works too — speech synthesis falls back from `say` to ffmpeg sine
beeps when unavailable, which exercises the cue-level fallback path).

Creates a DISPOSABLE project with synthetic media (testsrc video + spoken
lines with deliberate fillers and a false start, plus a sine music track),
then drives the real MCP tool end-to-end:

  start_brief → analysis (transcription; vision optional) → plan_cut →
  approve_cut (confirm-token round trip, WITH music-bed consent) →
  build_timeline → finish (render to a temp dir)

Success criteria (issue #10): the rendered file exists, plays (ffprobe), and
matches the cut summary (duration within tolerance of the plan estimate).

Never touches source media destructively; the pilot project is deleted at the
end (best effort). The user's current project is restored.

Run: .venv/bin/python tests/live_auto_edit_validation.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PILOT = f"auto_edit_pilot_{time.strftime('%H%M%S')}"
MEDIA_DIR = tempfile.mkdtemp(prefix="drm-auto-edit-media-")
RENDER_DIR = tempfile.mkdtemp(prefix="drm-auto-edit-render-")

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _speech_audio(path: str, text: str) -> bool:
    """Spoken audio via `say` when present; caller falls back to beeps."""
    if shutil.which("say"):
        subprocess.run(["say", "-o", path, text], check=True)
        return True
    return False


def synth_talk_clip(name: str, *, text: str, duration: float) -> str:
    """testsrc video with speech (or beeps) — deliberate filler content."""
    out = os.path.join(MEDIA_DIR, f"{name}.mp4")
    aiff = os.path.join(MEDIA_DIR, f"{name}.aiff")
    if _speech_audio(aiff, text):
        audio_in = ["-i", aiff]
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
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-t", str(duration),
        out,
    ], check=True, capture_output=True)
    return out


def synth_broll_clip(name: str, *, duration: float) -> str:
    out = os.path.join(MEDIA_DIR, f"{name}.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"smptebars=duration={duration}:size=1280x720:rate=24",
        "-c:v", "libx264", "-preset", "ultrafast", "-an", out,
    ], check=True, capture_output=True)
    return out


def synth_music(name: str, *, duration: float) -> str:
    out = os.path.join(MEDIA_DIR, f"{name}.wav")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"sine=frequency=220:duration={duration}",
        "-af", "volume=0.4", out,
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


async def run_pipeline(s) -> int:
    talk = synth_talk_clip(
        "interview",
        text="Welcome to the pilot. Um, this is the, this is the main interview "
             "line. You know, it has fillers on purpose. The closing thought "
             "arrives after a long pause.",
        duration=30.0)
    broll = synth_broll_clip("broll", duration=15.0)
    music = synth_music("music", duration=45.0)

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
        # 1) start_brief
        started = await s.auto_edit("start_brief", {
            "files": [talk, broll],
            "music": music,
            "target_duration_seconds": 20,
            "title_text": "Auto Edit Pilot",
        })
        check("start_brief", bool(started.get("success")), str(started.get("error") or started.get("brief_id")))
        if not started.get("success"):
            return 1
        brief_id = started["brief_id"]

        # 2) wait for analysis (transcription is the load-bearing artifact)
        deadline = time.time() + 600
        while time.time() < deadline:
            status = await s.auto_edit("brief_status", {"brief_id": brief_id})
            state = (status.get("brief") or {}).get("state")
            if state in ("ready", "created"):
                break
            await asyncio.sleep(5)
        check("analysis reached ready", state in ("ready", "created"), f"state={state}")

        # 3) plan_cut
        planned = await s.auto_edit("plan_cut", {"brief_id": brief_id})
        check("plan_cut", bool(planned.get("success")), str(planned.get("error") or planned.get("plan_id")))
        if not planned.get("success"):
            return 1
        plan_id = planned["plan_id"]
        summary = planned["summary"]
        print("\n----- CHECKPOINT SUMMARY -----\n" + summary + "\n------------------------------\n")
        check("summary carries consent line",
              "Music-bed render consent" in summary, "")
        est_seconds = (planned["plan"].get("estimates") or {}).get("duration_seconds")

        # 4) approve_cut with music-bed consent (confirm-token round trip)
        gate = await s.auto_edit("approve_cut", {
            "plan_id": plan_id, "music_bed_consent": True})
        check("approve_cut issues token", gate.get("status") == "confirmation_required", "")
        approved = await s.auto_edit("approve_cut", {
            "plan_id": plan_id, "music_bed_consent": True,
            "confirm_token": gate.get("confirm_token")})
        check("approve_cut", bool(approved.get("success")), str(approved.get("error") or ""))

        # 5) build_timeline
        gate = await s.auto_edit("build_timeline", {"plan_id": plan_id})
        built = await s.auto_edit("build_timeline", {
            "plan_id": plan_id, "confirm_token": gate.get("confirm_token")})
        check("build_timeline", bool(built.get("success")),
              str(built.get("error") or built.get("timeline_name")))
        check("no build errors", not built.get("build_errors"), str(built.get("build_errors")))
        # Live-probe unknowns from the issue — record, don't fail Phase 1:
        print(f"  [note] title insertion: {built.get('title')}")
        print(f"  [note] punch-ins: {built.get('punch_ins')}")
        print(f"  [note] usage: {(built.get('readback') or {}).get('usage_summary')}")

        # 6) finish — render
        params = {"plan_id": plan_id,
                  "render": {"target_dir": RENDER_DIR, "custom_name": "pilot_cut"}}
        gate = await s.auto_edit("finish", params)
        finished = await s.auto_edit("finish", {
            **params, "confirm_token": gate.get("confirm_token")})
        render = finished.get("render") or {}
        output = render.get("output_path")
        check("finish render", bool(finished.get("success")), str(render.get("error") or output))
        exists = bool(output and os.path.isfile(output))
        check("output exists", exists, str(output))
        if exists:
            dur = ffprobe_duration(output)
            check("output plays (ffprobe)", dur is not None, f"duration={dur}")
            if dur is not None and est_seconds:
                # Title offset + codec padding allowed; the cut itself must dominate.
                check("duration matches cut summary",
                      abs(dur - float(est_seconds)) <= max(6.0, 0.2 * float(est_seconds)),
                      f"rendered={dur:.2f}s vs estimate={est_seconds}s")
        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        try:
            if previous_project:
                pm.LoadProject(previous_project)
            pm.DeleteProject(PILOT)
        except Exception as exc:
            print(f"cleanup: {type(exc).__name__}: {exc}")


def main() -> int:
    import src.server as s
    code = asyncio.run(run_pipeline(s))
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    shutil.rmtree(MEDIA_DIR, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
