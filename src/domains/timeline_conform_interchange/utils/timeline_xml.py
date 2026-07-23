"""Sanitize FCP7 (xmeml) / FCPXML timeline interchange files so DaVinci Resolve's
scripting API can import them with media linked.

Resolve's GUI "Load XML" importer tolerates two things that the scripting API's
``MediaPool.ImportTimelineFromFile`` does NOT — either one makes the API abort the
entire import (it returns ``None`` and imports nothing, leaving any timeline it does
create fully offline):

1. **Clipitems whose media file is missing on disk.** The GUI creates an offline
   placeholder and prompts to relink; the API just bails.
2. **Generator clipitems** — slugs / solids / colour mattes such as Premiere's
   "Universal Counting Leader" or "Black Video", which carry a ``<file>`` element
   with no ``<pathurl>``. The API bails on these too.

This module rewrites the XML, removing those offending clipitems while leaving
everything else (cuts, transitions, retimes, filters, every track) byte-for-byte
intact. Because clipitem positions in xmeml are explicit (``<start>``/``<end>``),
removing a clip leaves a gap at the right place rather than shifting the edit. The
result imports through the API with source clips auto-imported and linked, matching
the GUI's behaviour.

Validated live on DaVinci Resolve Studio 21 against Premiere-exported xmeml v4
conform XMLs.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

# NOTE: FRAME-based verification of a proposed relink (sample the candidate, compare
# it structurally against a reference render) is NOT done here — that whole surface
# lives in the Node davinci-resolve-advanced MCP (`conform relink_scalefix` /
# `conform qc`). This module only rewrites paths by NAME, and says so: a match is
# reported with the tier that produced it, and anything ambiguous is reported rather
# than guessed. Plain XML sanitize — the default and only critical path — does no
# matching at all.


def _pathurl_to_disk(pathurl: Optional[str]) -> Optional[str]:
    """Convert an xmeml ``<pathurl>`` (file URL) to a local filesystem path."""
    if not pathurl:
        return None
    p = pathurl.strip()
    for prefix in ("file://localhost", "file://"):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    return urllib.parse.unquote(p)


def _media_exists(pathurl: Optional[str]) -> bool:
    disk = _pathurl_to_disk(pathurl)
    return bool(disk) and os.path.exists(disk)


def _disk_to_pathurl(disk_path: str) -> str:
    """Build a Premiere-style file URL from a local path (matches xmeml convention)."""
    return "file://localhost" + urllib.parse.quote(disk_path)


def _full_file_elements(seq: ET.Element) -> Dict[str, ET.Element]:
    """Map ``file id`` -> the full ``<file>`` element (the one carrying the pathurl)."""
    out: Dict[str, ET.Element] = {}
    for f in seq.iter("file"):
        fid = f.get("id")
        if fid and len(list(f)) and f.find("pathurl") is not None and fid not in out:
            out[fid] = f
    return out


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_stem(stem: str) -> str:
    """Fold a filename stem for the loosest match tier: lowercase, alphanumerics only.

    ``A001_C003_0704AB.mov`` and ``a001-c003-0704ab.mxf`` normalize alike; unrelated
    names still do not collide, because every alphanumeric character is preserved.
    """
    return _NORMALIZE_RE.sub("", stem.lower())


def scan_candidates(search_roots: List[str], max_files: int = 50000,
                    max_seconds: float = 20.0,
                    max_depth: Optional[int] = None) -> Dict[str, Any]:
    """Walk ``search_roots`` once, under hard bounds, collecting candidate media paths.

    Bounded the same way as the live ``build_relink_plan`` scan: a runaway root (a
    network mount, ``/``) stops at ``max_files``/``max_seconds`` and reports
    ``truncated`` rather than hanging the import. One walk serves every missing
    reference, so cost is independent of how many clips are offline.
    """
    started = time.monotonic()
    max_files = max(1, int(max_files))
    max_seconds = max(0.1, float(max_seconds))
    candidates: List[str] = []
    roots_missing: List[str] = []
    scanned = 0
    truncated = False

    for root in search_roots:
        if not isinstance(root, str) or not os.path.isdir(root):
            roots_missing.append(root)
            continue
        root_depth = len(os.path.abspath(root).rstrip(os.sep).split(os.sep))
        for dirpath, dirnames, filenames in os.walk(root):
            if max_depth is not None:
                depth = len(os.path.abspath(dirpath).rstrip(os.sep).split(os.sep)) - root_depth
                if depth >= max_depth:
                    dirnames[:] = []
            for name in filenames:
                candidates.append(os.path.join(dirpath, name))
            scanned += len(filenames)
            if scanned >= max_files or time.monotonic() - started >= max_seconds:
                truncated = True
                break
        if truncated:
            break

    return {"candidates": candidates, "scanned": scanned,
            "truncated": truncated, "roots_missing": roots_missing}


# Match tiers, strictest first. Each maps a filename to the key it matches on; the
# first tier that yields any candidate decides the outcome, so a weaker tier can
# never override an exact hit. Confidences are TIER LABELS, not learned scores.
_MATCH_TIERS = (
    ("exact", 1.0, lambda name: name),
    ("case_insensitive", 0.95, lambda name: name.lower()),
    ("ext_agnostic", 0.9, lambda name: os.path.splitext(name)[0].lower()),
    ("normalized", 0.8, lambda name: _normalize_stem(os.path.splitext(name)[0])),
)


def match_references(refs: List[Dict[str, Any]], candidates: List[str]) -> Dict[str, Any]:
    """Match each missing reference to on-disk candidates by NAME, tier by tier.

    Returns ``{"items": [...]}`` parallel to ``refs``, each item
    ``{status, method, confidence, assetId?, assetIds?}``:

    - ``matched`` — exactly one candidate in the strictest tier that hit.
    - ``ambiguous`` — several candidates hit; every path is reported and NOTHING is
      chosen. Guessing between two files with the same name is how a conform silently
      goes wrong, so this is a deliberate refusal, not a limitation.
    - ``unmatched`` — no tier hit.
    """
    indexes: List[Dict[str, List[str]]] = []
    for _, _, key_of in _MATCH_TIERS:
        index: Dict[str, List[str]] = {}
        for path in candidates:
            index.setdefault(key_of(os.path.basename(path)), []).append(path)
        indexes.append(index)

    items: List[Dict[str, Any]] = []
    for ref in refs:
        name = ref.get("name") or ""
        item = {"status": "unmatched", "method": None, "confidence": 0.0}
        for (method, confidence, key_of), index in zip(_MATCH_TIERS, indexes):
            hits = sorted(set(index.get(key_of(name), [])))
            if not hits:
                continue
            if len(hits) == 1:
                item = {"status": "matched", "method": method,
                        "confidence": confidence, "assetId": hits[0]}
            else:
                item = {"status": "ambiguous", "method": method,
                        "confidence": confidence, "assetIds": hits}
            break
        items.append(item)
    return {"items": items}


def _relink_missing_files(seq: ET.Element, file_elems: Dict[str, ET.Element],
                          search_roots: List[str], min_confidence: float,
                          scan_caps: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Match every missing file definition against on-disk candidates and rewrite its
    ``<pathurl>`` in place when exactly one confident match is found.

    Returns {relinked, ambiguous, scan} for the report. Mutates ``seq``.
    """
    fids: List[str] = []
    missing_refs: List[Dict[str, Any]] = []
    for fid, elem in file_elems.items():
        purl = elem.findtext("pathurl")
        if purl is None or _media_exists(purl):
            continue
        disk = _pathurl_to_disk(purl)
        name = os.path.basename(disk) if disk else (elem.findtext("name") or "")
        fids.append(fid)
        missing_refs.append({"name": name, "old_path": disk})

    if not missing_refs:
        return {"relinked": [], "ambiguous": [], "scan": None}

    caps = scan_caps or {}
    scan = scan_candidates(search_roots, **caps)
    conform = match_references(missing_refs, scan["candidates"])

    relinked: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    for fid, ref, item in zip(fids, missing_refs, conform["items"]):
        if item["status"] == "matched" and item["confidence"] >= min_confidence:
            new_path = item["assetId"]
            file_elems[fid].find("pathurl").text = _disk_to_pathurl(new_path)
            relinked.append({"name": ref["name"], "old_path": ref["old_path"],
                             "new_path": new_path, "method": item["method"],
                             "confidence": item["confidence"]})
        elif item["status"] == "ambiguous":
            ambiguous.append({"name": ref["name"], "old_path": ref["old_path"],
                              "candidates": item.get("assetIds", []),
                              "method": item["method"]})

    return {"relinked": relinked, "ambiguous": ambiguous,
            "scan": {"scanned": scan["scanned"], "truncated": scan["truncated"],
                     "roots_missing": scan["roots_missing"]}}


