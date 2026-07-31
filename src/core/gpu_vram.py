"""Free-VRAM precondition check for Resolve's GPU deblur render (issue #188).

`MediaPoolItem.RemoveMotionBlur` / `Folder.RemoveMotionBlur` decline instantly
(return `None`/`False` in ~0.0s, no rendering attempted) when the GPU doesn't
have enough free memory — confirmed by direct repro on an otherwise-idle
Resolve: same result regardless of clip content, `deblur_option`, or prior GPU
load. Resolve's own scripting API gives no error detail beyond the bare
falsy return, so callers were seeing an unexplained `{"success": False}`.
Checking free VRAM up front turns that into an actionable error instead.

`REMOVE_MOTION_BLUR_MIN_FREE_MIB` is a measured floor from that repro (~12GB
needed on a box with ~8GB usable), not a documented Resolve spec — Resolve
does not publish a VRAM minimum for this function.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, Optional

REMOVE_MOTION_BLUR_MIN_FREE_MIB = 12000


def free_vram_mib() -> Optional[int]:
    """Free VRAM (MiB) on the primary GPU, or None if it can't be determined
    (no NVIDIA driver, `nvidia-smi` missing/erroring, or a timeout). None is
    "unknown", never "zero" — callers must not treat it as a hard failure."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            check=False, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        return None
    value = lines[0].strip()
    if not value.isdigit():
        return None
    return int(value)


def insufficient_vram_error(
    min_free_mib: int = REMOVE_MOTION_BLUR_MIN_FREE_MIB,
) -> Optional[Dict[str, Any]]:
    """An error dict if measured free VRAM is below `min_free_mib`, else None.

    Returns None (never blocks) when VRAM can't be measured — an unknown GPU
    state must not be treated as insufficient.
    """
    free = free_vram_mib()
    if free is None or free >= min_free_mib:
        return None
    return {
        "error": (
            f"GPU deblur (RemoveMotionBlur) needs roughly {min_free_mib / 1024:.0f} GB of "
            f"free GPU VRAM; only {free / 1024:.1f} GB is currently free. Resolve's own API "
            "declines this call instantly in that state and returns a bare False/None with "
            "no detail — this check runs first so the failure is explained instead. Free up "
            "GPU memory (close other GPU-using apps; a prior heavy render may still hold "
            "VRAM) and retry. If this GPU has less VRAM than the feature needs, it cannot "
            "succeed on this hardware (see issue #188)."
        ),
        "free_vram_mib": free,
        "required_vram_mib": min_free_mib,
    }
