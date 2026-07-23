#!/usr/bin/env python3
"""Live proof for 3.3.4 (issue #23): proxy-media generation + link.

The scripting API has no GenerateProxy — only LinkProxyMedia attaches an EXISTING
proxy. The workaround productized here (`render build_proxies`) renders the target
clips as individual clips into a proxy dir (ExportAudio=False dodges the headless
Fairlight/PipeWire 0%-stall), links each result with LinkProxyMedia, and verifies via
the clip's 'Proxy Media Path' readback.

Creates a disposable project + one synthetic clip, builds a downscaled ProRes 422
Proxy, and asserts the clip reports a proxy path. Requires a running Resolve 21 Studio.
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


def main() -> int:
    _install_mcp_stubs()
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        import src.server as server
    finally:
        sys.argv = original_argv

    from src.domains.color_grade.utils.color_grade_live_probe import _make_synthetic_video, _first_imported_clip

    work_dir = Path(tempfile.mkdtemp(prefix="mcp_proxy_build_probe_"))
    # Resolve's render queue refuses to write into the system temp dir — render into
    # a disposable subfolder of the user's Videos folder instead.
    proxy_dir = Path.home() / "Videos" / f"_mcp_proxy_out_{int(time.time())}"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    project_name = f"_mcp_proxy_build_probe_{int(time.time())}"
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
        clip = _first_imported_clip(mp.ImportMedia([str(_make_synthetic_video(work_dir))]) or [])
        assert clip, "synthetic media import failed"
        src_name = clip.GetName()
        print(f"Source clip: {src_name}")

        dry = server.render("build_proxies", {"proxy_dir": str(proxy_dir), "require_temp_target": False, "dry_run": True})
        print(f"dry_run: {dry}")

        result = server.render("build_proxies", {
            "proxy_dir": str(proxy_dir),
            "require_temp_target": False,
            "format": "mov", "codec": "ProRes422P",
            "width": 320, "height": 180,
            "timeout_s": 240,
        })
        print(f"build_proxies: {result}")

        if not (isinstance(result, dict) and result.get("success")):
            failures.append(f"build_proxies failed: {result}")
        elif result.get("linked", 0) < 1:
            failures.append(f"no proxies linked: {result}")
        else:
            # Independent readback straight from the API.
            proxy_path = clip.GetClipProperty("Proxy Media Path") or ""
            print(f"Proxy Media Path readback: {proxy_path!r}")
            if not proxy_path:
                failures.append("clip reports no Proxy Media Path after link")
            else:
                print("PASS: rendered + linked proxy, readback confirms attachment")

    finally:
        if created:
            server.project_manager("save")
            server.project_manager("close")
            server.project_manager("delete", {"name": project_name})
            print(f"Deleted disposable project: {project_name}")
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(proxy_dir, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    return 0


if __name__ == "__main__":
    from tests.preflight import gate
    gate("open")
    raise SystemExit(main())
