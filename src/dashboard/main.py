"""Entry point for the standalone local analysis dashboard."""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from src.dashboard.state import DashboardState, _inventory_prefs
from src.dashboard.handler import Handler
from src.dashboard.media_inventory import resolve_media_inventory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local Resolve MCP control panel.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project-name", default="Dashboard Analysis")
    parser.add_argument("--project-id", default="dashboard")
    parser.add_argument("--analysis-root", default=os.path.expanduser("~/Documents/davinci-resolve-mcp-analysis"))
    open_group = parser.add_mutually_exclusive_group()
    open_group.add_argument("--open", dest="open", action="store_true", help="Open the control panel in the default browser.")
    open_group.add_argument("--no-open", dest="open", action="store_false", help="Run the control panel server without opening a browser.")
    parser.set_defaults(open=False)
    return parser.parse_args()


def _warm_inventory_cache(project_root: str) -> None:
    """Build the first Resolve inventory in the background at startup.

    Populates the inventory + path-existence caches before the browser connects so
    the first dashboard open paints live data immediately instead of waiting on a
    cold Media Pool walk. Best-effort: if Resolve isn't up yet this no-ops and the
    first real request builds normally.
    """
    try:
        pref_limit, exclude_bins = _inventory_prefs()
        resolve_media_inventory(project_root, limit=pref_limit, exclude_bins=exclude_bins)
    except Exception:  # noqa: BLE001 — warm-up must never crash startup
        pass


def main() -> None:
    from src.core import actor_identity
    actor_identity.set_instance("control-panel")
    args = parse_args()
    state = DashboardState(args.project_name, args.project_id, args.analysis_root)
    Handler.state = state
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"DaVinci Resolve MCP: {url}")
    print(f"Project analysis root: {state.project_root}")
    threading.Thread(target=_warm_inventory_cache, args=(state.project_root,), daemon=True).start()
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
