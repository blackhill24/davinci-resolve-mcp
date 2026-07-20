#!/usr/bin/env python3
"""Live proof for 3.3.1 (issue #23): offline-authored DRX node tree applies live.

The UI-only gap is that the live API cannot add/connect nodes or write primary grade
values. The workaround is: author a multi-node .drx OFFLINE (advanced-server `drx
build_graph` — node tree + primaries) then apply it live via ApplyGradeFromDRX
(`timeline_item_color safe_apply_drx`). This probe proves the whole loop end to end:

  build_graph (2 nodes, primaries)  ->  safe_apply_drx  ->  GetNumNodes() == 2

Creates a disposable project + synthetic clip, applies, reads the node count back, then
deletes the project. Requires a running Resolve 21 Studio and Node (advanced server).
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import types
from pathlib import Path


def _install_mcp_stubs() -> None:
    """Allow importing src.server when the real MCP SDK is absent."""
    try:
        import mcp.server.fastmcp  # noqa: F401

        return  # real SDK available — stubs would shadow it
    except ImportError:
        pass

    class FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            def decorate(func):
                return func

            return decorate

        def resource(self, *args, **kwargs):
            def decorate(func):
                return func

            return decorate

    class Context:
        pass

    class Image:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

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
    sys.modules.setdefault("anyio", anyio)
    sys.modules.setdefault("mcp", mcp)
    sys.modules.setdefault("mcp.types", mcp_types)
    sys.modules.setdefault("mcp.server", server_mod)
    sys.modules.setdefault("mcp.server.fastmcp", fastmcp)
    sys.modules.setdefault("mcp.server.stdio", stdio)


def _num_nodes(item) -> int:
    graph = item.GetNodeGraph()
    return int(graph.GetNumNodes())


def _apply_with_confirm(server, params):
    """safe_apply_drx is a destructive whole-graph replace — handle the confirm gate."""
    first = server.timeline_item_color("safe_apply_drx", params)
    token = first.get("confirm_token") if isinstance(first, dict) else None
    if token:
        return server.timeline_item_color("safe_apply_drx", {**params, "confirm_token": token})
    return first


def main() -> int:
    _install_mcp_stubs()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        import src.server as server
    finally:
        sys.argv = original_argv

    from src.utils import advanced_bridge
    from src.utils.color_grade_live_probe import _make_synthetic_video, _first_imported_clip

    if not advanced_bridge.node_available():
        print("SKIP: Node (advanced server) unavailable — cannot author DRX offline.")
        return 0

    work_dir = Path(tempfile.mkdtemp(prefix="mcp_drx_author_probe_"))
    project_name = f"_mcp_drx_author_probe_{int(time.time())}"
    created = False
    failures = []

    try:
        version = server.resolve_control("get_version")
        print(f"Connected to {version.get('product')} {version.get('version_string')}")

        assert server.project_manager("create", {"name": project_name}).get("success"), "project create failed"
        created = True

        video = _make_synthetic_video(work_dir)
        resolve = server.get_resolve()
        project = resolve.GetProjectManager().GetCurrentProject()
        media_pool = project.GetMediaPool()
        clip = _first_imported_clip(media_pool.ImportMedia([str(video)]) or [])
        assert clip, "synthetic media import failed"
        timeline = media_pool.CreateTimelineFromClips("DRX Author Probe", [clip])
        assert timeline, "timeline create failed"
        project.SetCurrentTimeline(timeline)
        server.resolve_control("open_page", {"page": "color"})

        items = timeline.GetItemListInTrack("video", 1) or []
        assert items, "no video items on timeline"
        baseline = _num_nodes(items[0])
        print(f"Baseline node count: {baseline}")

        # --- Author a 2-node tree OFFLINE (node tree + primaries) ---
        drx_path = work_dir / "authored_tree.drx"
        author = advanced_bridge.run_node_bridge(
            "scripts/drp-bridge.mjs",
            [
                "drx",
                "build_graph",
                json.dumps(
                    {
                        "nodes": [
                            {"label": "Balance", "params": {"lift": {"r": 0.01, "b": -0.01}, "gain": {"r": 1.03, "b": 0.97}}},
                            {"label": "Contrast", "params": {"contrast": 1.2, "pivot": 0.4}},
                        ],
                        "outputPath": str(drx_path),
                    }
                ),
            ],
        )
        if not author.get("success"):
            failures.append(f"offline build_graph failed: {author}")
        else:
            print(f"Authored offline: nodeCount={author['result'].get('nodeCount')} bytes={author['result'].get('bytes')}")
            assert drx_path.is_file(), "authored .drx not written"

            # --- Apply live ---
            applied = _apply_with_confirm(
                server,
                {"track_type": "video", "track_index": 1, "item_index": 0, "path": str(drx_path), "grade_mode": 0},
            )
            if not (isinstance(applied, dict) and applied.get("success")):
                failures.append(f"safe_apply_drx failed: {applied}")
            else:
                after = _num_nodes(items[0])
                print(f"Node count after apply: {after}")
                if after != 2:
                    failures.append(f"expected 2 nodes after apply, got {after}")
                else:
                    print("PASS: offline-authored 2-node tree landed live via ApplyGradeFromDRX")

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
    from preflight import gate
    gate("open")
    raise SystemExit(main())
