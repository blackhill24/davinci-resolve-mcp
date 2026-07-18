#!/usr/bin/env python3
"""ICM freshness detector.

Two jobs, both deterministic and token-free:
  (no args)   CHECK  - compares each workspace against .icm/manifest.json and
                       prints a *triaged* warning. Registered as a global
                       SessionStart hook; silent unless something drifted.
  --update    UPDATE - re-records every fingerprint + file list. Run after nav
                       docs are brought in line with the code (init/apply/sync,
                       or the in-flow upkeep rule scaffolded into projects).

Flags:
  --strict    with CHECK: exit 1 when drift or a manifest problem is found, so
              CI can gate on stale nav docs. Never add it to the SessionStart
              hook - the hook must never fail a session (default exit is 0).
  --version   print the checker version and exit.

Workspace paths from the manifest are validated: anything absolute, or
resolving outside the project root, is ignored with a warning - a malformed
(or hostile) manifest can never send this hook walking unrelated directories.
Manifest writes are atomic (temp file + rename), and --update stamps the
checker version so an outdated project-local copy can be detected.

Drift triage (structural vs content):
  structural - files added/removed/renamed since last sync. Routing tables are
               genuinely stale; the warning names the files (capped).
  content    - files edited in place only. Routing tables bind to structure,
               so they are usually still valid; flagged softly.

Fingerprints are content-addressed via git when available, so mtime churn from
branch switches, fresh clones, or `touch` never flags drift - only real content
or structure changes do. Non-git projects fall back to path+size+mtime.
Nav docs themselves (CLAUDE.md / CONTEXT.md / METHODOLOGY.md / IDENTITY.md) are
excluded from fingerprints: editing the docs is never drift - only the code
moving underneath them is.

Exits 0 silently when there is no .icm/manifest.json in (or above) the cwd, so
it is safe to register globally across every project.

Scaffolded projects carry their own copy at .icm/drift-check.py so that
collaborators and CI can check/update without installing the icm skill; when
that copy exists, messages recommend the project-local path.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT_VERSION = "2.1.0"

# Directory names never worth fingerprinting (noise / huge / regenerated).
SKIP_DIRS = {
    ".git", ".icm", "node_modules", ".next", "dist", "build", "out",
    "__pycache__", ".venv", "venv", ".turbo", "coverage", ".cache",
    ".svelte-kit", "target", ".pytest_cache", ".mypy_cache", ".idea",
    ".vscode", ".DS_Store",
}
# Nav docs: excluded from fingerprints (see module docstring).
NAV_BASENAMES = {"CLAUDE.md", "CONTEXT.md", "METHODOLOGY.md", "IDENTITY.md"}
MAX_NAMED = 5          # max file names printed per workspace per change kind
MANIFEST_VERSION = 2


def _skippable(rel: str) -> bool:
    p = Path(rel)
    return any(part in SKIP_DIRS for part in p.parts) or p.name in NAV_BASENAMES


def _git(base: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(base), *args],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout


def _git_lines(base: Path, *args: str) -> list[str]:
    # "-z" must precede any "--" pathspec or git reads it as a filename
    return [ln for ln in _git(base, args[0], "-z", *args[1:]).split("\0") if ln]


def in_git_worktree(base: Path) -> bool:
    try:
        return _git(base, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except Exception:
        return False


def git_snapshot(base: Path, rel: str) -> dict[str, str] | None:
    """{relpath: token} under base/rel. Token is the git blob hash for clean
    tracked files (content-addressed, mtime-immune) and size:mtime for
    modified/untracked ones."""
    if not (base / rel).exists():
        return None
    pathspec = [] if rel in (".", "") else ["--", rel]
    tracked: dict[str, str] = {}
    for line in _git_lines(base, "ls-files", "-s", *pathspec):
        meta, path = line.split("\t", 1)
        tracked[path] = meta.split()[1]
    # Refresh stat cache so touched-but-unchanged files don't read as modified.
    subprocess.run(
        ["git", "-C", str(base), "update-index", "-q", "--refresh", "--unmerged"],
        capture_output=True, timeout=60,
    )
    modified = set(_git_lines(base, "ls-files", "-m", *pathspec))
    deleted = set(_git_lines(base, "ls-files", "-d", *pathspec))
    untracked = set(_git_lines(base, "ls-files", "-o", "--exclude-standard", *pathspec))

    snap: dict[str, str] = {}
    for path in (set(tracked) - deleted) | untracked:
        if _skippable(path):
            continue
        if path in modified or path in untracked:
            try:
                st = (base / path).stat()
            except OSError:
                continue
            snap[path] = f"{st.st_size}:{st.st_mtime_ns}"
        else:
            snap[path] = tracked[path]
    return snap


def walk_snapshot(base: Path, rel: str) -> dict[str, str] | None:
    """Non-git fallback: {relpath: size:mtime} under base/rel."""
    target = base / rel
    if not target.exists():
        return None
    snap: dict[str, str] = {}
    if target.is_file():
        candidates = [target]
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            candidates.extend(Path(dirpath) / n for n in filenames)
    for f in candidates:
        try:
            relp = f.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            relp = f.as_posix()
        if _skippable(relp):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        snap[relp] = f"{st.st_size}:{st.st_mtime_ns}"
    return snap


def snapshot(base: Path, rel: str, use_git: bool) -> dict[str, str] | None:
    if use_git:
        try:
            return git_snapshot(base, rel)
        except Exception:
            pass
    return walk_snapshot(base, rel)


def fingerprint(snap: dict[str, str]) -> str:
    lines = sorted(f"{p}\t{tok}" for p, tok in snap.items())
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def root_structure_fingerprint(base: Path) -> str:
    """Top-level layout only (visible entries), so adding/removing a workspace
    flags the router without content noise."""
    entries = sorted(
        p.name + ("/" if p.is_dir() else "")
        for p in base.iterdir()
        if p.name not in SKIP_DIRS and not p.name.startswith(".")
    )
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()[:16]


def valid_rel(base: Path, rel) -> bool:
    """Manifest workspace paths must be relative and stay inside the project."""
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        return False
    try:
        (base / rel).resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return False
    return True


def write_manifest(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.split("."))


def stale_copy_hint(icm_dir: Path) -> str | None:
    """One-line nudge when the project-local checker copy predates this one."""
    copy = icm_dir / "drift-check.py"
    if not copy.is_file():
        return None
    try:
        if Path(sys.argv[0]).resolve() == copy.resolve():
            return None  # we ARE the project-local copy
        m = re.search(r'^SCRIPT_VERSION = "([\d.]+)"', copy.read_text(), re.M)
        if m and _ver(m.group(1)) >= _ver(SCRIPT_VERSION):
            return None
    except Exception:
        return None
    return ("[icm] note: .icm/drift-check.py is older than the skill's checker "
            f"(v{SCRIPT_VERSION}) - copy hooks/drift-check.py from the icm skill over it, "
            "then run: python3 .icm/drift-check.py --update")


def find_manifest(start: Path) -> Path | None:
    cur = start.resolve()
    for _ in range(6):  # walk up a few levels for nested launches
        m = cur / ".icm" / "manifest.json"
        if m.is_file():
            return m
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def name_some(paths, prefix: str) -> str:
    shown = sorted(paths)[:MAX_NAMED]
    extra = len(paths) - len(shown)
    s = ", ".join(prefix + p for p in shown)
    return s + (f", +{extra} more" if extra > 0 else "")


def run() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print(SCRIPT_VERSION)
        return 0
    update = "--update" in args
    strict = "--strict" in args
    manifest_path = find_manifest(Path.cwd())
    if manifest_path is None:
        return 0  # project does not use ICM; no-op

    base = manifest_path.parent.parent  # .icm/manifest.json -> project root
    data = json.loads(manifest_path.read_text())
    workspaces = data.get("workspaces", {})
    use_git = in_git_worktree(base)
    # Recommend the project-local copy when it exists (works for anyone).
    tool = (".icm/drift-check.py"
            if (manifest_path.parent / "drift-check.py").is_file() else sys.argv[0])

    if update:
        data["version"] = MANIFEST_VERSION
        data["checker_version"] = SCRIPT_VERSION
        data["synced"] = date.today().isoformat()
        data["_root_structure"] = root_structure_fingerprint(base)
        total = 0
        for name, ws in workspaces.items():
            rel = ws.get("path", name)
            if not valid_rel(base, rel):
                print(f"[icm] warning: workspace '{name}' path ignored "
                      f"(must be relative, inside the project): {rel}")
                continue
            snap = snapshot(base, rel, use_git)
            if snap is None:
                ws["fingerprint"] = "MISSING"
                ws["files"] = []
                print(f"[icm] warning: workspace '{name}' path not found: {rel}")
                continue
            ws["fingerprint"] = fingerprint(snap)
            ws["files"] = sorted(snap)
            total += len(snap)
        write_manifest(manifest_path, data)
        print(f"[icm] manifest refreshed: {len(workspaces)} workspace(s), "
              f"{total} file(s) fingerprinted.")
        return 0

    # ---- CHECK ----
    if data.get("version", 1) < MANIFEST_VERSION:
        print("[icm] manifest predates the current fingerprint scheme - run "
              f"`python3 {tool} --update` once from the project root (docs unaffected), "
              "then drift detection resumes with structural/content triage.")
        return 1 if strict else 0

    structural, content, missing = [], [], []
    bad_manifest = False
    for name, ws in workspaces.items():
        rel = ws.get("path", name)
        if not valid_rel(base, rel):
            print(f"[icm] warning: workspace '{name}' path ignored "
                  f"(must be relative, inside the project): {rel}")
            bad_manifest = True
            continue
        snap = snapshot(base, rel, use_git)
        if snap is None:
            missing.append(f"    - {name}: path `{rel}` no longer exists")
            continue
        stored_files = set(ws.get("files", []))
        added = set(snap) - stored_files
        removed = stored_files - set(snap)
        if added or removed:
            parts = []
            if added:
                parts.append(name_some(added, "+"))
            if removed:
                parts.append(name_some(removed, "-"))
            structural.append(f"    - {name}: {'; '.join(parts)}")
        elif fingerprint(snap) != ws.get("fingerprint"):
            content.append(f"    - {name}: file(s) edited in place")

    root_moved = (
        "_root_structure" in data
        and root_structure_fingerprint(base) != data["_root_structure"]
    )

    drifted = bool(structural or content or missing or root_moved)
    if drifted:
        synced = data.get("synced")
        print("[icm] ⚠ Nav docs are STALE relative to the code"
              + (f" (last sync {synced})" if synced else "") + ":")
        if missing:
            print("  Workspace paths gone (router + manifest need fixing):")
            print("\n".join(missing))
        if structural:
            print("  Structure changed (routing tables genuinely stale - fix these):")
            print("\n".join(structural))
        if content:
            print("  Content-only edits (routing usually still valid; re-verify source "
                  "before load-bearing work):")
            print("\n".join(content))
        if root_moved:
            print("  Root layout changed - review the router CLAUDE.md workspace index.")
        print("Fix: run /icm sync (targeted - only what is listed above). If the docs are "
              f"already accurate, just re-record: python3 {tool} --update (from the project root)")
    hint = stale_copy_hint(manifest_path.parent)
    if hint:
        print(hint)
    return 1 if strict and (drifted or bad_manifest) else 0


def main() -> int:
    strict = "--strict" in sys.argv[1:]
    try:
        return run()
    except Exception as e:  # never break session start (but do fail strict CI runs)
        print(f"[icm] drift-check error ({'strict' if strict else 'ignored'}): {e}")
        return 1 if strict else 0


if __name__ == "__main__":
    sys.exit(main())
