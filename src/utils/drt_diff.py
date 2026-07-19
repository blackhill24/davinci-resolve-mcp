"""Raw structural diff of two Resolve container archives (.drt / .drp).

Unlike the Node ``drp-format`` ``diff`` op — which is *semantic*: it keys on
clip DbIds and detects grade changes by hashing per-clip ``<Body>`` blobs — this
is a *raw discovery* differ. It unzips both archives and reports which inner
entries were added, removed, or changed, and for changed text entries a
line-level diff. Its job is reverse-engineering an **unknown** encoding via the
codec's documented export-diff / ground-truth method: edit one value in Resolve
(e.g. an A2 clip's volume automation), export the ``.drt``, and diff it against
the baseline to see exactly which bytes moved. That is the load-bearing step for
issue #14 (drt volume automation) — the semantic diff can't help because it only
knows about grades, so a media-less field like clip volume is invisible to it.

Pure and offline: the only inputs are two archive paths on disk. No Resolve, no
Node, no network.
"""

from __future__ import annotations

import difflib
import hashlib
import posixpath
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple

# A basename Resolve generates from a uuid/hex id — the part that changes on every
# export (e.g. SeqContainer/a6eaaacb-b35c-4afe-....xml). Only entries whose NAME is
# machine-generated like this are eligible for rename-pairing; human-named entries
# (project.xml, gone.xml) are left as honest add/remove.
_UUIDISH = re.compile(r"^[0-9a-fA-F]{8,}(-[0-9a-fA-F]{4,})+$")

# Lines that differ ONLY because Resolve regenerates identifiers on every export —
# DbId / <Sequence> uuids / thumbnail ids — not a content edit. Verified live on
# Resolve 21: a no-op re-export still churns these, so hunting for a specific value
# edit (issue #14 clip-volume ground truth) means filtering them out first.
# <SubType> carries a garbage int that flips between exports of the SAME timeline
# (verified: 1694526720 -> 3342389 with no edit) — its value is uninitialized, not
# a uuid, so the pattern below must name it explicitly or it reads as a false edit.
RESOLVE_ID_CHURN = re.compile(
    r"(DbId=|<Sequence>|<SubType>|<BtThumnail\b|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-)")


def significant_lines(change: Dict[str, Any]) -> Dict[str, List[str]]:
    """The added/removed lines of a text change with identifier churn removed.

    Pass a ``changed`` entry from :func:`diff_containers`; returns
    ``{"added": [...], "removed": [...]}`` holding only lines that survive the
    :data:`RESOLVE_ID_CHURN` filter — i.e. candidate real edits. Empty lists mean
    the two exports differ only by regenerated ids (a no-op edit).
    """
    def _keep(lines: List[str]) -> List[str]:
        return [ln for ln in lines if not RESOLVE_ID_CHURN.search(ln)]
    return {
        "added": _keep(change.get("added_lines", [])),
        "removed": _keep(change.get("removed_lines", [])),
    }

# A changed text entry longer than this many changed lines is summarized rather
# than dumped whole — a re-serialized XML often reflows unrelated whitespace, and
# the point is to spot the *field* that carries the edit, not drown in reflow.
DEFAULT_MAX_DIFF_LINES = 400


def _read_entries(path: str) -> Dict[str, bytes]:
    """{entry name -> raw bytes} for every file member of a zip archive.

    Directories are skipped. Raises ``zipfile.BadZipFile`` if ``path`` is not a
    zip container (a ``.drt``/``.drp`` always is — export-then-modify preserves
    that), so callers can surface a clean error instead of a stack trace.
    """
    entries: Dict[str, bytes] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            entries[info.filename] = zf.read(info.filename)
    return entries


def _looks_texty(data: bytes) -> bool:
    """Heuristic: is this entry human-diffable text (XML) vs. an opaque blob?"""
    if b"\x00" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _text_delta(
    name: str, before: bytes, after: bytes, *, max_lines: int
) -> Dict[str, Any]:
    """Line-level delta for one changed text entry.

    Returns the added/removed line lists (the discovery signal — the field that
    carries the edit shows up here) plus a capped unified diff for eyeballing.
    """
    a_lines = before.decode("utf-8", "replace").splitlines()
    b_lines = after.decode("utf-8", "replace").splitlines()
    added: List[str] = []
    removed: List[str] = []
    for line in difflib.unified_diff(a_lines, b_lines, lineterm=""):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    unified = list(
        difflib.unified_diff(
            a_lines, b_lines, fromfile=f"a/{name}", tofile=f"b/{name}", lineterm=""
        )
    )
    truncated = len(unified) > max_lines
    return {
        "kind": "text",
        "added_lines": added,
        "removed_lines": removed,
        "unified_diff": unified[:max_lines],
        "unified_diff_truncated": truncated,
    }


