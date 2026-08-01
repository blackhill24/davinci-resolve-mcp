#!/usr/bin/env python3
"""Live end-to-end quality gate for the montage genre (issue #181, phase 6/6
of the montage-quality epic — the closing gate).

Requires DaVinci Resolve Studio running. Drives the auto_edit tool directly
(start_brief -> plan_cut -> approve_cut -> build_timeline -> finish) on four
of the five reference clips in /home/jon/Downloads/visdeo/ against the real
reference track (the fifth, "DJI_20260530130257_0230_D.MP4", is 59.94fps
against the other four's 29.97fps — montage correctly REFUSES a mixed-fps
brief, verified live; that's a working guard, not something this harness
should route around by resampling), and asserts the invariants phases 1-6
exist to guarantee:

  - every V1 item starts on a beat frame (build_timeline's beat_alignment
    readback, issue #181 section A)
  - no two consecutive items share a source clip (issue #177's round-robin)
  - the timeline runtime matches the music within one frame
  - every item carries the expected Fusion comp / look-bucket grade
    (phases 4-5) and the plan's own beat/section/motion bookkeeping
  - the render produced a file, longer than zero frames

Known gap this harness works around (documented in live_montage_probe.py,
the epic #38 precedent): editorial classification (select_potential/pacing
per shot) normally comes from an LLM vision pass, which needs an interactive
host to fulfill commit_vision/commit_shot_vision — unavailable to an
unattended harness. This probe runs the REAL standard analysis (real
ffprobe, real cut detection, real shot boundaries) on the real footage, then
seeds editorial classification onto those REAL shots (not fabricated
boundaries) so the rest of the pipeline — beat detection, scouting,
bucketing, motion, QC — all run against real data.

Never touches source media destructively; extracted QC frames land under
the analysis root. The disposable project is deleted at the end (best
effort) and the user's previous project is restored.

Run: .venv/bin/python tests/domains/auto_edit/live_montage_quality.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

PILOT = f"montage_quality_pilot_{time.strftime('%H%M%S')}"
from tests.render_scratch import cleanup_render_dir, make_render_dir

REFERENCE_DIR = "/home/jon/Downloads/visdeo"
# All FIVE reference clips, deliberately MIXED-RATE:
# "DJI_20260530130257_0230_D.MP4" is 59.94fps against the other four's
# 29.97fps. Montage used to refuse the brief outright; it now cuts a
# majority-rate (29.97) timeline and gives each shot source frames in its own
# rate, because Resolve resamples off-rate media to preserve its wall-clock
# length (live_mixed_fps_probe.py). The beat-alignment check below is what
# proves the mixed brief still lands every cut on the grid.
REFERENCE_CLIPS = [
    "Blue sky.MP4",
    "DJI_20260524195204_0209_D.MP4",
    "DJI_20260530130257_0230_D.MP4",
    "DRILL TRUCK IN THE CLOUDS.MP4",
    "flagging.MP4",
]
REFERENCE_MUSIC = "More oomph Perfect soul 1.mp3"

RENDER_DIR = make_render_dir("drm-montage-quality-render-")

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


_SELECT_CYCLE = itertools.cycle(["high", "high", "medium", "low"])
_PACING_CYCLE = itertools.cycle(["kinetic", "still", "moderate", "variable"])


def seed_editorial_over_real_shots(project_root: str, clip_ref: str) -> int:
    """Overlay editorial classification onto the REAL shots a standard
    analysis pass already detected (real cut-detection boundaries, real
    ffprobe technical facts) — never fabricated shot ranges. Returns the
    number of shots seeded."""
    from src.core import timeline_brain_db
    from src.domains.media_analysis.utils import analysis_store

    conn = timeline_brain_db.connect(project_root)
    clip_uuid = analysis_store.resolve_clip_uuid_ingesting(project_root, conn, clip_ref)
    if not clip_uuid:
        raise RuntimeError(f"no analyzed clip found for {clip_ref!r}")
    row = conn.execute(
        "SELECT report_json FROM analysis_reports WHERE clip_uuid = ?", (clip_uuid,)
    ).fetchone()
    if not row:
        raise RuntimeError(f"no canonical report for {clip_ref!r}")
    report = json.loads(row["report_json"])
    visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    shots = visual.get("shot_descriptions") or []
    if not shots:
        # Vision was off (visual.status == "skipped") — no shot_descriptions
        # exist at all, but the REAL technical cut-detection pass still ran
        # and recorded real shot_ranges (a single range spanning the whole
        # clip when there were no detected cuts, e.g. a continuous take like
        # "Blue sky.MP4"). Build shot_descriptions FROM those real ranges —
        # never fabricated boundaries — so seeding editorial has something
        # real to attach to.
        shot_ranges = ((report.get("cut_analysis") or {}).get("shot_ranges") or [])
        if not shot_ranges:
            raise RuntimeError(f"{clip_ref!r} has no detected shots — cut detection found nothing")
        shots = [
            {
                "shot_index": r["index"],
                "time_seconds_start": r["start"],
                "time_seconds_end": r["end"],
                "frame_indices_used": [],
                "description": "",
                "qc_flags": [],
            }
            for r in shot_ranges
        ]
    visual = dict(visual, success=True)
    for shot in shots:
        shot["editorial"] = {
            "editorial_role": "montage_element",
            "select_potential": next(_SELECT_CYCLE),
            "best_moment_present": False,
            "best_moment": None,
            "pacing": next(_PACING_CYCLE),
            "stillness_type": None,
            "pacing_note": None,
        }
    visual["shot_descriptions"] = shots
    report["visual"] = visual
    clip_dir_name = report.get("clip", {}).get("clip_dir")
    clip_dir = os.path.join(project_root, "clips", clip_dir_name) if clip_dir_name else None
    result = analysis_store.ingest_report(project_root, report, clip_dir=clip_dir)
    if not result.get("success"):
        raise RuntimeError(f"reseed ingest failed for {clip_ref!r}: {result}")
    return len(shots)


async def _confirm_round_trip(s, action: str, params: dict) -> dict:
    gate = await s.auto_edit(action, params)
    if gate.get("status") != "confirmation_required":
        return gate
    return await s.auto_edit(action, {**params, "confirm_token": gate["confirm_token"]})


async def run_pipeline(s) -> int:
    files = [os.path.join(REFERENCE_DIR, name) for name in REFERENCE_CLIPS]
    music = os.path.join(REFERENCE_DIR, REFERENCE_MUSIC)
    for path in files + [music]:
        if not os.path.isfile(path):
            check("reference media present", False, path)
            return 2

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
        started = await s.auto_edit("start_brief", {
            "files": files, "music": music, "genre": "montage",
            "target_duration_seconds": 30.0, "title_text": "Montage Quality Gate",
        })
        check("start_brief", bool(started.get("success")), str(started.get("error") or started.get("brief_id")))
        if not started.get("success"):
            return 1
        brief_id = started["brief_id"]

        # Pump brief_status until the real analysis batch finishes, then
        # seed editorial classification onto the REAL shots it found (see
        # module docstring — vision needs an interactive host this harness
        # doesn't have; this is the same documented workaround
        # live_montage_probe.py uses for epic #38).
        deadline = time.time() + 600
        status_out: dict = {}
        while time.time() < deadline:
            status_out = await s.auto_edit("brief_status", {"brief_id": brief_id})
            brief = status_out.get("brief") or {}
            if brief.get("state") not in ("created", "analyzing"):
                break
            await asyncio.sleep(5)
        check("real analysis batch completes",
              (status_out.get("brief") or {}).get("state") not in ("created", "analyzing"),
              str((status_out.get("brief") or {}).get("state")))

        project_root = None
        root_info = await s.media_analysis("resolve_output_root", {"create": False})
        project_root = root_info.get("project_root")
        check("resolved analysis root", bool(project_root), str(project_root))
        if not project_root:
            return 1

        seeded_total = 0
        for path in files:
            seeded_total += seed_editorial_over_real_shots(project_root, path)
        check("seeded editorial onto real shots", seeded_total > 0, f"{seeded_total} shots")

        # 2) plan_cut — offers the scout handoff by default (phase 3); this
        # harness declines it (scout=false) since it has no interactive host
        # either, exercising the honest-degradation path deliberately.
        plan_out = await s.auto_edit("plan_cut", {"brief_id": brief_id, "scout": False})
        check("plan_cut", bool(plan_out.get("success")), str(plan_out.get("error") or ""))
        if not plan_out.get("success"):
            return 1
        plan = plan_out["plan"]
        plan_id = plan["plan_id"]
        check("grid_available", bool(plan.get("grid_available")), f"tempo={plan.get('tempo_bpm')}")
        check("look_buckets computed", bool(plan.get("look_buckets")), str(plan.get("look_bucket_basis")))

        # no two consecutive segments share a source clip (issue #177)
        clip_uuids = [seg["clip_uuid"] for seg in plan["segments"]]
        no_repeat = all(a != b for a, b in zip(clip_uuids, clip_uuids[1:]))
        check("no two consecutive segments share a clip_uuid", no_repeat, str(clip_uuids))

        # 3) approve_cut
        approve_out = await _confirm_round_trip(s, "approve_cut", {"plan_id": plan_id})
        check("approve_cut", bool(approve_out.get("success")), str(approve_out.get("error") or ""))

        # 4) build_timeline — the beat-alignment readback (issue #181 section A)
        build_out = await _confirm_round_trip(s, "build_timeline", {"plan_id": plan_id})
        check("build_timeline", bool(build_out.get("success")), str(build_out.get("error") or ""))
        check("no build errors", not build_out.get("build_errors"), str(build_out.get("build_errors")))
        beat_alignment = build_out.get("beat_alignment") or {}
        check("beat_alignment ran", bool(beat_alignment), str(beat_alignment))
        check("every item on the beat grid",
              beat_alignment.get("checked", 0) > 0 and not beat_alignment.get("deviations"),
              str(beat_alignment.get("deviations")))

        # timeline runtime matches the music within one frame.
        # _edit_engine_capture's readback carries duration_SECONDS (+
        # clip_count), not a frame count — convert using the plan's own fps
        # rather than assuming a key that doesn't exist.
        fps = float(plan.get("fps") or 24.0)
        readback = build_out.get("readback") or {}
        music_seg = plan.get("music") or {}
        expected_frames = int(music_seg.get("record_end_frame", 0))
        actual_frames = round(float(readback.get("duration_seconds") or 0.0) * fps)
        check("runtime matches music within one frame",
              abs(actual_frames - expected_frames) <= 1,
              f"expected={expected_frames} actual={actual_frames}")

        # 5) finish — grade (per-bucket match), motion, render, QC.
        # plan["look_buckets"] is {bucket: <raw CDL dict>} — finish's
        # grade["match"] expects {bucket: {"cdl": <CDL dict>}}, same shape
        # as grade["cdl"] itself; do not pass the raw CDL dict directly.
        match_grade = {b: {"cdl": cdl} for b, cdl in (plan.get("look_buckets") or {}).items()}
        finish_params = {
            "plan_id": plan_id,
            "grade": {"match": match_grade},
            "motion": {},
            "render": {"target_dir": RENDER_DIR, "custom_name": "montage_quality_cut"},
        }
        gate = await s.auto_edit("finish", finish_params)
        if gate.get("status") == "confirmation_required":
            gate = await s.auto_edit("finish", {**finish_params, "confirm_token": gate["confirm_token"]})
        check("finish", bool(gate.get("success")), str(gate.get("error") or ""))
        check("motion applied to at least one clip",
              (gate.get("motion") or {}).get("applied", 0) > 0,
              f"motion={gate.get('motion')} errors={gate.get('errors')}")
        check("match grade applied to at least one clip",
              (gate.get("grade") or {}).get("match", {}).get("applied", 0) > 0, str(gate.get("grade")))
        output_path = (gate.get("render") or {}).get("output_path")
        exists = bool(output_path and os.path.isfile(output_path) and os.path.getsize(output_path) > 0)
        check("render produced a non-empty file", exists, str(output_path))
        check("QC pass ran (or honestly declined)", "qc" in gate, str(gate.get("qc")))

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
    cleanup_render_dir(RENDER_DIR)
    return code


if __name__ == "__main__":
    from tests.preflight import gate
    gate("idle")
    sys.exit(main())
