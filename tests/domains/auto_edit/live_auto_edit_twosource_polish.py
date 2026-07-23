#!/usr/bin/env python3
"""Live TWO-SOURCE polish demo — closes issue #13's cross-dissolve acceptance inch.

The Phase-1 live pilot was single-source (no source-change cut), so polish_timeline
emitted a lower-third but no cross-dissolve. This drives the real auto_edit tool on
a cut assembled from TWO distinct source clips, so plan_cut yields a source-change
boundary, then asserts the polished (reimported) timeline carries a cross-dissolve
AND a lower-third with source clips still linked.

No render (export → drt-surgery → reimport only), so no ALSA/render-hang risk.
Disposable project; the prior project is restored. Reuses the Phase-1 validation's
synth+speech helpers.

Run: .venv/bin/python tests/live_auto_edit_twosource_polish.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from tests.domains.auto_edit.live_auto_edit_validation import synth_talk_clip, MEDIA_DIR  # noqa: E402

PILOT = f"twosrc_polish_{time.strftime('%H%M%S')}"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def run(s) -> int:
    # Two clips, distinct speech → distinct media pool items. 18s each with a 24s
    # target forces the plan to draw from BOTH, guaranteeing a source-change cut.
    talk1 = synth_talk_clip(
        "speakerA",
        text="Hello and welcome to the show. This is the first speaker, um, talking "
             "about the opening topic in a clear and complete sentence.",
        duration=18.0)
    talk2 = synth_talk_clip(
        "speakerB",
        text="And now, you know, the second source entirely. A different clip with "
             "its own distinct closing remarks to wrap everything up.",
        duration=18.0)

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — aborting")
        return 2
    pm = r.GetProjectManager()
    previous = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None
    proj = pm.CreateProject(PILOT)
    check("disposable project created", proj is not None, PILOT)
    if proj is None:
        return 2

    try:
        started = await s.auto_edit("start_brief", {
            "files": [talk1, talk2],
            "target_duration_seconds": 24,
            "title_text": "Two Source Demo",
        })
        check("start_brief", bool(started.get("success")),
              str(started.get("error") or started.get("brief_id")))
        if not started.get("success"):
            return 1
        brief_id = started["brief_id"]

        deadline = time.time() + 900
        state = None
        while time.time() < deadline:
            st = await s.auto_edit("brief_status", {"brief_id": brief_id})
            state = (st.get("brief") or {}).get("state")
            if state == "ready":
                break
            await asyncio.sleep(5)
        check("analysis reached ready", state == "ready", f"state={state}")
        if state != "ready":
            return 1

        planned = await s.auto_edit("plan_cut", {"brief_id": brief_id})
        check("plan_cut", bool(planned.get("success")),
              str(planned.get("error") or planned.get("plan_id")))
        if not planned.get("success"):
            return 1
        plan_id = planned["plan_id"]

        segs = (planned.get("plan") or {}).get("segments") or []
        speech = [g for g in segs if g.get("role") == "speech"]
        uuids = [g.get("clip_uuid") for g in speech]
        src_changes = sum(
            1 for i in range(1, len(speech))
            if speech[i].get("clip_uuid") != speech[i - 1].get("clip_uuid"))
        print(f"  [note] {len(speech)} speech segments, {len(set(uuids))} distinct "
              f"sources, {src_changes} source-change boundaries")
        check("plan spans two sources (dissolve trigger)", src_changes >= 1,
              f"src_changes={src_changes}, distinct={len(set(uuids))}")

        gate = await s.auto_edit("approve_cut", {"plan_id": plan_id})
        approved = await s.auto_edit("approve_cut", {
            "plan_id": plan_id, "confirm_token": gate.get("confirm_token")})
        check("approve_cut", bool(approved.get("success")), str(approved.get("error") or ""))

        gate = await s.auto_edit("build_timeline", {"plan_id": plan_id})
        built = await s.auto_edit("build_timeline", {
            "plan_id": plan_id, "confirm_token": gate.get("confirm_token")})
        check("build_timeline", bool(built.get("success")),
              str(built.get("error") or built.get("timeline_name")))
        if not built.get("success"):
            return 1

        # Polish: a lower-third plus the source-change cross-dissolve(s).
        popts = {"lower_thirds": [{"text": "Speaker A", "at_segment": 0}]}
        gate = await s.auto_edit("polish_timeline", {"plan_id": plan_id, "options": popts})
        check("polish_timeline issues token",
              gate.get("status") == "confirmation_required", str(gate.get("error") or ""))
        polished = await s.auto_edit("polish_timeline", {
            "plan_id": plan_id, "options": popts,
            "confirm_token": gate.get("confirm_token")})
        check("polish_timeline", bool(polished.get("success")),
              str(polished.get("error") or polished.get("polished_timeline")))
        if polished.get("success"):
            check("cross-dissolve present in polished timeline",
                  int(polished.get("transitions") or 0) >= 1,
                  f"transitions={polished.get('transitions')}")
            check("lower-third present in polished timeline",
                  int(polished.get("lower_thirds") or 0) >= 1,
                  f"lower_thirds={polished.get('lower_thirds')}")
            check("source clips stayed linked",
                  bool(polished.get("clips_relinked")),
                  str(polished.get("media_link")))
            print(f"  [note] polished timeline: {polished.get('polished_timeline')}"
                  f" ops={polished.get('ops_applied')}"
                  f" media_link={polished.get('media_link')}"
                  f" baseline={polished.get('baseline_media_link')}"
                  f" warning={polished.get('warning')}")
        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        try:
            if previous:
                pm.LoadProject(previous)
            pm.DeleteProject(PILOT)
        except Exception as exc:
            print(f"cleanup: {type(exc).__name__}: {exc}")
        shutil.rmtree(MEDIA_DIR, ignore_errors=True)


def main() -> int:
    import src.server as s
    code = asyncio.run(run(s))
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return code


if __name__ == "__main__":
    from preflight import gate
    gate("open")
    sys.exit(main())
