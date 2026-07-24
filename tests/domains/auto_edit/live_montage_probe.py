#!/usr/bin/env python3
"""Live end-to-end validation for the montage genre (epic #38 P3 closer).

Requires DaVinci Resolve Studio running. Creates a DISPOSABLE project with
synthetic B-roll (DNxHR-in-.mov, same recipe as live_orchestrate_probe.py)
and a REAL click-track music file (exact fixture proven in
test_music_analysis.py / test_montage_edit.py — a real ffmpeg-decoded
beat grid, not a mocked one), then drives the real `orchestrate` tool
end-to-end for a montage job:

  start_job(genre=montage) -> run_stage(ingest) -> run_stage(analysis)
  [fused with montage's plan pipeline] -> approve_gate(G1) [adopts
  auto_edit.approve_cut] -> run_stage(edit) [build_timeline, montage roles]
  -> run_stage(conform) -> run_stage(grade) [no-op] -> approve_gate(G2) ->
  run_stage(audio) [no-op] -> approve_gate(G3) -> run_stage(deliver)
  [render + verify] -> run_stage(review) -> finish_job.

Known gap: editorial classification (select_potential/pacing per shot)
normally comes from an LLM vision pass, which needs an interactive host to
fulfill commit_vision — the same limitation live_orchestrate_probe.py's
talking-head run documents for vision generally (it relies on real
TRANSCRIPTION instead, for the same reason). This probe seeds those fields
directly into the analysis DB, matching the exact pattern
tests/test_montage_edit.py's offline suite already uses — the analysis DB
doesn't care whether a field came from real vision inference or a seeded
fixture; what this probe verifies LIVE is everything downstream of that
data: real beat detection, real Resolve ingest/timeline build/render.

Two real interactions this probe surfaced (not obvious from the design or
the offline suite, only from actually running it):
  - `start_brief` always kicks a real media_analysis batch job regardless
    of genre; that pass re-ingests each clip and WIPES the seeded editorial
    data before montage's plan_cut ever reads it (ingest_report rebuilds
    shots from scratch per clip, no merge with a prior seed). Fix: seed
    AFTER `run_stage(ingest)`, let the first `run_stage(analysis)` fail
    honestly, reseed, then retry — the real batch won't re-run once
    terminal, so the retry's plan_cut sees the fresh seed.
  - `resolve_clip_id` must be a REAL Resolve media-pool unique ID (matched
    by file path after ingest), not a placeholder string — montage_edit
    carries it straight into the CutList, and build_timeline needs a real
    ID to find the clip.

Never touches source media destructively; the pilot project is deleted at
the end (best effort, and robust to an unsaved-default previous project —
see live_orchestrate_probe.py's cleanup fix). The user's current project
is restored.

Run: .venv/bin/python tests/live_montage_probe.py
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

PILOT = f"montage_pilot_{time.strftime('%H%M%S')}"
from tests.render_scratch import cleanup_render_dir, make_render_dir

MEDIA_DIR = tempfile.mkdtemp(prefix="drm-montage-media-")
RENDER_DIR = make_render_dir("drm-montage-render-")

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


_DNXHR = ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"]


def synth_broll_clip(name: str, *, pattern: str, duration: float) -> str:
    out = os.path.join(MEDIA_DIR, f"{name}.mov")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"{pattern}=duration={duration}:size=1280x720:rate=24",
        *_DNXHR, "-an", out,
    ], check=True, capture_output=True)
    return out


def click_track(path: str, *, bpm: float = 120.0, clicks: int = 24, sample_rate: int = 22050) -> None:
    """Same exact metronome fixture as test_music_analysis.py's click-track
    test and test_montage_edit.py's real-beats test — proven to detect
    cleanly through the real (ffmpeg-backed) music_analysis.detect_beats."""
    interval = 60.0 / bpm
    total = int(sample_rate * (interval * clicks + 1.0))
    click_len = int(0.02 * sample_rate)
    buf = bytearray(total * 2)
    for beat in range(clicks):
        start = int(beat * interval * sample_rate)
        for i in range(click_len):
            idx = start + i
            if idx >= total:
                break
            env = 1.0 - i / click_len
            val = int(0.7 * env * math.sin(2 * math.pi * 880 * i / sample_rate) * 32767)
            struct.pack_into("<h", buf, idx * 2, val)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(buf))


def _shot(idx, start, end, *, select_potential, pacing, description):
    return {
        "shot_index": idx, "time_seconds_start": start, "time_seconds_end": end,
        "frame_indices_used": [idx], "description": description, "qc_flags": [],
        "editorial": {
            "editorial_role": "montage_element", "select_potential": select_potential,
            "best_moment_present": False, "best_moment": None, "pacing": pacing,
            "stillness_type": None, "pacing_note": None,
        },
    }


def _visual_report(shots, *, clip_select_potential):
    return {
        "success": True, "clip_summary": "Montage pilot B-roll.", "clip_summary_oneliner": "B-roll.",
        "editorial_classification": {
            "primary_use": "montage", "select_potential": clip_select_potential,
            "energy_arc": "varied", "style": "documentary", "genre_indicators": [], "reason": "",
        },
        "content": {"locations": [], "actions": []},
        "shot_and_style": {"shot_sizes": ["medium"], "camera_motion": ["static"]},
        "slate": {"slate_visible": False},
        "editing_notes": {"best_moments": [], "search_tags": []},
        "shot_descriptions": shots,
    }


def seed_analysis(project_root: str, *, clip_id: str, name: str, path: str, shots) -> None:
    from src.domains.media_analysis.utils import analysis_store
    from tests.domains.media_analysis.test_analysis_store import make_report
    report = make_report(visual=_visual_report(shots, clip_select_potential="medium"))
    report["clip"] = dict(report["clip"], clip_id=clip_id, clip_name=name,
                          file_path=path, media_id=clip_id + "-m", fps=24.0)
    report["transcription"] = {"success": False, "segments": []}
    result = analysis_store.ingest_report(project_root, report, clip_dir=clip_id + "-dir")
    if not result.get("success"):
        raise RuntimeError(f"seed_analysis failed for {name}: {result}")


async def _confirm_round_trip(s, action: str, params: dict) -> dict:
    gate = await s.orchestrate(action, params)
    if gate.get("status") != "confirmation_required":
        return gate
    return await s.orchestrate(action, {**params, "confirm_token": gate["confirm_token"]})


def _find_media_pool_clip_id(proj, path: str):
    """Real Resolve unique ID for an imported clip, matched by File Path —
    montage_edit's build_cut_list_for_brief carries whatever resolve_clip_id
    the analysis DB has straight into the CutList, and build_timeline needs
    a REAL media-pool ID to find the clip, not a placeholder string."""
    mp = proj.GetMediaPool()
    root = mp.GetRootFolder()
    target = os.path.basename(path)
    stack = [root]
    while stack:
        folder = stack.pop()
        for clip in (folder.GetClipList() or []):
            try:
                clip_path = str(clip.GetClipProperty("File Path") or "")
            except Exception:
                clip_path = ""
            if os.path.basename(clip_path) == target:
                return clip.GetUniqueId()
        stack.extend(folder.GetSubFolderList() or [])
    return None


async def run_pipeline(s) -> int:
    broll_a = synth_broll_clip("broll_a", pattern="testsrc", duration=9.0)
    broll_b = synth_broll_clip("broll_b", pattern="smptebars", duration=9.0)
    broll_c = synth_broll_clip("broll_c", pattern="rgbtestsrc", duration=9.0)
    music_path = os.path.join(MEDIA_DIR, "click.wav")
    click_track(music_path, bpm=120.0, clicks=24)  # 12s track, beat every 0.5s

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
        # Resolve this pilot's analysis root BEFORE start_job, so we can seed
        # editorial classification into the same DB start_job will use.
        root_info = await s.media_analysis("resolve_output_root", {"create": True})
        project_root = root_info.get("project_root")
        check("resolved analysis root", bool(project_root), str(project_root))
        if not project_root:
            return 1

        # 1) start_job
        started = await s.orchestrate("start_job", {
            "files": [broll_a, broll_b, broll_c], "music": music_path,
            "genre": "montage", "title_text": "Montage Pilot",
        })
        check("start_job", bool(started.get("success")), str(started.get("error") or started.get("job_id")))
        if not started.get("success"):
            return 1
        job_id = started["job_id"]

        # 2) ingest — must run BEFORE seeding: montage_edit carries whatever
        # resolve_clip_id the analysis DB has straight into the CutList, and
        # build_timeline needs the REAL media-pool unique ID Resolve assigns
        # on import, not a placeholder string.
        ingest = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "ingest"})
        check("run_stage(ingest)", bool(ingest.get("success")), str(ingest.get("error") or ""))
        clip_id_a = _find_media_pool_clip_id(proj, broll_a)
        clip_id_b = _find_media_pool_clip_id(proj, broll_b)
        clip_id_c = _find_media_pool_clip_id(proj, broll_c)
        check("resolved real media-pool clip IDs",
              bool(clip_id_a and clip_id_b and clip_id_c),
              f"a={clip_id_a} b={clip_id_b} c={clip_id_c}")

        def _seed():
            seed_analysis(project_root, clip_id=clip_id_a, name="broll_a.mov", path=broll_a, shots=[
                _shot(1, 0.0, 4.0, select_potential="high", pacing="kinetic", description="Fast motion."),
                _shot(2, 4.0, 9.0, select_potential="medium", pacing="moderate", description="Steady pan."),
            ])
            seed_analysis(project_root, clip_id=clip_id_b, name="broll_b.mov", path=broll_b, shots=[
                _shot(1, 0.0, 4.0, select_potential="high", pacing="still", description="Calm wide shot."),
                _shot(2, 4.0, 9.0, select_potential="medium", pacing="kinetic", description="Quick action."),
            ])
            seed_analysis(project_root, clip_id=clip_id_c, name="broll_c.mov", path=broll_c, shots=[
                _shot(1, 0.0, 4.0, select_potential="high", pacing="kinetic", description="Peak energy moment."),
                _shot(2, 4.0, 9.0, select_potential="low", pacing="variable", description="Filler coverage."),
            ])

        _seed()

        # 3) analysis — fused with montage's own plan pipeline. REAL
        # interaction discovered running this probe: start_brief ALWAYS
        # kicks a real media_analysis batch job (transcription-oriented,
        # vision disabled by default — vision needs an interactive host,
        # same limitation this probe's docstring already flags). That real
        # (vision-less) pass re-ingests each clip and WIPES the seeded
        # editorial classification before montage's plan_cut ever reads it
        # (ingest_report rebuilds shots from scratch per clip, no merge).
        # Reseed once the real batch has run its course, then retry — the
        # batch won't re-run (brief_status only re-pumps while non-terminal).
        deadline = time.time() + 120
        analysis_out: dict = {}
        reseeded = False
        while time.time() < deadline:
            analysis_out = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "analysis"})
            if "error" in analysis_out:
                if not reseeded:
                    check("real analysis pass overwrote seeded editorial data (expected)",
                          True, str(analysis_out.get("error")))
                    _seed()
                    reseeded = True
                    continue
                break
            if not analysis_out.get("waiting_on"):
                break
            await asyncio.sleep(3)
        check("run_stage(analysis) completes",
              "error" not in analysis_out and not analysis_out.get("waiting_on"),
              str(analysis_out.get("error") or analysis_out.get("brief_state") or ""))
        if "error" in analysis_out:
            return 1

        # 4) G1 — adopts auto_edit.approve_cut verbatim
        g1 = await _confirm_round_trip(s, "approve_gate", {"job_id": job_id, "gate": "G1"})
        check("approve_gate(G1)", bool(g1.get("success")), str(g1.get("error") or ""))

        # 5) edit — build_timeline (montage roles on V1)
        edit_out = await _confirm_round_trip(s, "run_stage", {"job_id": job_id, "stage": "edit"})
        check("run_stage(edit)", bool(edit_out.get("success")), str(edit_out.get("error") or ""))
        check("no build errors", not edit_out.get("build_errors"), str(edit_out.get("build_errors")))

        # 6) conform
        conform = await s.orchestrate("run_stage", {
            "job_id": job_id, "stage": "conform", "accept_gaps": True, "accept_missing": True})
        check("run_stage(conform)", bool(conform.get("success")), str(conform.get("error") or ""))

        # 7) grade (no-op — montage has no grade specified)
        grade = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "grade"})
        check("run_stage(grade) no-op", bool(grade.get("success")), str(grade.get("error") or ""))

        # 8) G2 — mandatory vision handoff
        frame_path = os.path.join(MEDIA_DIR, "frame.jpg")
        subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", broll_a, "-frames:v", "1", frame_path],
                       check=True, capture_output=True)
        g2 = await _confirm_round_trip(s, "approve_gate", {
            "job_id": job_id, "gate": "G2",
            "vision_assessment": "Synthetic testsrc/smptebars/rgbtestsrc patterns; no real grade "
                                  "applied — probe frame only, checking the mechanism not the look.",
            "preview_frame_path": frame_path,
        })
        check("approve_gate(G2)", bool(g2.get("success")), str(g2.get("error") or ""))

        # 9) audio (no-op)
        audio = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "audio"})
        check("run_stage(audio) no-op", bool(audio.get("success")), str(audio.get("error") or ""))

        # 10) G3 + deliver
        g3 = await _confirm_round_trip(s, "approve_gate", {"job_id": job_id, "gate": "G3"})
        check("approve_gate(G3)", bool(g3.get("success")), str(g3.get("error") or ""))
        deliver = await s.orchestrate("run_stage", {
            "job_id": job_id, "stage": "deliver",
            "render": {"target_dir": RENDER_DIR, "custom_name": "montage_pilot_cut"},
        })
        output_path = (deliver.get("render") or {}).get("output_path")
        check("run_stage(deliver)", bool(deliver.get("success")), str(deliver.get("error") or output_path))
        exists = bool(output_path and os.path.isfile(output_path))
        check("output exists", exists, str(output_path))

        # 11) review + finish_job
        review = await s.orchestrate("run_stage", {"job_id": job_id, "stage": "review"})
        check("run_stage(review)", bool(review.get("success")), str(review.get("error") or ""))
        finished = await s.orchestrate("finish_job", {"job_id": job_id})
        check("finish_job", bool(finished.get("success")), str(finished.get("error") or ""))
        check("finish_job verifies output", bool(finished.get("output_verified")),
              str(finished.get("output_path")))

        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        try:
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
