#!/usr/bin/env python3
"""Live validation for the productized `timeline import_srt` tool + 3.2.6 styling
(issue #30).

Two questions:
  1. TEMPLATE SEEDING — can a SYNTHETIC subtitle cue element (minimal
     Sm2TiGenerator XML wrapping the embedded ground-truth EffectFiltersBA blob,
     inside a synthesized Sm2TiTrack SubType=3) survive .drt reimport? If yes, the
     tool can ship a fully self-contained embedded template later; if no, the
     template_drt / seed-a-cue-first requirement stays.
  2. TOOL — does `timeline import_srt` (with style overrides) produce a NEW
     "(subtitled)" timeline whose subtitle track carries the SRT's cues with the
     right timing, readable via the live transcript?

Run: .venv/bin/python tests/live_import_srt_tool_probe.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import subtitle_codec as sc  # noqa: E402

PILOT = f"import_srt_probe_{time.strftime('%H%M%S')}"
WORK = tempfile.mkdtemp(prefix="drm-srt-tool-probe-")
CHECKS: list[tuple[str, bool, str]] = []

# Ground-truth cue blob (same as tests/test_subtitle_codec.py CUE1).
from tests.test_subtitle_codec import CUE1  # noqa: E402

SRT = """1
00:00:01,000 --> 00:00:02,500
First imported cue

2
00:00:03,000 --> 00:00:05,000
Second cue — unicode café 日本語
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def synth_clip() -> str:
    out = os.path.join(WORK, "base.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=24:d=8",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out,
    ], check=True, capture_output=True)
    return out


def synth_template_drt(base_drt: str, out_path: str) -> None:
    """Transplant a SYNTHETIC subtitle track (one cue, ground-truth blob) into a
    real exported .drt."""
    cue = ('<Sm2TiGenerator DbId="9e6b1a58-0000-4000-8000-000000000001">'
           "<FieldsBlob/><PrettyType>Subtitle</PrettyType><Name>HELLO ALPHA CUE</Name>"
           "<Start>86424</Start><Duration>36</Duration>"
           "<MarkersBA/><UiMemento>0</UiMemento><Flags>0</Flags><PriorityIndex>0</PriorityIndex>"
           f"<EffectFiltersBA>{CUE1}</EffectFiltersBA>"
           "<RenderTextEnabled>true</RenderTextEnabled></Sm2TiGenerator>")
    track = ('<Element><Sm2TiTrack DbId="9e6b1a58-0000-4000-8000-000000000002">'
             "<FieldsBlob/><Type>2</Type><SubType>3</SubType><Flags>0</Flags>"
             '<Sequence>9e6b1a58-0000-4000-8000-000000000003</Sequence>'
             f"<Items><Element>{cue}</Element></Items>"
             "<FusionCompHolderItems/><UserDefinedName/><LayersVec/></Sm2TiTrack></Element>")
    with zipfile.ZipFile(base_drt) as zin:
        names = zin.namelist()
        seq_name = next(n for n in names
                        if re.search(r"(^|/)SeqContainer(/|\d*\.xml)", n) and n.endswith(".xml"))
        seq_xml = zin.read(seq_name).decode("utf-8")
        if "<SubtitleTrackVec/>" not in seq_xml:
            raise RuntimeError("base drt has no empty <SubtitleTrackVec/> to replace")
        seq_xml = seq_xml.replace(
            "<SubtitleTrackVec/>", f"<SubtitleTrackVec>{track}</SubtitleTrackVec>", 1)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for n in names:
                zout.writestr(n, seq_xml if n == seq_name else zin.read(n))


def _run_gated(s, action: str, params: dict) -> dict:
    gate = s.timeline(action, params)
    if gate.get("status") == "confirmation_required":
        return s.timeline(action, {**params, "confirm_token": gate.get("confirm_token")})
    return gate


