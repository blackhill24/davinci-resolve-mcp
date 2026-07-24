#!/usr/bin/env python3
"""Live read-only probe of the Resolve Scripting API surface.

Walks the Resolve / ProjectManager / Project / MediaStorage / MediaPool /
Folder / Gallery / Timeline / ColorGroup objects and records which getters
answer and which raise, so a version bump's API surface can be diffed against
a previous run.

Read-only by default: every probe is a getter. The one mutating path — creating
a scratch timeline when the open project has none, so the Timeline getters have
something to answer about — is opt-in behind --allow-mutation and is deleted
again on the way out.

Run:
  .venv/bin/python tests/live_api_probe.py [--allow-mutation] [--out PATH]

Exit codes:
  0  every probe answered
  1  one or more probes raised (see the summary / the JSON report)
  2/3 preflight gate: Resolve not running / scripting unavailable
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SCRATCH_TIMELINE = "ZZ_api_probe_scratch"


def _probe(results, label, fn):
    """Run one getter, record OK/FAIL, and echo a line. Never raises."""
    try:
        value = fn()
    except Exception as exc:
        results[label] = {"status": "FAIL", "error": str(exc)}
        print(f"  x {label} ERROR: {exc}")
        return None
    text = str(value)[:100]
    results[label] = {"status": "OK", "value": text}
    print(f"  . {label} = {text[:80]}")
    return value


def _probe_all(results, probes):
    for label, fn in probes.items():
        _probe(results, label, fn)


def _timeline_probes(tl, extra=False):
    probes = {
        "TL.GetName": lambda: tl.GetName(),
        "TL.GetStartFrame": lambda: tl.GetStartFrame(),
        "TL.GetEndFrame": lambda: tl.GetEndFrame(),
        "TL.GetTrackCount('video')": lambda: tl.GetTrackCount("video"),
        "TL.GetTrackCount('audio')": lambda: tl.GetTrackCount("audio"),
        "TL.GetStartTimecode": lambda: tl.GetStartTimecode(),
        "TL.GetCurrentTimecode": lambda: tl.GetCurrentTimecode(),
        "TL.GetMarkers": lambda: tl.GetMarkers(),
        "TL.GetSetting('')": lambda: tl.GetSetting(""),
        "TL.GetUniqueId": lambda: tl.GetUniqueId(),
    }
    if extra:
        probes["TL.GetNodeGraph"] = lambda: tl.GetNodeGraph() is not None
        probes["TL.GetMarkInOut"] = lambda: tl.GetMarkInOut()
    else:
        probes["TL.GetCurrentVideoItem"] = lambda: tl.GetCurrentVideoItem()
    return probes


def run_probe(allow_mutation: bool = False) -> dict:
    """Probe the live API surface and return the {label: {status, ...}} report.

    Raises RuntimeError if Resolve cannot be reached — callers turn that into a
    non-zero exit. It must not be swallowed: a probe run that connected to
    nothing has proven nothing.
    """
    import DaVinciResolveScript as dvr

    results: dict = {}

    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        raise RuntimeError(
            "scriptapp('Resolve') returned no object — Resolve is not running, or "
            "Preferences > General > 'External scripting using' is not Local."
        )

    print(f"Connected to {resolve.GetProductName()} {resolve.GetVersionString()}")
    print("=" * 60)

    print("\n--- Resolve Object ---")
    _probe_all(results, {
        "Resolve.GetProductName": lambda: resolve.GetProductName(),
        "Resolve.GetVersion": lambda: resolve.GetVersion(),
        "Resolve.GetVersionString": lambda: resolve.GetVersionString(),
        "Resolve.GetCurrentPage": lambda: resolve.GetCurrentPage(),
        "Resolve.GetMediaStorage": lambda: resolve.GetMediaStorage() is not None,
        "Resolve.GetProjectManager": lambda: resolve.GetProjectManager() is not None,
        "Resolve.GetKeyframeMode": lambda: resolve.GetKeyframeMode(),
        "Resolve.Fusion": lambda: resolve.Fusion(),
    })

    print("\n--- ProjectManager Object ---")
    pm = resolve.GetProjectManager()
    _probe_all(results, {
        "PM.GetCurrentProject": lambda: pm.GetCurrentProject() is not None,
        "PM.GetProjectListInCurrentFolder": lambda: pm.GetProjectListInCurrentFolder(),
        "PM.GetFolderListInCurrentFolder": lambda: pm.GetFolderListInCurrentFolder(),
        "PM.GetCurrentFolder": lambda: pm.GetCurrentFolder(),
        "PM.GetCurrentDatabase": lambda: pm.GetCurrentDatabase(),
        "PM.GetDatabaseList": lambda: pm.GetDatabaseList(),
    })

    project = pm.GetCurrentProject()
    if not project:
        raise RuntimeError("Resolve is open but no project is loaded — open a project first.")

    print("\n--- Project Object ---")
    _probe_all(results, {
        "Project.GetName": lambda: project.GetName(),
        "Project.GetMediaPool": lambda: project.GetMediaPool() is not None,
        "Project.GetTimelineCount": lambda: project.GetTimelineCount(),
        "Project.GetSetting('')": lambda: project.GetSetting(""),
        "Project.GetRenderFormats": lambda: project.GetRenderFormats(),
        "Project.GetRenderCodecs": lambda: project.GetRenderCodecs("mp4"),
        "Project.GetCurrentRenderFormatAndCodec": lambda: project.GetCurrentRenderFormatAndCodec(),
        "Project.GetCurrentRenderMode": lambda: project.GetCurrentRenderMode(),
        "Project.GetRenderPresetList": lambda: project.GetRenderPresetList(),
        "Project.GetRenderJobList": lambda: project.GetRenderJobList(),
        "Project.IsRenderingInProgress": lambda: project.IsRenderingInProgress(),
        "Project.GetGallery": lambda: project.GetGallery() is not None,
        "Project.GetUniqueId": lambda: project.GetUniqueId(),
        "Project.GetPresetList": lambda: project.GetPresetList(),
        "Project.GetColorGroupsList": lambda: project.GetColorGroupsList(),
        "Project.RefreshLUTList": lambda: project.RefreshLUTList(),
        "Project.GetQuickExportRenderPresets": lambda: project.GetQuickExportRenderPresets(),
        "Project.GetRenderResolutions": lambda: project.GetRenderResolutions("mp4", "H.264"),
    })

    print("\n--- MediaStorage Object ---")
    ms = resolve.GetMediaStorage()
    _probe_all(results, {
        "MS.GetMountedVolumeList": lambda: ms.GetMountedVolumeList(),
        "MS.GetSubFolderList": lambda: (
            ms.GetSubFolderList(ms.GetMountedVolumeList()[0])
            if ms.GetMountedVolumeList() else "no volumes"
        ),
        "MS.GetFileList": lambda: (
            ms.GetFileList(ms.GetMountedVolumeList()[0])[:5]
            if ms.GetMountedVolumeList() else "no volumes"
        ),
    })

    print("\n--- MediaPool Object ---")
    mp = project.GetMediaPool()
    _probe_all(results, {
        "MP.GetRootFolder": lambda: mp.GetRootFolder() is not None,
        "MP.GetCurrentFolder": lambda: mp.GetCurrentFolder() is not None,
        "MP.GetUniqueId": lambda: mp.GetUniqueId(),
        "MP.GetSelectedClips": lambda: mp.GetSelectedClips(),
    })

    print("\n--- Folder Object ---")
    root = mp.GetRootFolder()
    _probe_all(results, {
        "Folder.GetName": lambda: root.GetName(),
        "Folder.GetClipList": lambda: root.GetClipList(),
        "Folder.GetSubFolderList": lambda: root.GetSubFolderList(),
        "Folder.GetIsFolderStale": lambda: root.GetIsFolderStale(),
        "Folder.GetUniqueId": lambda: root.GetUniqueId(),
    })

    print("\n--- Gallery Object ---")
    gallery = project.GetGallery()
    if gallery:
        _probe_all(results, {
            "Gallery.GetAlbumName": lambda: gallery.GetAlbumName(),
            "Gallery.GetCurrentStillAlbum": lambda: gallery.GetCurrentStillAlbum() is not None,
            "Gallery.GetGalleryStillAlbums": lambda: gallery.GetGalleryStillAlbums(),
            "Gallery.GetGalleryPowerGradeAlbums": lambda: gallery.GetGalleryPowerGradeAlbums(),
        })

        print("\n--- GalleryStillAlbum Object ---")
        album = gallery.GetCurrentStillAlbum()
        if album:
            _probe_all(results, {
                "GSA.GetStills": lambda: album.GetStills(),
                "GSA.GetLabel": lambda: (
                    album.GetLabel(album.GetStills()[0]) if album.GetStills() else "no stills"
                ),
            })

    print("\n--- Timeline Object ---")
    if project.GetTimelineCount() > 0:
        tl = project.GetCurrentTimeline()
        if tl:
            _probe_all(results, _timeline_probes(tl))
    elif not allow_mutation:
        # The open project has no timeline, so the Timeline getters have nothing
        # to answer about. Creating a scratch one is a WRITE into the user's open
        # project — never done implicitly. Re-run with --allow-mutation to cover
        # these probes.
        print("  SKIPPED (project has no timeline; re-run with --allow-mutation to cover)")
        results["TL.*"] = {"status": "SKIP", "value": "no timeline; --allow-mutation not set"}
    else:
        print(f"  creating scratch timeline {SCRATCH_TIMELINE!r} (--allow-mutation)")
        tl = _probe(results, "MP.CreateEmptyTimeline", lambda: mp.CreateEmptyTimeline(SCRATCH_TIMELINE))
        if tl:
            try:
                _probe_all(results, _timeline_probes(tl, extra=True))
            finally:
                try:
                    if mp.DeleteTimelines([tl]):
                        print(f"  removed scratch timeline {SCRATCH_TIMELINE!r}")
                    else:
                        print(
                            f"  WARNING: could not remove scratch timeline "
                            f"{SCRATCH_TIMELINE!r} — delete it by hand"
                        )
                except Exception as exc:
                    print(
                        f"  WARNING: removing scratch timeline {SCRATCH_TIMELINE!r} "
                        f"raised {exc!r} — delete it by hand"
                    )

    print("\n--- ColorGroup Object ---")
    groups = _probe(
        results, "Project.GetColorGroupsList (for ColorGroup)", lambda: project.GetColorGroupsList()
    )
    if groups:
        cg = groups[0]
        _probe_all(results, {
            "CG.GetName": lambda: cg.GetName(),
            "CG.GetPreClipNodeGraph": lambda: cg.GetPreClipNodeGraph() is not None,
            "CG.GetPostClipNodeGraph": lambda: cg.GetPostClipNodeGraph() is not None,
        })
    else:
        print("  no color groups in this project")

    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--allow-mutation",
        action="store_true",
        help="permit creating (and deleting) a scratch timeline when the project has none",
    )
    parser.add_argument(
        "--out",
        default=os.path.join(tempfile.gettempdir(), "resolve-live-api-probe.json"),
        help="where to write the JSON report (default: a temp-dir path, never the repo)",
    )
    args = parser.parse_args(argv)

    try:
        results = run_probe(allow_mutation=args.allow_mutation)
    except RuntimeError as exc:
        print(f"FATAL: {exc}")
        return 1

    ok = sum(1 for v in results.values() if v["status"] == "OK")
    fail = sum(1 for v in results.values() if v["status"] == "FAIL")
    skip = sum(1 for v in results.values() if v["status"] == "SKIP")

    print("\n" + "=" * 60)
    print(f"RESULTS: {ok} answered, {fail} raised, {skip} skipped, {ok + fail + skip} total")
    if ok + fail:
        print(f"Answer rate: {ok / (ok + fail) * 100:.1f}%")

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    print(f"\nDetailed results saved to {args.out}")

    return 1 if fail else 0


if __name__ == "__main__":
    from preflight import gate

    gate("project")
    raise SystemExit(main())
