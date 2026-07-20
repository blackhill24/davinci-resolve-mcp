"""Live re-verification (Stage 4, issue #24) of two api_truth bug entries that
had no Resolve-21 stamp: Composition.Paste and FlowView.SetPos/GetPosTable.

Both are exercised through the shipped fusion_comp mitigations (copy_tool,
get_position/set_position) rather than the raw API, since that's what
production code actually calls.

Disposable project only; deletes it and restores the original project on exit.

Run:
  RESOLVE_SCRIPT_API=/opt/resolve/Developer/Scripting \
  RESOLVE_SCRIPT_LIB=/opt/resolve/libs/Fusion/fusionscript.so \
  PYTHONPATH=.:/opt/resolve/Developer/Scripting/Modules \
  .venv/bin/python tests/live_fusion_bug_reverify.py
"""
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import src.server as s  # noqa: E402
from src.utils.project_cleanup import delete_project_safely  # noqa: E402

PROJECT_NAME = "ZZ_fusion_bug_reverify"


def main() -> int:
    resolve = s.get_resolve()
    if not resolve:
        print("FATAL: cannot connect to Resolve")
        return 1
    print(f"Connected: {resolve.GetProductName()} {resolve.GetVersionString()}")
    pm = resolve.GetProjectManager()
    original = pm.GetCurrentProject()
    original_name = original.GetName() if original else None

    proj = pm.LoadProject(PROJECT_NAME) or pm.CreateProject(PROJECT_NAME)
    if not proj:
        print("FATAL: could not load or create disposable project")
        return 1
    try:
        mp = proj.GetMediaPool()
        tl = mp.CreateEmptyTimeline(f"ZZ_fusion_tl_{int(time.time())}")
        if not tl:
            print("FATAL: CreateEmptyTimeline failed")
            return 1
        proj.SetCurrentTimeline(tl)
        if not tl.InsertFusionCompositionIntoTimeline():
            print("FATAL: InsertFusionCompositionIntoTimeline failed")
            return 1

        scope = {"timeline_item": {"track_type": "video", "track_index": 1, "item_index": 0}}
        resolve.OpenPage("fusion")

        add = s.fusion_comp("add_tool", {**scope, "tool_type": "Background", "name": "ZZ_Src"})
        print(f"add_tool -> {add}")
        if not add.get("tool_name"):
            print("FAIL: add_tool did not create a source node")
            return 1

        # ---- Composition.Paste workaround: copy_tool (AddTool + Save/LoadSettings file round-trip) ----
        copy = s.fusion_comp("copy_tool", {**scope, "tool_name": "ZZ_Src", "name": "ZZ_Copy"})
        print(f"copy_tool -> {copy}")
        copy_ok = bool(copy.get("success")) and copy.get("new_tool") == "ZZ_Copy"

        listing = s.fusion_comp("get_tool_list", scope)
        names = [t["name"] for t in listing.get("tools", [])]
        copy_persisted = "ZZ_Copy" in names
        print(f"tool_list after copy -> {names}")

        # ---- FlowView.SetPos/GetPosTable workaround: set_position + get_position ----
        setpos = s.fusion_comp("set_position", {**scope, "tool_name": "ZZ_Copy", "x": 3.5, "y": -2.0})
        print(f"set_position -> {setpos}")
        getpos = s.fusion_comp("get_position", {**scope, "tool_name": "ZZ_Copy"})
        print(f"get_position -> {getpos}")
        pos_roundtrip = (
            getpos.get("x") is not None and getpos.get("y") is not None
            and abs(getpos["x"] - 3.5) < 0.01 and abs(getpos["y"] - (-2.0)) < 0.01
        )

        print("\n" + "=" * 70)
        print(f"Composition.Paste (copy_tool) round-trip: "
              f"{'PASS' if copy_ok and copy_persisted else 'FAIL'}")
        print(f"FlowView SetPos/GetPosTable round-trip: "
              f"{'PASS' if pos_roundtrip else 'FAIL'}")
        print("=" * 70)
        return 0 if (copy_ok and copy_persisted and pos_roundtrip) else 1
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
    raise SystemExit(main())
