#!/usr/bin/env python3
"""Live full-pipeline acceptance for Tier-2 ducking (drt_automation, issue #14).

Drives the real MCP tool end-to-end with prefer_drt_ducking and proves the music
bed clip on the POLISHED timeline carries the authored duck level — no rendered
derivative, mode = drt_automation:

  start_brief -> analysis -> plan_cut -> approve_cut(prefer_drt_ducking=True) ->
  build_timeline -> polish_timeline (applies set_audio_level in the drt round-trip)
  -> export the (polished) timeline -> decode the A2 music clip's EffectFiltersBA
     and assert it equals the plan's bed gain_db.

No finish/render (not needed for the ducking acceptance). Reuses the media synth
and helpers from live_auto_edit_validation. Disposable project, restored + deleted
at the end. Requires DaVinci Resolve Studio running.

Run: .venv/bin/python tests/live_auto_edit_drt_ducking.py
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.live_auto_edit_validation as V  # reuse media synth + helpers  # noqa: E402

PILOT = f"duck_pipe_{time.strftime('%H%M%S')}"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def decode_audio_level(drt_path: str):
    """The dB level of the first audio clip carrying an EffectFiltersBA, or None."""
    z = zipfile.ZipFile(drt_path)
    for n in z.namelist():
        if n.startswith("SeqContainer") and n.endswith(".xml"):
            xml = z.read(n).decode("utf-8", "replace")
            for clip in re.findall(r"<Sm2TiAudioClip.*?</Sm2TiAudioClip>", xml, re.S):
                m = re.search(r"<EffectFiltersBA>([0-9a-f]+)</EffectFiltersBA>", clip)
                if m:
                    b = bytes.fromhex(m.group(1))
                    k = b.find(bytes.fromhex("0a0911"))
                    if k >= 0:
                        return struct.unpack("<d", b[k + 3:k + 11])[0]
    return None


async def run(s) -> int:
    talk = V.synth_talk_clip("interview", text=(
        "Welcome to the pilot. Um, this is the, this is the main interview line. "
        "You know, it has fillers on purpose. The closing thought arrives after a "
        "long pause."), duration=30.0)
    broll = V.synth_broll_clip("broll", duration=15.0)
    music = V.synth_music("music", duration=45.0)

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — aborting"); return 2
    pm = r.GetProjectManager()
    previous = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None
    proj = pm.CreateProject(PILOT)
    check("disposable project created", proj is not None, PILOT)
    if proj is None:
        return 2
    try:
        started = await s.auto_edit("start_brief", {
            "files": [talk, broll], "music": music,
            "target_duration_seconds": 20, "title_text": "Duck Pilot"})
        check("start_brief", bool(started.get("success")), str(started.get("error") or ""))
        if not started.get("success"):
            return 1
        brief_id = started["brief_id"]

        deadline = time.time() + 600
        state = None
        while time.time() < deadline:
            status = await s.auto_edit("brief_status", {"brief_id": brief_id})
            state = (status.get("brief") or {}).get("state")
            if state == "ready":
                break
            await asyncio.sleep(5)
        check("analysis ready", state == "ready", f"state={state}")
        if state != "ready":
            return 1

        planned = await s.auto_edit("plan_cut", {"brief_id": brief_id})
        check("plan_cut", bool(planned.get("success")), str(planned.get("error") or ""))
        if not planned.get("success"):
            return 1
        plan_id = planned["plan_id"]
        gain_db = (planned["plan"].get("music") or {}).get("gain_db")
        check("plan has music gain_db", isinstance(gain_db, (int, float)), f"gain_db={gain_db}")

        # approve_cut WITH prefer_drt_ducking (derivative-free; no consent).
        gate = await s.auto_edit("approve_cut", {"plan_id": plan_id, "prefer_drt_ducking": True})
        approved = await s.auto_edit("approve_cut", {
            "plan_id": plan_id, "prefer_drt_ducking": True,
            "confirm_token": gate.get("confirm_token")})
        check("approve_cut", bool(approved.get("success")), str(approved.get("error") or ""))
        check("ducking_mode == drt_automation",
              approved.get("ducking_mode") == "drt_automation", str(approved.get("ducking_mode")))

        gate = await s.auto_edit("build_timeline", {"plan_id": plan_id})
        built = await s.auto_edit("build_timeline", {
            "plan_id": plan_id, "confirm_token": gate.get("confirm_token")})
        check("build_timeline", bool(built.get("success")),
              str(built.get("error") or built.get("timeline_name")))
        if not built.get("success"):
            return 1

        popts = {"lower_thirds": [{"text": "Pilot Speaker", "at_segment": 0}]}
        gate = await s.auto_edit("polish_timeline", {"plan_id": plan_id, "options": popts})
        preview_ops = [o.get("op") for o in (gate.get("preview") or {}).get("ops", [])]
        check("polish preview includes set_audio_level",
              "set_audio_level" in preview_ops, str(preview_ops))
        polished = await s.auto_edit("polish_timeline", {
            "plan_id": plan_id, "options": popts, "confirm_token": gate.get("confirm_token")})
        check("polish_timeline", bool(polished.get("success")),
              str(polished.get("error") or polished.get("polished_timeline")))
        if not polished.get("success"):
            return 1
        check("polished clips stayed linked", bool(polished.get("clips_relinked")),
              str(polished.get("media_link")))

        # PROOF: export the polished timeline and decode the A2 music clip level.
        tl = proj.GetCurrentTimeline()
        out = os.path.join(tempfile.mkdtemp(prefix="duck_pipe_"), "polished.drt")
        exp = s._export_timeline_checked(tl, {
            "path": out, "format": "drt", "require_temp_path": False,
            "background": False, "async_job": False})
        level = decode_audio_level(exp.get("primary_file") or out) if exp.get("success") else None
        check("polished A2 music clip carries a ducked level", level is not None, f"level={level}")
        if level is not None and isinstance(gain_db, (int, float)):
            check("ducked level == plan bed gain", abs(level - float(gain_db)) < 1e-6,
                  f"drt={level} vs plan gain_db={gain_db}")
        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        try:
            if previous:
                pm.LoadProject(previous)
            pm.DeleteProject(PILOT)
        except Exception as exc:
            print(f"cleanup: {type(exc).__name__}: {exc}")


def main() -> int:
    import src.server as s
    code = asyncio.run(run(s))
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    shutil.rmtree(V.MEDIA_DIR, ignore_errors=True)
    return code


if __name__ == "__main__":
    from preflight import gate
    gate("open")
    sys.exit(main())
