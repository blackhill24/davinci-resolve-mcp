#!/usr/bin/env python3
"""Live proof for 3.3.5 (issue #23): title/generator placement layer.

There is no Source/Auto Track Selector API — Insert*IntoTimeline always lands on V1
and locking V1 makes the insert FAIL rather than redirect to V2. This probe:
  1. re-probes the 21.0 timeline surface to confirm NO track-selector method appeared,
  2. proves the lock-aware guard: with V1 locked, safe_place_overlay fails loudly
     (V1_LOCKED) instead of silently misfiring,
  3. proves a real placement lands on V1 after unlocking.

Requires a running Resolve 21 Studio.
"""

from __future__ import annotations

import sys
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


def main() -> int:
    _install_mcp_stubs()
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        import src.server as server
    finally:
        sys.argv = original_argv

    project_name = f"_mcp_track_selector_probe_{int(time.time())}"
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
        timeline = mp.CreateEmptyTimeline("Track Selector Probe")
        assert timeline, "empty timeline create failed"
        project.SetCurrentTimeline(timeline)

        # 1. Re-probe: no track-selector method on 21.0
        methods = [m for m in dir(timeline) if not m.startswith("_")]
        selector = [m for m in methods
                    if ("selector" in m.lower())
                    or ("targettrack" in m.lower())
                    or ("patch" in m.lower())]
        print(f"Track-selector-ish methods on Timeline: {selector or 'NONE'}")
        if selector:
            failures.append(f"unexpected track-selector methods surfaced: {selector}")

        # 2. Lock-aware guard: lock V1, expect V1_LOCKED
        server.timeline("set_track_lock", {"track_type": "video", "index": 1, "locked": True})
        locked_res = server.timeline("safe_place_overlay", {"kind": "generator", "name": "Solid Color"})
        print(f"placed with V1 locked: {locked_res}")
        code = (locked_res.get("error") or {}).get("code") if isinstance(locked_res, dict) else None
        if code != "V1_LOCKED":
            failures.append(f"expected V1_LOCKED with locked V1, got {locked_res}")

        # 3. Unlock and place for real
        server.timeline("set_track_lock", {"track_type": "video", "index": 1, "locked": False})
        placed = server.timeline("safe_place_overlay", {"kind": "generator", "name": "Solid Color"})
        print(f"placed after unlock: {placed}")
        if not (isinstance(placed, dict) and placed.get("placed")):
            failures.append(f"placement failed after unlock: {placed}")
        elif placed.get("items_after", 0) <= placed.get("items_before", 0):
            failures.append(f"no new V1 item after place: {placed}")
        else:
            print("PASS: no selector API, lock guard fires, placement lands on V1")

    finally:
        if created:
            server.project_manager("save")
            server.project_manager("close")
            server.project_manager("delete", {"name": project_name})
            print(f"Deleted disposable project: {project_name}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    return 0


if __name__ == "__main__":
    from preflight import gate
    gate("open")
    raise SystemExit(main())
