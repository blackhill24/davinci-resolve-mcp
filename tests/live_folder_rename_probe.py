#!/usr/bin/env python3
"""Live proof for 3.3.3 (issue #23): media-pool folder rename fallback.

The scripting API has no RenameSubFolder. The lossless path is offline (advanced
project_db rename_folder, a Sm2MpFolder.Name UPDATE on a closed project). This probe
covers the LIVE fallback (`media_pool rename_folder`): a delete-recreate that preserves
clips + subfolders while losing ColorTag / UniqueId / manual ordering. It creates a
disposable project with Master/Old { a clip, a Child subfolder }, renames Old -> New,
and asserts New exists with the clip + Child moved and Old gone.

Requires a running Resolve 21 Studio.
"""

from __future__ import annotations

import sys
import tempfile
import time
import types
from pathlib import Path


def _install_mcp_stubs() -> None:
    try:
        import mcp.server.fastmcp  # noqa: F401

        return
    except ImportError:
        pass

    class FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda f: f

        def resource(self, *a, **k):
            return lambda f: f

    class Context:
        pass

    class Image:
        def __init__(self, *a, **k):
            pass

    anyio = types.ModuleType("anyio")
    anyio.run = lambda func: func()
    mcp = types.ModuleType("mcp")
    mcp_types = types.ModuleType("mcp.types")
    server_mod = types.ModuleType("mcp.server")
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    stdio = types.ModuleType("mcp.server.stdio")
    fastmcp.FastMCP = FastMCP
    fastmcp.Context = Context
    fastmcp.Image = Image
    stdio.stdio_server = lambda *a, **k: None
    mcp.types = mcp_types
    for name, mod in [("anyio", anyio), ("mcp", mcp), ("mcp.types", mcp_types),
                      ("mcp.server", server_mod), ("mcp.server.fastmcp", fastmcp),
                      ("mcp.server.stdio", stdio)]:
        sys.modules.setdefault(name, mod)


def _subfolder_names(mp, parent_path):
    root = mp.GetRootFolder()

    def walk(folder, path):
        if path == "":
            return folder
        parts = path.split("/")
        cur = folder
        for part in parts:
            nxt = None
            for s in cur.GetSubFolderList() or []:
                if s.GetName() == part:
                    nxt = s
                    break
            if not nxt:
                return None
            cur = nxt
        return cur

    target = walk(root, parent_path)
    if not target:
        return None
    return [s.GetName() for s in target.GetSubFolderList() or []]


def main() -> int:
    _install_mcp_stubs()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        import src.server as server
    finally:
        sys.argv = original_argv

    from src.utils.color_grade_live_probe import _make_synthetic_video, _first_imported_clip

    work_dir = Path(tempfile.mkdtemp(prefix="mcp_folder_rename_probe_"))
    project_name = f"_mcp_folder_rename_probe_{int(time.time())}"
    created = False
    failures = []

    try:
        version = server.resolve_control("get_version")
        print(f"Connected to {version.get('product')} {version.get('version_string')}")
        assert server.project_manager("create", {"name": project_name}).get("success"), "project create failed"
        created = True

        resolve = server.get_resolve()
        project = resolve.GetProjectManager().GetCurrentProject()
        mp = project.GetMediaPool()
        root = mp.GetRootFolder()
        old = mp.AddSubFolder(root, "Old")
        assert old, "could not create 'Old'"
        mp.AddSubFolder(old, "Child")
        # import a clip into 'Old'
        mp.SetCurrentFolder(old)
        clip = _first_imported_clip(mp.ImportMedia([str(_make_synthetic_video(work_dir))]) or [])
        assert clip, "clip import failed"

        clips_before = len(old.GetClipList() or [])
        subs_before = len(old.GetSubFolderList() or [])
        print(f"'Old' before: clips={clips_before} subfolders={subs_before}")

        # dry_run first
        dry = server.media_pool("rename_folder", {"path": "Master/Old", "new_name": "New", "dry_run": True})
        print(f"dry_run: {dry}")

        # real rename (two-step confirm gate)
        first = server.media_pool("rename_folder", {"path": "Master/Old", "new_name": "New"})
        token = first.get("confirm_token") if isinstance(first, dict) else None
        result = server.media_pool("rename_folder", {"path": "Master/Old", "new_name": "New", "confirm_token": token}) if token else first
        print(f"rename result: {result}")

        if not (isinstance(result, dict) and result.get("success")):
            failures.append(f"rename_folder failed: {result}")
        else:
            names = _subfolder_names(mp, "") or []
            if "New" not in names:
                failures.append(f"'New' not found under Master after rename: {names}")
            if "Old" in names:
                failures.append(f"'Old' still present after rename: {names}")
            if result.get("clips_moved") != clips_before:
                failures.append(f"clips_moved {result.get('clips_moved')} != {clips_before}")
            if result.get("subfolders_moved") != subs_before:
                failures.append(f"subfolders_moved {result.get('subfolders_moved')} != {subs_before}")
            if not failures:
                print("PASS: live delete-recreate rename preserved clips + subfolders")

    finally:
        if created:
            server.project_manager("save")
            server.project_manager("close")
            server.project_manager("delete", {"name": project_name})
            print(f"Deleted disposable project: {project_name}")
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