def main() -> int:
    import src.server as s
    from src.utils.project_cleanup import delete_project_safely

    if not sc.zstd_available():
        print("zstandard not installed in the server env — exit 2")
        return 2

    srt_path = os.path.join(WORK, "cues.srt")
    open(srt_path, "w", encoding="utf-8").write(SRT)

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — aborting")
        return 2
    pm = r.GetProjectManager()
    proj = pm.CreateProject(PILOT)
    check("disposable project created", proj is not None, PILOT)
    if proj is None:
        return 2

    try:
        mp = proj.GetMediaPool()
        imported = mp.ImportMedia([synth_clip()])
        check("media imported", bool(imported))
        tl = mp.CreateEmptyTimeline("SRT Base")
        check("base timeline created", tl is not None)
        if tl is None:
            return 1
        proj.SetCurrentTimeline(tl)
        mp.AppendToTimeline(list(imported or []))

        # ---- Q1: synthetic template feasibility ----
        base_drt = os.path.join(WORK, "base.drt")
        exported = s._export_timeline_checked(tl, {
            "path": base_drt, "format": "drt",
            "require_temp_path": False, "background": False, "async_job": False})
        check("baseline drt exported", bool(exported.get("success")))
        template_drt = os.path.join(WORK, "template.drt")
        synthetic_ok = False
        if exported.get("success"):
            synth_template_drt(exported.get("primary_file") or base_drt, template_drt)
            timported = mp.ImportTimelineFromFile(template_drt, {
                "timelineName": "synthetic_template_probe", "importSourceClips": False})
            if timported is not None:
                n_sub = int(timported.GetTrackCount("subtitle") or 0)
                items = timported.GetItemListInTrack("subtitle", 1) or [] if n_sub else []
                texts = [str(it.GetName() or "") for it in items]
                synthetic_ok = n_sub >= 1 and any("HELLO ALPHA CUE" in t for t in texts)
                check("Q1: SYNTHETIC template cue survives reimport", synthetic_ok,
                      f"subtitle_tracks={n_sub} texts={texts}")
                proj.SetCurrentTimeline(tl)
            else:
                check("Q1: SYNTHETIC template cue survives reimport", False,
                      "ImportTimelineFromFile returned None")

        # ---- Q2: the import_srt tool — EMBEDDED template path (no template_drt,
        # no pre-existing cue; the tool must fall back to its built-in template) ----
        params = {"path": srt_path,
                  "style": {"size": 48, "color": "#ffcc00"}}
        result = _run_gated(s, "import_srt", params)
        check("import_srt (embedded template)", bool(result.get("success")),
              str(result.get("error") or ""))
        check("template_source == embedded",
              result.get("template_source") == "embedded",
              str(result.get("template_source")))
        if result.get("success"):
            check("2 cues imported", result.get("cues_imported") == 2,
                  str(result.get("cues_imported")))
            new_tl, _ = s._find_timeline_by_name(proj, result["new_timeline"])
            check("new '(subtitled)' timeline exists", new_tl is not None,
                  str(result.get("new_timeline")))
            if new_tl is not None:
                n_sub = int(new_tl.GetTrackCount("subtitle") or 0)
                check("subtitle track present", n_sub >= 1, f"tracks={n_sub}")
                items = new_tl.GetItemListInTrack("subtitle", 1) or []
                texts = [str(it.GetName() or "") for it in items]
                check("cue texts round-tripped",
                      any("First imported cue" in t for t in texts)
                      and any("café" in t for t in texts), str(texts))
                if items:
                    st = int(items[0].GetStart())
                    tl_start = int(new_tl.GetStartFrame())
                    check("cue timing lands at +1s", st == tl_start + 24,
                          f"start={st} expected={tl_start + 24}")
            check("original timeline untouched",
                  int(tl.GetTrackCount("subtitle") or 0) == 0)

            # ---- append mode: import MORE cues onto the (subtitled) timeline,
            # keeping the existing two ----
            if new_tl is not None:
                more_srt = os.path.join(WORK, "more.srt")
                open(more_srt, "w", encoding="utf-8").write(
                    "1\n00:00:06,000 --> 00:00:07,000\nAppended third cue\n")
                proj.SetCurrentTimeline(new_tl)
                appended = _run_gated(s, "import_srt", {"path": more_srt, "mode": "append"})
                proj.SetCurrentTimeline(tl)
                check("import_srt mode=append", bool(appended.get("success")),
                      str(appended.get("error") or ""))
                if appended.get("success"):
                    app_tl, _ = s._find_timeline_by_name(proj, appended["new_timeline"])
                    if app_tl is not None:
                        app_items = app_tl.GetItemListInTrack("subtitle", 1) or []
                        app_texts = [str(it.GetName() or "") for it in app_items]
                        check("append kept old cues + added new (3 total)",
                              len(app_items) == 3
                              and any("Appended third cue" in t for t in app_texts),
                              str(app_texts))

            # ---- style verification via re-export: Resolve must PRESERVE the
            # styled blob through its own import->export round trip ----
            if new_tl is not None:
                reexp = os.path.join(WORK, "styled_roundtrip.drt")
                proj.SetCurrentTimeline(new_tl)
                rex = s._export_timeline_checked(new_tl, {
                    "path": reexp, "format": "drt",
                    "require_temp_path": False, "background": False, "async_job": False})
                proj.SetCurrentTimeline(tl)
                if rex.get("success"):
                    with zipfile.ZipFile(rex.get("primary_file") or reexp) as z:
                        seq_name = next(n for n in z.namelist()
                                        if re.search(r"(^|/)SeqContainer(/|\d*\.xml)", n)
                                        and n.endswith(".xml"))
                        seq_xml = z.read(seq_name).decode("utf-8")
                    t = sc.find_template_cue(seq_xml)
                    if t is None:
                        check("style survives Resolve round trip", False,
                              "no cue found in re-export")
                    else:
                        style = sc.read_cue_style(t["eff_hex"])
                        check("style survives Resolve round trip",
                              style.get("color") == "#ffcc00"
                              and abs((style.get("size") or 0) - 48.0) < 0.01,
                              str(style))
                else:
                    check("style survives Resolve round trip", False,
                          "re-export failed")

    finally:
        try:
            delete_project_safely(pm, PILOT)
            print(f"Deleted disposable project: {PILOT}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup warning: {exc}")
        import shutil
        shutil.rmtree(WORK, ignore_errors=True)

    failures = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    from preflight import gate
    gate("open")
    raise SystemExit(main())