def _build_file_map(seq: ET.Element) -> Dict[str, str]:
    """Map ``file id`` -> ``pathurl`` from full ``<file>`` definitions.

    Premiere defines a ``<file>`` fully on first use, then references it by id
    (``<file id="file-37"/>``). We need the full definitions to resolve the
    pathurl of a reference-only clipitem.
    """
    file_map: Dict[str, str] = {}
    for f in seq.iter("file"):
        fid = f.get("id")
        if not fid:
            continue
        # A full definition has child elements and a pathurl; a bare reference does not.
        if len(list(f)) and f.find("pathurl") is not None:
            file_map[fid] = f.findtext("pathurl")
    return file_map


def _clip_pathurl(clipitem: ET.Element, file_map: Dict[str, str]) -> Optional[str]:
    fref = clipitem.find("file")
    if fref is None:
        return None
    inline = fref.findtext("pathurl")
    if inline:
        return inline
    fid = fref.get("id")
    return file_map.get(fid) if fid else None


def analyze_timeline_xml(path: str) -> Dict[str, Any]:
    """Inspect a timeline XML without modifying it.

    Returns a report dict describing how many clipitems would be kept vs. removed
    (missing media / generators). Pure parsing — does not touch Resolve.
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = fh.read()
    root = ET.fromstring(raw)
    seq = root.find("sequence")
    if seq is None:
        raise ValueError("No <sequence> element found; not an FCP7 xmeml timeline")
    file_map = _build_file_map(seq)

    kept = 0
    missing: List[Dict[str, str]] = []
    generators: List[Dict[str, str]] = []
    total = 0
    for media in seq.findall("media"):
        for av in list(media):  # <video> / <audio>
            track_type = av.tag
            for track in av.findall("track"):
                for ci in track.findall("clipitem"):
                    total += 1
                    fref = ci.find("file")
                    name = ci.findtext("name") or "(unnamed)"
                    if fref is None:
                        generators.append({"name": name, "track_type": track_type,
                                           "reason": "no-file"})
                        continue
                    pathurl = _clip_pathurl(ci, file_map)
                    if pathurl is None:
                        generators.append({"name": name, "track_type": track_type,
                                           "reason": "no-pathurl"})
                    elif not _media_exists(pathurl):
                        missing.append({"name": name, "track_type": track_type,
                                        "path": _pathurl_to_disk(pathurl)})
                    else:
                        kept += 1

    seq_name = seq.findtext("name") or os.path.splitext(os.path.basename(path))[0]
    return {
        "timeline_name": seq_name,
        "clip_total": total,
        "kept": kept,
        "missing_media": missing,
        "missing_media_count": len(missing),
        "generators": generators,
        "generator_count": len(generators),
        "needs_sanitize": bool(missing or generators),
    }


def sanitize_timeline_xml(path: str, out_dir: Optional[str] = None,
                          search_roots: Optional[List[str]] = None,
                          min_confidence: float = 0.7,
                          scan_caps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Write a sanitized copy of the timeline XML to a temp file.

    Removes clipitems that reference missing media or are generators (no pathurl),
    preserving everything else. Returns ``{temp_path, report}`` where ``report`` is
    the same shape as :func:`analyze_timeline_xml` plus ``output_path``.

    When ``search_roots`` is given, each clip whose media is missing at its original
    path is first matched by name against media found under those roots (tiers:
    exact / case-insensitive / ext-agnostic / normalized stem — see
    :func:`match_references`); a UNIQUE match at/above ``min_confidence`` rewrites
    the clip's ``<pathurl>`` so it relinks instead of being dropped. Several matches
    in the same tier are reported under ``ambiguous`` and nothing is rewritten.
    Clips that still cannot be resolved (and generators) are removed. ``scan_caps``
    overrides the candidate-scan bounds.

    Matching is by NAME only. To confirm a relink is the right *picture* — sample the
    frame and compare it against a reference render — use the advanced MCP's
    ``conform relink_scalefix`` / ``conform qc``.
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = fh.read()
    root = ET.fromstring(raw)
    seq = root.find("sequence")
    if seq is None:
        raise ValueError("No <sequence> element found; not an FCP7 xmeml timeline")

    relink = {"relinked": [], "ambiguous": [], "scan": None}
    if search_roots:
        relink = _relink_missing_files(seq, _full_file_elements(seq), search_roots,
                                       min_confidence, scan_caps)

    file_map = _build_file_map(seq)

    kept = 0
    missing: List[Dict[str, str]] = []
    generators: List[Dict[str, str]] = []
    total = 0
    for media in seq.findall("media"):
        for av in list(media):
            track_type = av.tag
            for track in av.findall("track"):
                for ci in list(track.findall("clipitem")):
                    total += 1
                    name = ci.findtext("name") or "(unnamed)"
                    fref = ci.find("file")
                    if fref is None:
                        generators.append({"name": name, "track_type": track_type,
                                           "reason": "no-file"})
                        track.remove(ci)
                        continue
                    pathurl = _clip_pathurl(ci, file_map)
                    if pathurl is None:
                        generators.append({"name": name, "track_type": track_type,
                                           "reason": "no-pathurl"})
                        track.remove(ci)
                    elif not _media_exists(pathurl):
                        missing.append({"name": name, "track_type": track_type,
                                        "path": _pathurl_to_disk(pathurl)})
                        track.remove(ci)
                    else:
                        kept += 1

    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="mcp_xml_import_")
    else:
        os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    out_path = os.path.join(out_dir, f"{base}.sanitized.xml")
    body = ET.tostring(root, encoding="unicode")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        fh.write(body)
        fh.write("\n")

    seq_name = seq.findtext("name") or base
    report = {
        "output_path": out_path,
        "timeline_name": seq_name,
        "clip_total": total,
        "kept": kept,
        "removed_total": len(missing) + len(generators),
        "missing_media": missing,
        "missing_media_count": len(missing),
        "generators": generators,
        "generator_count": len(generators),
        "relinked": relink["relinked"],
        "relinked_count": len(relink["relinked"]),
        "ambiguous": relink["ambiguous"],
        "ambiguous_count": len(relink["ambiguous"]),
        "scan": relink["scan"],
    }
    return report
