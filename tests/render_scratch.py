"""Shared render-output scratch dir for live probes (issue #92).

Resolve's render queue refuses a bare ``/tmp`` destination (see #23), so the
live probes render into a ``~/Videos`` subdirectory. Left to themselves they
never remove it — 83 dirs / ~1.3 GB had accumulated in the user's own media
folder. This centralizes the lifecycle so ``~/Videos`` cannot grow unbounded:

- :func:`make_render_dir` sweeps any leftover ``<prefix>*`` dirs from a prior
  run *before* creating this run's dir, so even a crash (which skips cleanup)
  leaves at most one run's output behind — it is reclaimed on the next run.
- :func:`cleanup_render_dir` removes this run's dir on a clean exit.

Set ``DRM_KEEP_RENDERS=1`` to keep the current run's output for inspection;
the start-of-run sweep still runs, so a kept dir survives only until the next
probe of the same kind.
"""

from __future__ import annotations

import os
import shutil
import tempfile

_VIDEOS = os.path.expanduser("~/Videos")


def _base_dir() -> str | None:
    """The ~/Videos dir if it exists, else None (fall back to system temp)."""
    return _VIDEOS if os.path.isdir(_VIDEOS) else None


def sweep_stale(prefix: str, keep: str | None = None) -> None:
    """Remove leftover ``<prefix>*`` render dirs from a prior (crashed) run.

    ``keep`` is an absolute path spared from the sweep (this run's own dir).
    """
    base = _base_dir()
    if base is None:
        return
    keep_abs = os.path.abspath(keep) if keep else None
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path) or os.path.abspath(path) == keep_abs:
            continue
        shutil.rmtree(path, ignore_errors=True)


def make_render_dir(prefix: str) -> str:
    """Sweep prior ``<prefix>*`` output, then create a fresh render dir.

    Rendered into ``~/Videos`` when it exists (Resolve won't render to /tmp),
    otherwise the system temp dir.
    """
    sweep_stale(prefix)
    return tempfile.mkdtemp(prefix=prefix, dir=_base_dir())


def cleanup_render_dir(path: str) -> None:
    """Remove a run's render dir unless ``DRM_KEEP_RENDERS`` is set."""
    if os.environ.get("DRM_KEEP_RENDERS"):
        return
    shutil.rmtree(path, ignore_errors=True)
