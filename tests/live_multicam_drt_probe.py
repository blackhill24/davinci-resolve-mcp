#!/usr/bin/env python3
"""3.1.7 (issue #30): export-diff investigation of the UI-authored MULTICAM clip.

There is no scripting method to create a native multicam clip (the May-2026 API
doc has zero multicam mentions; `media_pool setup_multicam_timeline` only preps a
stacked timeline — the conversion is UI-only, see api_truth). Before any vendor
work can even be scoped, the on-disk representation must be captured: this
harness diffs a project .drp exported BEFORE vs AFTER the one manual GUI step.

Phases (the multicam conversion itself is the manual GUI step in between):

  setup   - disposable project + 2 synthetic camera angles imported, baseline
            .drp exported, project left open+current. THEN the operator selects
            both clips in the Media Pool and does:
              right-click -> "New Multicam Clip Using Selected Clips..."
              (sync by timecode is fine for the synthetic media)
  diff    - re-export the .drp and diff vs baseline (src/domains/timeline_conform_interchange/utils/drt_diff), dump
            the multicam-bearing regions (new SeqContainers / MpClip entries /
            changed folders) so the encoding is visible.
  cleanup - delete the disposable project.

Run: .venv/bin/python tests/live_multicam_drt_probe.py setup
     ... (manual GUI: create the multicam clip) ...
     .venv/bin/python tests/live_multicam_drt_probe.py diff
     .venv/bin/python tests/live_multicam_drt_probe.py cleanup
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domains.timeline_conform_interchange.utils import drt_diff  # noqa: E402

STATE_FILE = os.path.join(tempfile.gettempdir(), "drm-multicam-probe-state.json")


def _run_ffmpeg(args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _make_angles(work_dir: str):
    paths = []
    for name, vf, freq in (("cam_a.mov", "testsrc2=size=320x180:rate=24:duration=4", 880),
                           ("cam_b.mov", "testsrc=size=320x180:rate=24:duration=4", 660)):
        out = os.path.join(work_dir, name)
        _run_ffmpeg([
            "-f", "lavfi", "-i", vf,
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=4",
            "-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p",
            "-c:a", "pcm_s16le", "-timecode", "01:00:00:00", out])
        paths.append(out)
    return paths


def _state() -> dict:
    if not os.path.exists(STATE_FILE):
        print(f"no state file {STATE_FILE} — run setup first")
        sys.exit(2)
    return json.load(open(STATE_FILE))


def phase_setup() -> int:
    import src.server as s
    r = s.get_resolve()
    if r is None:
        print("Resolve not available")
        return 2
    pm = r.GetProjectManager()
    name = f"multicam_probe_{int(time.time())}"
    proj = pm.CreateProject(name)
    if proj is None:
        print("project create failed")
        return 1
    work = tempfile.mkdtemp(prefix="drm-multicam-probe-")
    mp = proj.GetMediaPool()
    imported = mp.ImportMedia(_make_angles(work))
    print(f"imported {len(imported or [])} angle clips")
    pm.SaveProject()
    baseline = os.path.join(work, "baseline.drp")
    if not pm.ExportProject(name, baseline, False):
        print("baseline .drp export failed")
        return 1
    json.dump({"project": name, "work": work, "baseline": baseline},
              open(STATE_FILE, "w"))
    print(f"baseline exported: {baseline}")
    print("\nNOW (manual GUI step): select cam_a + cam_b in the Media Pool ->\n"
          "  right-click -> 'New Multicam Clip Using Selected Clips...' -> sync\n"
          "  by timecode -> Create. Then run the diff phase.")
    return 0


def phase_diff() -> int:
    import src.server as s
    st = _state()
    r = s.get_resolve()
    pm = r.GetProjectManager()
    pm.SaveProject()
    after = os.path.join(st["work"], "after_multicam.drp")
    if not pm.ExportProject(st["project"], after, False):
        print("after .drp export failed")
        return 1
    report = drt_diff.diff_containers(st["baseline"], after)
    if report.get("error"):
        print(f"diff error: {report['error']}")
        return 1
    print(f"summary: {report.get('summary')}")
    print(f"added:   {report.get('added')}")
    print(f"removed: {report.get('removed')}")
    for change in report.get("changed", []):
        print(f"\n----- changed: {change.get('name')} "
              f"(renamed_from={change.get('renamed_from')}) -----")
        print(json.dumps(drt_diff.significant_lines(change), indent=2,
                         default=str)[:4000])

    # Dump any NEW SeqContainer entries wholesale — the multicam container is
    # expected to be one of them (multicam ~= timeline-like angle stack).
    with zipfile.ZipFile(st["baseline"]) as zb, zipfile.ZipFile(after) as za:
        before_names = set(zb.namelist())
        for n in za.namelist():
            if n in before_names or not n.endswith(".xml"):
                continue
            xml = za.read(n).decode("utf-8", errors="replace")
            print(f"\n===== NEW ENTRY: {n} ({len(xml)} chars) =====")
            print(xml[:6000])
            for tag in sorted(set(re.findall(r"<(Sm2\w+)", xml))):
                print(f"  element: {tag}")
    print(f"\nafter export: {after} (kept for offline analysis)")
    return 0


def phase_cleanup() -> int:
    import src.server as s
    from src.domains.project_lifecycle.utils.project_cleanup import delete_project_safely
    st = _state()
    r = s.get_resolve()
    delete_project_safely(r.GetProjectManager(), st["project"])
    print(f"deleted {st['project']} (work dir kept: {st['work']})")
    os.remove(STATE_FILE)
    return 0


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if phase == "setup":
        return phase_setup()
    if phase == "diff":
        return phase_diff()
    if phase == "cleanup":
        return phase_cleanup()
    print(f"unknown phase {phase!r} (setup|diff|cleanup)")
    return 1


if __name__ == "__main__":
    from preflight import gate
    gate("open")
    raise SystemExit(main())
