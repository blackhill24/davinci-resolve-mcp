"""Live Resolve validation for the add_keyframe BezierSpline fix (PR #56).

Connects to a running DaVinci Resolve, builds a disposable project holding one
Fusion Composition clip, adds a Transform tool, and applies the EXACT technique
used by the fixed `fusion_comp(action="add_keyframe")` handler:

    if not inp.GetConnectedOutput():
        tool.AddModifier(input_name, "BezierSpline")
    tool[input_name][time] = value

Then it reads the interpolated value at several frames and the keyframe list.
This validates the Fusion technique against the live app, independent of the
long-running MCP server process (which may still be serving pre-fix code).

Self-setting-up: creates its own project + timeline + Fusion comp, then deletes
the project and restores the original on exit, so it runs unattended in a batch.
Never touches the user's project, timeline, or any source media.

Run:
  RESOLVE_SCRIPT_API=/opt/resolve/Developer/Scripting \
  RESOLVE_SCRIPT_LIB=/opt/resolve/libs/Fusion/fusionscript.so \
  PYTHONPATH=.:/opt/resolve/Developer/Scripting/Modules \
  .venv/bin/python tests/domains/fusion_composition/live_fusion_keyframe_validation.py
"""

import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.core.platform import get_resolve_paths  # noqa: E402

paths = get_resolve_paths()
os.environ["RESOLVE_SCRIPT_API"] = paths["api_path"]
os.environ["RESOLVE_SCRIPT_LIB"] = paths["lib_path"]
sys.path.insert(0, paths["modules_path"])

import DaVinciResolveScript as dvr_script  # noqa: E402

from src.domains.project_lifecycle.utils.project_cleanup import delete_project_safely  # noqa: E402

PROJECT_NAME = "ZZ_fusion_keyframe_validation"


def _fusion_item(timeline):
    """The first timeline item on V1 that carries a Fusion comp."""
    for clip in timeline.GetItemListInTrack("video", 1) or []:
        if clip.GetFusionCompCount() > 0:
            return clip
    return None


def _validate(item) -> int:
    comp = item.GetFusionCompByIndex(1)
    tool_name = "MCPKeyTestLive"

    comp.Lock()
    try:
        tool = comp.FindTool(tool_name) or comp.AddTool("Transform", -1, -1)
        try:
            tool.SetAttrs({"TOOLS_Name": tool_name})
        except Exception:
            pass

        inp = tool["Size"]
        # The fix: attach a spline the first time, then key it.
        already = False
        try:
            already = inp.GetConnectedOutput() is not None
        except Exception:
            already = False
        if not already:
            tool.AddModifier("Size", "BezierSpline")
        tool["Size"][0] = 1.0
        tool["Size"][75] = 1.4
    finally:
        comp.Unlock()

    v0 = tool.GetInput("Size", 0)
    v37 = tool.GetInput("Size", 37)
    v75 = tool.GetInput("Size", 75)
    kfs = tool["Size"].GetKeyFrames()

    print(f"get_input(Size, 0)  = {v0}")
    print(f"get_input(Size, 37) = {v37}")
    print(f"get_input(Size, 75) = {v75}")
    print(f"GetKeyFrames()      = {kfs}  (raw {{index: frame}})")

    # Mirror the fixed get_keyframes handler: frame positions are the VALUES of
    # GetKeyFrames(); read each keyframed value back via GetInput(frame).
    serialized = [
        {"time": kfs[idx], "value": tool.GetInput("Size", kfs[idx])}
        for idx in sorted(kfs)
    ]
    print(f"get_keyframes()     = {serialized}")

    ok = (
        abs(v0 - 1.0) < 1e-6
        and abs(v75 - 1.4) < 1e-6
        and 1.0 < v37 < 1.4  # genuine interpolation between the keyframes
        and bool(kfs)
        and serialized == [
            {"time": 0.0, "value": 1.0},
            {"time": 75.0, "value": 1.4},
        ]
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    resolve = dvr_script.scriptapp("Resolve")
    if resolve is None:
        print("FATAL: cannot connect to Resolve")
        return 1
    print(f"Connected: {resolve.GetProductName()} {resolve.GetVersionString()}")

    pm = resolve.GetProjectManager()
    original = pm.GetCurrentProject()
    original_name = original.GetName() if original else None

    project = pm.LoadProject(PROJECT_NAME) or pm.CreateProject(PROJECT_NAME)
    if not project:
        print("FATAL: could not load or create disposable project")
        return 1
    try:
        timeline = project.GetMediaPool().CreateEmptyTimeline(f"ZZ_fusion_kf_tl_{int(time.time())}")
        if not timeline:
            print("FATAL: CreateEmptyTimeline failed")
            return 1
        project.SetCurrentTimeline(timeline)
        if not timeline.InsertFusionCompositionIntoTimeline():
            print("FATAL: InsertFusionCompositionIntoTimeline failed")
            return 1
        print(f"project={project.GetName()!r} timeline={timeline.GetName()!r}")

        item = _fusion_item(timeline)
        if item is None:
            print("FATAL: no timeline item with a Fusion comp on V1")
            return 1
        return _validate(item)
    finally:
        try:
            cleanup = delete_project_safely(pm, PROJECT_NAME, switch_to=original_name, retries=2)
            print(f"\ncleanup delete_project: {cleanup}")
        except Exception as e:
            print(f"cleanup delete error: {e}")
        try:
            if original_name:
                pm.LoadProject(original_name)
                print(f"restored project: {original_name}")
        except Exception as e:
            print(f"restore error: {e}")


if __name__ == "__main__":
    from tests.preflight import gate
    gate("open")
    sys.exit(main())
