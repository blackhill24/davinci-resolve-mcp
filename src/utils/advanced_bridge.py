"""Advanced-server bridge — invoke resolve-advanced (Node) ops from the live server.

Generalizes the one-shot Node subprocess pattern that used to live privately as
``analysis_dashboard._run_advanced_bridge``. Two bridges sit on top of the shared
runner here:

* ``panel-bridge.mjs`` — READ-ONLY inspection (capabilities, lineage). The control
  panel uses this; it never mutates. Reached via :func:`run_panel_bridge`.
* ``drp-bridge.mjs`` — drp-format vendor OPS on an exported ``.drt``/``.drp`` in a
  scratch dir (``place_transition``, ``place_fusion_title``, ``split_clip`` …).
  Reached via :func:`run_drp_op`. Foundation for ``polish_timeline`` and drt audio
  automation.

Honest refusal: when Node is not on PATH, every entry point returns a structured
``{"success": False, "error": ..., "hint": ...}`` — never a silent failure and
never a raised exception. Callers can branch on ``success``.

Scratch discipline (source-media safety, mirrors the auto_edit rules): drt/drp
surgery always writes to a scratch location, never beside the source. Callers pass
an exported timeline; :func:`run_drp_op` copies nothing destructively — the Node op
reads ``drpPath`` and writes a fresh buffer to ``outputPath`` under scratch.

Stdlib only (no third-party deps) so this stays in the dependency-light posture
shared by the offline drift guards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

from src.utils.proc import safe_run


def _repo_root() -> str:
    # src/utils/advanced_bridge.py → repo root is two levels up.
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def advanced_root() -> str:
    """Absolute path to the ``resolve-advanced/`` Node workspace."""
    return os.path.join(_repo_root(), "resolve-advanced")


def node_path() -> Optional[str]:
    """The ``node`` executable on PATH, or ``None`` when Node is unavailable."""
    return shutil.which("node")


def node_available() -> bool:
    return node_path() is not None


def _node_unavailable() -> Dict[str, Any]:
    return {
        "success": False,
        "error": "Node.js not found on PATH",
        "hint": "Install Node.js 18+ to enable advanced-server (resolve-advanced) features.",
    }


def run_node_bridge(
    bridge_rel: str,
    argv: List[str],
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Run a one-shot Node bridge script and return its single JSON object.

    ``bridge_rel`` is a path relative to :func:`advanced_root` (e.g.
    ``"scripts/drp-bridge.mjs"``). ``argv`` are the string arguments passed after
    the script. The bridge must print exactly one JSON object with a ``success``
    key; anything else is reported as a structured failure.

    Never raises for the expected failure modes (missing Node, missing bridge,
    timeout, OS error, malformed output) — each becomes ``{"success": False, ...}``.
    """
    node = node_path()
    if not node:
        return _node_unavailable()

    root = advanced_root()
    bridge = os.path.join(root, bridge_rel)
    if not os.path.isfile(bridge):
        return {"success": False, "error": f"advanced bridge missing: {bridge}"}

    try:
        # safe_run defaults stdin to DEVNULL: never let a child race-read a
        # protocol/stdin stream.
        proc = safe_run(
            [node, bridge, *[str(a) for a in argv]],
            capture_output=True,
            text=True,
            # Node progress logs (e.g. the DRX merger's "1 → 2") are UTF-8; the process
            # locale may be ascii, so decode explicitly rather than crash on the arrow.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=root,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"advanced bridge timed out after {timeout:.0f}s"}
    except OSError as exc:
        return {"success": False, "error": str(exc)}

    raw = (proc.stdout or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict) or "success" not in payload:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
        return {"success": False, "error": "advanced bridge returned no JSON", "stderr": stderr_tail}
    return payload


def run_drx_compute(
    action: str,
    args: Dict[str, Any],
    *,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Compute a per-clip grade offline (``level_clips``, ``skin_match``,
    ``shot_match``, ...) — Resolve stays open the whole time; this never
    touches the live API, it just reads extracted frames and writes
    ``.drx`` files. ``args`` is passed through verbatim (``clips``,
    ``outDir``, and the action's own params) — the caller (host or another
    tool) owns frame extraction; this doesn't reimplement it.

    Returns the drx tool's raw result (``grades: [{id, drxPath, ...}]``,
    ``warnings``, ``skipped``) under ``result`` on success, honest refusal
    when Node is unavailable.
    """
    return run_node_bridge(
        "scripts/drp-bridge.mjs",
        ["drx", str(action), json.dumps(args)],
        timeout=timeout,
    )


def run_panel_bridge(
    surface: str,
    op: str,
    args: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Read-only inspection bridge (capabilities|lineage). See panel-bridge.mjs."""
    return run_node_bridge(
        "scripts/panel-bridge.mjs",
        [str(surface), str(op), json.dumps(args or {})],
        timeout=timeout,
    )


def run_drp_op(
    op: str,
    drp_path: str,
    *,
    out_path: Optional[str] = None,
    scratch_dir: Optional[str] = None,
    tool: str = "drp",
    timeout: float = 60.0,
    **opts: Any,
) -> Dict[str, Any]:
    """Invoke a drp-format vendor op on an exported ``.drt``/``.drp`` in scratch.

    ``op`` is a drp/drt tool action (``place_transition``, ``split_clip`` …).
    ``drp_path`` is the source timeline/project; it is read, never written. The
    mutated buffer is written to ``out_path`` when given, else to a fresh file in
    a scratch dir (``scratch_dir`` or a system temp dir) — never beside the source.

    Extra keyword args are forwarded verbatim to the op (e.g. ``track=1``,
    ``atFrame=100``, ``durationFrames=24``). Returns the bridge payload with an
    added ``output_path`` on success.

    Honest refusal when Node is unavailable or the source is missing.
    """
    if not node_available():
        return _node_unavailable()

    src = os.path.abspath(os.path.expanduser(drp_path))
    if not os.path.isfile(src):
        return {"success": False, "error": f"source timeline not found: {src}"}

    if out_path:
        output_path = os.path.abspath(os.path.expanduser(out_path))
    else:
        base = scratch_dir or tempfile.mkdtemp(prefix="drm-advanced-bridge-")
        os.makedirs(base, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(src))
        output_path = os.path.join(base, f"{stem}.{op}{ext or '.drt'}")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    args: Dict[str, Any] = {"drpPath": src, "outputPath": output_path, **opts}
    payload = run_node_bridge(
        "scripts/drp-bridge.mjs",
        [str(tool), str(op), json.dumps(args)],
        timeout=timeout,
    )
    if payload.get("success"):
        if not os.path.isfile(output_path):
            return {
                "success": False,
                "error": "op reported success but no output file was produced",
                "expected_output": output_path,
                "result": payload.get("result"),
            }
        payload["output_path"] = output_path
    return payload


def run_drp_op_chain(
    ops: List[Dict[str, Any]],
    src_path: str,
    *,
    scratch_dir: Optional[str] = None,
    tool: str = "drp",
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Apply a sequence of drp-format ops to ONE exported ``.drt``, in order.

    Each op is a spec ``{"op": <action>, "args": {...}}`` (extra keys like
    ``reason``/``kind`` are ignored) — the shape :func:`auto_edit.plan_polish_ops`
    emits. Ops are threaded: op 0 reads ``src_path`` and writes a fresh scratch
    file; op *i* reads op *i-1*'s output. The source is read, never written, so
    the final ``output_path`` is the fully-mutated timeline while the export stays
    byte-for-byte intact (source-media safety).

    Returns ``{"success", "output_path", "steps": [...]}``. Stops at the first
    failing op and reports which step failed (honest partial-failure surface).
    An empty ``ops`` list is a no-op: success with ``output_path == src_path``.
    """
    if not node_available():
        return _node_unavailable()
    src = os.path.abspath(os.path.expanduser(src_path))
    if not os.path.isfile(src):
        return {"success": False, "error": f"source timeline not found: {src}"}
    if not ops:
        return {"success": True, "output_path": src, "steps": [],
                "note": "no ops — nothing to polish"}

    base = scratch_dir or tempfile.mkdtemp(prefix="drm-advanced-chain-")
    os.makedirs(base, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(src))
    ext = ext or ".drt"

    current = src
    steps: List[Dict[str, Any]] = []
    for i, spec in enumerate(ops):
        op = str(spec.get("op") or "")
        op_args = dict(spec.get("args") or {})
        if not op:
            return {"success": False, "error": f"op {i} has no 'op' action",
                    "steps": steps, "failed_step": i}
        step_out = os.path.join(base, f"{stem}.{i:02d}.{op}{ext}")
        payload = run_drp_op(
            op, current, out_path=step_out, tool=tool, timeout=timeout, **op_args)
        steps.append({
            "index": i, "op": op, "success": bool(payload.get("success")),
            "output_path": payload.get("output_path"),
            "error": payload.get("error"),
        })
        if not payload.get("success"):
            return {"success": False,
                    "error": f"op {i} ({op}) failed: {payload.get('error')}",
                    "steps": steps, "failed_step": i, "last_output": current}
        current = payload["output_path"]

    return {"success": True, "output_path": current, "steps": steps}