def _binary_delta(before: bytes, after: bytes) -> Dict[str, Any]:
    """Byte-level delta for one changed opaque entry (protobuf/media blob)."""
    return {
        "kind": "binary",
        "size_before": len(before),
        "size_after": len(after),
        "sha256_before": hashlib.sha256(before).hexdigest(),
        "sha256_after": hashlib.sha256(after).hexdigest(),
    }


def _rename_key(name: str) -> Optional[Tuple[str, str]]:
    """Pairing key for an entry whose FILENAME is unstable across exports.

    A real Resolve ``.drt`` names the timeline container ``SeqContainer/<uuid>.xml``
    with a FRESH uuid every export (verified live on Resolve 21) — so the same
    logical timeline appears as one "added" + one "removed" entry under naive name
    matching. An entry is rename-pairable only when its basename is uuid/hex-like
    (machine-generated, hence the unstable part); keying such entries on
    ``(parent dir, extension)`` lets a lone add/remove pair in the same folder be
    recognized as the same entry, renamed, and diffed by content. Human-named
    entries return ``None`` and stay honest add/remove.
    """
    stem, ext = posixpath.splitext(posixpath.basename(name))
    if not _UUIDISH.match(stem):
        return None
    return (posixpath.dirname(name), ext)


def diff_containers(
    path_before: str,
    path_after: str,
    *,
    max_diff_lines: int = DEFAULT_MAX_DIFF_LINES,
    name_filter: Optional[str] = None,
    pair_renamed: bool = True,
) -> Dict[str, Any]:
    """Raw entry-level diff of two ``.drt``/``.drp`` archives.

    ``name_filter`` (a substring) narrows the compared entries — pass
    ``"SeqContainer"`` to focus on the timeline XML where clip volume /
    automation lives and skip project-chrome churn.

    ``pair_renamed`` (default on) pairs a lone added+removed entry that share a
    :func:`_structural_key` — Resolve's per-export uuid container filenames would
    otherwise read as unrelated add/remove instead of a content change. Each such
    pair is reported under ``changed`` with a ``renamed_from`` field.

    Returns::

        {
          "added":   [entry names only in AFTER],
          "removed": [entry names only in BEFORE],
          "changed": [{"name", ...text or binary delta...}],
          "unchanged": <count>,
          "summary": "<one-line human summary>",
        }

    or ``{"error": ...}`` if either path is not a readable zip container.
    """
    try:
        before = _read_entries(path_before)
        after = _read_entries(path_after)
    except (OSError, zipfile.BadZipFile) as exc:
        return {"error": f"not a readable container archive: {type(exc).__name__}: {exc}"}

    def _keep(name: str) -> bool:
        return name_filter is None or name_filter in name

    before_names = {n for n in before if _keep(n)}
    after_names = {n for n in after if _keep(n)}

    added = sorted(after_names - before_names)
    removed = sorted(before_names - after_names)

    changed: List[Dict[str, Any]] = []
    unchanged = 0

    def _record_change(name: str, b: bytes, a: bytes, *, renamed_from: Optional[str] = None) -> None:
        if _looks_texty(b) and _looks_texty(a):
            delta = _text_delta(name, b, a, max_lines=max_diff_lines)
        else:
            delta = _binary_delta(b, a)
        entry = {"name": name, **delta}
        if renamed_from is not None:
            entry["renamed_from"] = renamed_from
        changed.append(entry)

    for name in sorted(before_names & after_names):
        b, a = before[name], after[name]
        if b == a:
            unchanged += 1
            continue
        _record_change(name, b, a)

    # Pair uuid-renamed entries (one add + one remove sharing a structural key)
    # so a per-export container rename reads as a content diff, not add+remove.
    if pair_renamed and added and removed:
        rem_by_key: Dict[Tuple[str, str], List[str]] = {}
        for r in removed:
            key = _rename_key(r)
            if key is not None:
                rem_by_key.setdefault(key, []).append(r)
        paired_removed = set()
        still_added: List[str] = []
        for a_name in added:
            key = _rename_key(a_name)
            bucket = rem_by_key.get(key) if key is not None else None
            if bucket and len(bucket) == 1:
                r_name = bucket.pop(0)
                paired_removed.add(r_name)
                if before[r_name] == after[a_name]:
                    unchanged += 1
                else:
                    _record_change(a_name, before[r_name], after[a_name], renamed_from=r_name)
            else:
                still_added.append(a_name)
        added = sorted(still_added)
        removed = sorted(n for n in removed if n not in paired_removed)

    changed.sort(key=lambda c: c["name"])
    summary = (
        f"{len(added)} added, {len(removed)} removed, {len(changed)} changed, "
        f"{unchanged} unchanged"
        + (f" (filter={name_filter!r})" if name_filter else "")
    )
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "summary": summary,
    }
