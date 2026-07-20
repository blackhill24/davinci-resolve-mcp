#!/usr/bin/env python3
"""Resolve status preflight for live tests.

Answers, BEFORE any live_* harness runs: is Resolve closed, open without a
project, or open with a project? Never launches or mutates Resolve.

Run:  .venv/bin/python tests/preflight.py [--json] [--require open|project|timeline]

States and default exit codes (with --require open, the default):
  open_project / open_no_project      -> 0
  closed                              -> 2   (Resolve not running / scripting off)
  scripting_unavailable               -> 3   (DaVinciResolveScript import failed)

--require project additionally exits 2 on open_no_project; --require timeline
also exits 2 when no timeline is current. Exit code 1 is deliberately unused so
"environment not ready" (2/3) never collides with a harness's own test failure.

Typical gate before a live suite:
  .venv/bin/python tests/preflight.py --require project || exit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.platform import setup_environment  # noqa: E402

EXIT_READY = 0
EXIT_NOT_READY = 2
EXIT_NO_SCRIPTING = 3


def collect_status() -> dict:
    """Classify the running-Resolve state without launching anything."""
    status: dict = {
        "state": "closed",
        "product": None,
        "version": None,
        "project": None,
        "timeline": None,
        "page": None,
        "detail": None,
    }

    setup_environment()
    try:
        import DaVinciResolveScript as dvr_script
    except ImportError as exc:
        status["state"] = "scripting_unavailable"
        status["detail"] = (
            f"import DaVinciResolveScript failed: {exc}. Check RESOLVE_SCRIPT_API/"
            "RESOLVE_SCRIPT_LIB/PYTHONPATH (see scripts/doctor.py)."
        )
        return status

    try:
        resolve = dvr_script.scriptapp("Resolve")
    except Exception as exc:
        status["detail"] = f"scriptapp('Resolve') raised: {exc!r}"
        return status

    if not resolve:
        status["detail"] = (
            "scriptapp('Resolve') returned no object. Resolve is not running, or "
            "Preferences > General > 'External scripting using' is not Local."
        )
        return status

    try:
        status["product"] = resolve.GetProductName()
        status["version"] = resolve.GetVersionString()
        status["page"] = resolve.GetCurrentPage()
    except Exception as exc:
        status["detail"] = f"Resolve handle answered but root calls failed: {exc!r}"
        return status

    project = None
    try:
        pm = resolve.GetProjectManager()
        project = pm.GetCurrentProject() if pm else None
    except Exception as exc:
        status["detail"] = f"GetProjectManager/GetCurrentProject raised: {exc!r}"

    if not project:
        status["state"] = "open_no_project"
        status.setdefault("detail", None)
        if status["detail"] is None:
            status["detail"] = "Resolve is open on the project manager with no project loaded."
        return status

    status["state"] = "open_project"
    try:
        status["project"] = project.GetName()
        timeline = project.GetCurrentTimeline()
        status["timeline"] = timeline.GetName() if timeline else None
    except Exception as exc:
        status["detail"] = f"project introspection raised: {exc!r}"
    return status


def gate(require: str = "open") -> dict:
    """Import-and-call preflight for live_* harnesses.

    Prints the status line and, when the requirement is not met, exits the
    process with EXIT_NOT_READY/EXIT_NO_SCRIPTING (2/3 — never 1, so a gate
    stop is distinguishable from a real test failure). Returns the status
    dict when ready. Also sets DAVINCI_MCP_NO_AUTOLAUNCH so a Resolve that
    quits mid-run fails fast instead of relaunching."""
    import os

    os.environ.setdefault("DAVINCI_MCP_NO_AUTOLAUNCH", "1")
    status = collect_status()
    state = status["state"]

    if state == "open_project":
        print(
            f"[preflight] open_project: {status['product']} {status['version']} — "
            f"project '{status['project']}', timeline {status['timeline']!r}, "
            f"page {status['page']!r}"
        )
    elif state == "open_no_project":
        print(f"[preflight] open_no_project: {status['product']} {status['version']} — no project loaded")
    else:
        print(f"[preflight] {state}: {status['detail']}")

    if state == "scripting_unavailable":
        print("[preflight] NOT READY — fix scripting env (see scripts/doctor.py); aborting live run.")
        sys.exit(EXIT_NO_SCRIPTING)
    if state == "closed":
        print("[preflight] NOT READY — start DaVinci Resolve (Studio) first; aborting live run.")
        sys.exit(EXIT_NOT_READY)
    if state == "open_no_project" and require in ("project", "timeline"):
        print("[preflight] NOT READY — this harness needs a project open; aborting live run.")
        sys.exit(EXIT_NOT_READY)
    if require == "timeline" and not status["timeline"]:
        print("[preflight] NOT READY — this harness needs a current timeline; aborting live run.")
        sys.exit(EXIT_NOT_READY)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Report Resolve status before running live tests.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--require",
        choices=["open", "project", "timeline"],
        default="open",
        help="Readiness bar: open = Resolve responding (default); project = a project "
        "must be loaded; timeline = a timeline must also be current.",
    )
    args = parser.parse_args()

    status = collect_status()
    state = status["state"]

    if state == "scripting_unavailable":
        code = EXIT_NO_SCRIPTING
    elif state == "closed":
        code = EXIT_NOT_READY
    elif state == "open_no_project":
        code = EXIT_NOT_READY if args.require in ("project", "timeline") else EXIT_READY
    else:  # open_project
        code = EXIT_NOT_READY if args.require == "timeline" and not status["timeline"] else EXIT_READY

    status["require"] = args.require
    status["ready"] = code == EXIT_READY

    if args.json:
        print(json.dumps(status, indent=2))
        return code

    if state == "open_project":
        line = (
            f"open_project: {status['product']} {status['version']} — "
            f"project '{status['project']}', timeline "
            f"{status['timeline']!r}, page {status['page']!r}"
        )
    elif state == "open_no_project":
        line = f"open_no_project: {status['product']} {status['version']} — no project loaded"
    else:
        line = f"{state}: {status['detail']}"

    verdict = "READY" if status["ready"] else f"NOT READY (require={args.require})"
    print(f"[preflight] {line}")
    print(f"[preflight] {verdict}")
    if not status["ready"] and state == "closed":
        print("[preflight] Start DaVinci Resolve (Studio) and re-run. Live tests will otherwise")
        print("[preflight] fail with NOT_CONNECTED — or auto-launch Resolve unless")
        print("[preflight] DAVINCI_MCP_NO_AUTOLAUNCH=1 is set.")
    if not status["ready"] and state == "open_no_project":
        print("[preflight] Open (or let the harness create) a project — this run requires one.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
