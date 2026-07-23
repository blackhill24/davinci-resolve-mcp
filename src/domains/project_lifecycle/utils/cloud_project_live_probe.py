#!/usr/bin/env python3
"""Live ProjectManager CloudProject family probe (issue #25, epic #26 stage 5).

Requires a Blackmagic Cloud account with a disposable test library — see
``tests/cloud-test-setup.md``. Skips cleanly (does not fail) when the
required environment is absent, so it never breaks the offline/live suites
for contributors without a cloud account configured.

Known limitation baked into the design: there is no scripted way to delete a
cloud project from Blackmagic Cloud storage (no ``DeleteCloudProject`` /
``GetCloudProjectList`` — see ``api_truth`` "Cloud project enumeration /
export / user management"). This probe removes the *local* project reference
it creates via ``safe_project_delete``, but any cloud-side copy must be
deleted manually from the Blackmagic Cloud account after each run.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.domains.timeline_edit.utils.timeline_kernel_probe import ProbeRecorder, render_markdown_report, utc_timestamp

ENV_MEDIA_PATH = "RESOLVE_CLOUD_PROJECT_MEDIA_PATH"
ENV_PREFIX = "RESOLVE_CLOUD_TEST_PREFIX"
ENV_IMPORT_FILE = "RESOLVE_CLOUD_TEST_IMPORT_FILE"
ENV_RESTORE_FOLDER = "RESOLVE_CLOUD_TEST_RESTORE_FOLDER"

_SYNC_MODES = ["none", "proxy_only", "proxy_and_orig"]


def _skip_report(reason: str) -> Dict[str, Any]:
    return {
        "metadata": {
            "title": "Cloud Project Probe",
            "timestamp_utc": utc_timestamp(),
            "skipped": True,
            "reason": reason,
        },
        "artifacts": {},
        "counts": {"skipped": 1},
        "records": [],
    }


def _record_tool_result(recorder: ProbeRecorder, category: str, name: str, result: Dict[str, Any]) -> None:
    if not isinstance(result, dict):
        recorder.record(category, name, "error", details={"reason": "non-dict result", "result": repr(result)})
        return
    if result.get("error"):
        recorder.record(category, name, "error", details={"reason": result.get("error")}, evidence=result)
        return
    success = result.get("success")
    if success is False:
        recorder.record(category, name, "partially_supported", details={"reason": "success returned false"}, evidence=result)
        return
    verified = result.get("verified")
    if verified is False:
        recorder.record(category, name, "partially_supported", details={"reason": "readback contradiction — API reported success but verification failed"}, evidence=result)
        return
    recorder.record(category, name, "supported", evidence=result)


def run_probe(server, output_dir: Path, *, keep_open: bool = False) -> Dict[str, Any]:
    """Run the live cloud-project probe. Returns a skip report if
    RESOLVE_CLOUD_PROJECT_MEDIA_PATH is not set."""
    project_media_path = os.environ.get(ENV_MEDIA_PATH)
    if not project_media_path:
        return _skip_report(
            f"{ENV_MEDIA_PATH} is not set — no Blackmagic Cloud test library "
            "configured. See tests/cloud-test-setup.md.",
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = os.environ.get(ENV_PREFIX, "_mcp_cloud_probe")
    timestamp = int(time.time())
    recorder = ProbeRecorder()
    cleanup_projects: list = []
    delete_results: Dict[str, Any] = {}

    metadata = {
        "title": "Cloud Project Probe",
        "timestamp_utc": utc_timestamp(),
        "project_media_path": project_media_path,
        "skipped": False,
    }

    try:
        # 5.2: sweep all three CLOUD_SYNC_* modes via create, and check the
        # documented "only 1st load honours all settings" quirk.
        first_created_name: Optional[str] = None
        for mode in _SYNC_MODES:
            project_name = f"{prefix}_{mode}_{timestamp}"
            create_result = server.project_manager_cloud("create", {
                "settings": {
                    "project_name": project_name,
                    "project_media_path": project_media_path,
                    "sync_mode": mode,
                },
            })
            _record_tool_result(recorder, "cloud_create", f"create[{mode}]", create_result)
            if create_result.get("success"):
                cleanup_projects.append(project_name)
                if first_created_name is None:
                    first_created_name = project_name

        if first_created_name:
            # First load on this system should honour project_media_path + sync_mode;
            # a second load of the same project should honour only project_name
            # (docs/reference/resolve_scripting_api.txt lines 597-598).
            load_1 = server.project_manager_cloud("load", {
                "settings": {
                    "project_name": first_created_name,
                    "project_media_path": project_media_path,
                    "sync_mode": "proxy_only",
                },
            })
            _record_tool_result(recorder, "cloud_load", "load_first", load_1)
            load_2 = server.project_manager_cloud("load", {
                "settings": {
                    "project_name": first_created_name,
                    "project_media_path": project_media_path,
                    "sync_mode": "proxy_and_orig",
                },
            })
            _record_tool_result(recorder, "cloud_load", "load_second_settings_ignored", load_2)
        else:
            recorder.record("cloud_load", "load_quirk", "not_applicable", details={"reason": "no cloud project was created to load"})

        # 5.3: advisory-bool readback guard on import/restore — only run if the
        # caller supplied a real file/folder to import/restore from.
        import_file = os.environ.get(ENV_IMPORT_FILE)
        if import_file:
            import_result = server.project_manager_cloud("import_project", {
                "path": import_file,
                "settings": {"project_media_path": project_media_path},
            })
            _record_tool_result(recorder, "cloud_import_restore", "import_project", import_result)
        else:
            recorder.record("cloud_import_restore", "import_project", "not_applicable", details={"reason": f"{ENV_IMPORT_FILE} not set"})

        restore_folder = os.environ.get(ENV_RESTORE_FOLDER)
        if restore_folder:
            restore_result = server.project_manager_cloud("restore", {
                "folder_path": restore_folder,
                "settings": {"project_media_path": project_media_path},
            })
            _record_tool_result(recorder, "cloud_import_restore", "restore", restore_result)
        else:
            recorder.record("cloud_import_restore", "restore", "not_applicable", details={"reason": f"{ENV_RESTORE_FOLDER} not set"})

        if keep_open:
            print(f"LEFT CLOUD PROJECTS OPEN FOR INSPECTION: {cleanup_projects}")
            cleanup_projects = []
    finally:
        if not keep_open:
            for name in reversed(cleanup_projects):
                # Removes the local project reference only — the cloud-side copy
                # has no scripted delete (api_truth: "Cloud project enumeration /
                # export / user management") and must be removed manually from
                # the Blackmagic Cloud account.
                delete_results[name] = server.project_manager("safe_project_delete", {"name": name, "close_current": True})
                print(f"Deleted local reference to {name}: {delete_results[name]}")

    metadata["cleanup"] = delete_results
    metadata["manual_cleanup_required"] = bool(cleanup_projects) or bool(delete_results)
    report = recorder.to_report(
        metadata,
        {
            "json": str(output_dir / "cloud-project-probe.json"),
            "markdown": str(output_dir / "cloud-project-probe.md"),
        },
    )
    json_path = output_dir / "cloud-project-probe.json"
    markdown_path = output_dir / "cloud-project-probe.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")
    print(f"Wrote JSON report: {json_path}")
    print(f"Wrote Markdown report: {markdown_path}")
    print(f"Counts: {json.dumps(report['counts'], sort_keys=True)}")
    if delete_results:
        print(
            "NOTE: local project references were deleted, but any cloud-side "
            "copies must be removed manually from the Blackmagic Cloud account "
            "— there is no scripted delete for cloud projects.",
        )
    return report
