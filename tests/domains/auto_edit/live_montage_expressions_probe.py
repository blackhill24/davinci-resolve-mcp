#!/usr/bin/env python3
"""Live probe for the montage Fusion expressions phase 2's `shake` and
`fadeout` flags drive.

Requires DaVinci Resolve Studio running. Narrow on purpose: it does NOT run the
pipeline (live_montage_quality.py already does, end to end). It answers the two
questions that harness cannot, because the reference track's arrangement only
ever produced `intro`/`outro` sections and so never raised a `shake` flag:

  1. Does a Fusion Transform actually expose an input named ``Angle``, and does
     an expression set on it READ BACK? Shake rides that input rather than the
     tool's Center, because Center is a Point input and this repo's own bridge
     notes (fusion_composition._fusion_set_point_input) record that Point inputs
     take different encodings per platform/version. `Size` and `Gain` are the
     only expression targets verified live so far — `Angle` was an assumption
     until this probe.
  2. Does a second, independent BrightnessContrast (``MCP_Fadeout``) coexist
     with ``MCP_Flash`` in one comp, each holding its own Gain expression? The
     outro's final entry can carry both flags at once, and a shared tool would
     have them overwrite each other.

Neither answer is inferable offline: SetExpression's own return value is not
trustworthy (see _fusion_expression_set_ok), so read-back against a real Fusion
engine is the only check that means anything.

Never touches source media. Works in a disposable project, deleted at the end;
the user's previous project is restored.

Run: PYTHONPATH=. .venv/bin/python tests/domains/auto_edit/live_montage_expressions_probe.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.domains.auto_edit.utils import montage_motion  # noqa: E402

PILOT = f"montage_expr_probe_{time.strftime('%H%M%S')}"
CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{' — ' + detail if detail else ''}")


def _add_tool(comp, tool_type: str, name: str):
    """AddTool + rename inside its own Lock cycle — the pattern every proven
    Fusion mutation in this repo uses (see actions.py's _fusion_locked)."""
    comp.Lock()
    try:
        tool = comp.AddTool(tool_type, -1, -1)
        if tool is None:
            return None
        try:
            tool.SetAttrs({"TOOLS_Name": name})
        except Exception:
            pass
        return tool
    finally:
        comp.Unlock()


def _set_expr_readback(comp, tool, input_name: str, expr: str):
    """Set an expression and return what reads back (None on failure).
    SetExpression's own return is discarded on purpose — it returns False even
    on success (the finding _fusion_expression_set_ok documents)."""
    comp.Lock()
    try:
        tool[input_name].SetExpression(str(expr), 0)
    except Exception as exc:
        print(f"    SetExpression({input_name}) raised: {type(exc).__name__}: {exc}")
        return None
    finally:
        comp.Unlock()
    try:
        return tool[input_name].GetExpression()
    except Exception as exc:
        print(f"    GetExpression({input_name}) raised: {type(exc).__name__}: {exc}")
        return None


def run(s) -> int:
    r = s.get_resolve()
    if r is None:
        print("Resolve not available — aborting")
        return 2
    pm = r.GetProjectManager()
    previous_project = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None
    proj = pm.CreateProject(PILOT)
    check("disposable project created", proj is not None, PILOT)
    if proj is None:
        return 2

    try:
        # A Fusion-page comp is enough: the expression engine is the same one a
        # timeline item's comp uses, and this needs no media.
        fusion = r.Fusion()
        comp = fusion.NewComp() if fusion else None
        check("fusion comp available", comp is not None)
        if comp is None:
            return 2

        # 1) shake -> Transform.Angle
        transform = _add_tool(comp, "Transform", "MCP_BeatPulse")
        check("Transform tool added", transform is not None)
        if transform is None:
            return 1

        inputs = {}
        try:
            inputs = comp.GetToolList(False, "Transform") and transform.GetInputList() or {}
        except Exception:
            inputs = {}
        input_names = []
        for key in (inputs or {}):
            try:
                input_names.append(str(inputs[key].GetAttrs().get("INPS_ID")))
            except Exception:
                continue
        check("Transform exposes an 'Angle' input", "Angle" in input_names,
              f"{len(input_names)} inputs; Angle present={'Angle' in input_names}")

        shake_expr = montage_motion.build_shake_expression(
            beat_seconds=0.5, fps=24.0, record_start_frame=48)
        got = _set_expr_readback(comp, transform, "Angle", shake_expr)
        check("shake expression reads back off Transform.Angle",
              bool(got) and "fmod" in str(got) and "sin" in str(got), str(got))

        # The already-verified Size target must still work alongside it — shake
        # and the zoom pulse share this one tool.
        zoom_expr = montage_motion.build_zoom_expression(
            zoom_start=1.0, zoom_end=1.05, amp=0.05, beat_seconds=0.5, fps=24.0,
            record_start_frame=48, clip_length_frames=48)
        got_size = _set_expr_readback(comp, transform, "Size", zoom_expr)
        check("zoom expression still reads back off the SAME tool's Size",
              bool(got_size) and "fmod" in str(got_size), str(got_size))

        # 2) fadeout -> its own BrightnessContrast, coexisting with the flash one
        flash = _add_tool(comp, "BrightnessContrast", "MCP_Flash")
        fade = _add_tool(comp, "BrightnessContrast", "MCP_Fadeout")
        check("two independent BrightnessContrast tools added",
              flash is not None and fade is not None)
        if flash is None or fade is None:
            return 1

        flash_got = _set_expr_readback(comp, flash, "Gain", montage_motion.build_flash_expression())
        fade_got = _set_expr_readback(
            comp, fade, "Gain",
            montage_motion.build_fadeout_expression(fps=24.0, clip_length_frames=120))
        check("flash Gain expression reads back", bool(flash_got), str(flash_got))
        check("fadeout Gain expression reads back", bool(fade_got), str(fade_got))
        check("the two Gain expressions did NOT overwrite each other",
              bool(flash_got) and bool(fade_got) and str(flash_got) != str(fade_got),
              f"flash={flash_got!r} fade={fade_got!r}")

        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        try:
            restored = bool(previous_project and pm.LoadProject(previous_project))
            if not restored:
                pm.CreateProject(previous_project or "Untitled Project")
            pm.DeleteProject(PILOT)
        except Exception as exc:
            print(f"cleanup: {type(exc).__name__}: {exc}")


def main() -> int:
    import src.server as s
    code = run(s)
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return code


if __name__ == "__main__":
    from tests.preflight import gate
    gate("idle")
    sys.exit(main())
