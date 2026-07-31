"""Static guard: no NEW discarded Resolve mutator return values in src/.

Resolve's scripting API reports failure by RETURN VALUE, not by raising. A call
whose return is dropped is therefore an unconditional "it worked". That single
shape has produced a bug in three consecutive audits:

  * #110 — ungated project delete, render-queue wipe
  * #111 finding 5 — `ensure_timeline()` discarded `SetSetting("timelineFrameRate")`
    and reported success at the project default fps
  * #111 finding 6 — `modify_keyframe()` / `set_keyframe_interpolation()` discarded
    `DeleteKeyframe()`, leaving BOTH the old and new keyframe on the item while
    reporting the edit succeeded

Issue #113 tracks sweeping the sites that already exist. This guard is the other
half: it freezes that set so a FOURTH audit doesn't turn up a fresh one. It does
not assert the accepted sites are correct — only that the list stops growing
silently, and that it shrinks as #113 works through the tiers.

Excluded from the scan, deliberately:
  * `finally:` / `except:` bodies — best-effort cleanup on the way out, with no
    caller left to report to. Checking those returns would be noise.

NOT excluded: `*_live_probe.py`. They are diagnostic harnesses rather than tool
paths, so they are low-priority for #113, but they are still `src/` code and a new
discarded return there should still be a deliberate choice.

## When this test fails

**"new discarded mutator return"** — you added one. Either check the return, or,
if it genuinely cannot be checked, add it to ACCEPTED_DISCARDED_RETURNS with a
comment saying why. Do not add it silently to make the test pass; that is the
exact habit this guard exists to interrupt.

**"no longer present"** — you FIXED one (thank you) or renamed/moved the function.
Update ACCEPTED_DISCARDED_RETURNS to match. The baseline is meant to ratchet down.

Keys are `(file, enclosing function, method)` rather than `file:line` so the
baseline survives unrelated edits that shift line numbers.
"""
from __future__ import annotations

import ast
import collections
import pathlib
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

# A Resolve mutator: CapWords starting with one of these verbs, where the verb is
# followed by another capital (so `Settings` / `Adder` don't match, but
# `SetInput` / `DeleteTimelines` do).
MUTATOR_PREFIXES = ("Set", "Add", "Delete", "Append", "Create")


def _is_mutator(name: str) -> bool:
    return bool(name) and name[0].isupper() and any(
        name.startswith(p) and len(name) > len(p) and name[len(p)].isupper()
        for p in MUTATOR_PREFIXES
    )


def _cleanup_ranges(tree: ast.AST):
    """Line spans of every `finally:` and `except:` body — best-effort cleanup."""
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            bodies = list(node.finalbody) + [s for h in node.handlers for s in h.body]
            for stmt in bodies:
                spans.append((stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno)))
    return spans


def _enclosing_function(funcs, lineno: str) -> str:
    """Innermost function containing `lineno` (nested defs pick the tightest)."""
    containing = [f for f in funcs if f[0] <= lineno <= f[1]]
    if not containing:
        return "<module>"
    return min(containing, key=lambda f: f[1] - f[0])[2]


def scan_discarded_mutator_returns() -> collections.Counter:
    """{(relpath, function, method): count} for every dropped mutator return."""
    found: collections.Counter = collections.Counter()

    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _cleanup_ranges(tree)
        funcs = [
            (n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        for node in ast.walk(tree):
            # `ast.Expr` wrapping a `Call` IS the discarded-return shape: the call
            # is a statement, so nothing receives what it returned.
            if not isinstance(node, ast.Expr):
                continue
            call = node.value
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if not _is_mutator(call.func.attr):
                continue
            if any(lo <= node.lineno <= hi for lo, hi in skip):
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            found[(rel, _enclosing_function(funcs, node.lineno), call.func.attr)] += 1

    return found


# ── Accepted baseline ────────────────────────────────────────────────────────
#
# Discarded mutator returns still accepted: 31 keys, 39 call sites. #113 is now
# fully triaged — every entry below has a stated reason, grouped. This is the
# record so a future audit does not re-derive the same list a fourth time.
#
#   Tier 1 — DONE (#115). All 18 SetCurrentTimeline-before-a-mutation sites on
#            tool paths go through `_set_current_timeline()` (read-back verified).
#   Tier 2 — DONE (#116). All 8 destructive / user-visible sites report what
#            actually happened: SetStartTimecode goes through
#            `_set_start_timecode()` and refuses the append when it does not take,
#            because the record frames were computed from that start;
#            DeleteTimelines / DeleteStills / DeleteMarkerByCustomData either fail
#            or surface a warning instead of claiming work they did not do.
#   Tier 3 — DONE. Triage found 4 that were NOT ignorable and fixed them:
#            SetCurrentRenderMode x2 (a wrong mode silently renders one stitched
#            file instead of per-clip proxies, or one file per clip instead of a
#            continuous render-in-place), the Lua completion sentinel (a failed
#            clear returned the PREVIOUS script's output as the current run's),
#            and the contact-sheet playhead (thumbnails captured at the wrong
#            frame but labelled with correct-looking timecodes). The rest are
#            genuinely ignorable and each group below says why.
#
# Do not grow this dict without a reason in the diff, and do not add an entry
# just to make the guard pass — that is the habit it exists to interrupt.
ACCEPTED_DISCARDED_RETURNS = {
    # ── Fusion tool setters (26) ──────────────────────────────────────────────
    # Fusion's tool/flow setters come through the Lua bridge and have no
    # dependable return. The repo's own live-verified ledger says so for the
    # clearest case: src/core/api_truth.py on FlowView.SetPos — "SetPos returns
    # nothing reliable; ... confirm with GetPosTable" (re-verified on Resolve
    # Studio 21.0.2.4). Checking these would be checking noise.
    #
    # Verification here is by READ-BACK where it matters, which is already in
    # place: _safe_set_fusion_inputs reads each value back with GetInput() and
    # reports it (`readback` param, default on), and set_position confirms via
    # GetPosTable. That is the src/core/readback.py doctrine, and it is a better
    # check than the return value would be even if the return existed.
    ("src/domains/fusion_composition/actions.py", "_fusion_add_mask", "SetAttrs"): 1,
    ("src/domains/fusion_composition/actions.py", "_fusion_add_mask", "SetInput"): 1,
    ("src/domains/fusion_composition/actions.py", "_fusion_comp_bulk_set_expressions", "SetExpression"): 1,
    ("src/domains/fusion_composition/actions.py", "_fusion_comp_bulk_set_inputs", "SetInput"): 2,
    ("src/domains/fusion_composition/actions.py", "_fusion_set_point_input", "SetInput"): 1,
    ("src/domains/fusion_composition/actions.py", "_fusion_set_text_plus", "SetInput"): 1,
    ("src/domains/fusion_composition/actions.py", "_safe_add_fusion_tool", "SetAttrs"): 1,
    # auto_edit's _ensure_fusion_tool_locked mirrors _safe_add_fusion_tool
    # exactly (same AddTool+rename-in-one-lock recipe, live-verified as the
    # one pattern that actually renames a newly added tool). Verified by
    # GetAttrs().get("TOOLS_Name") read-back right after, same doctrine.
    ("src/domains/auto_edit/actions.py", "_ensure_fusion_tool_locked", "SetAttrs"): 1,
    # SetExpression/SetInput's return is the SAME unreliable Lua-bridge
    # signal as FlowView.SetPos above — live-verified here the hard way: with
    # creation, rename, wiring, AND the expression/input all inside one
    # unbroken Lock cycle, SetExpression/SetInput STILL returned False on
    # every call. _fusion_expression_set_ok/_fusion_input_set_ok discard it
    # and verify with GetExpression/GetInput read-back instead, matching
    # _fusion_comp_bulk_set_expressions's own doctrine (which discards
    # SetExpression's return entirely, see line ~615 of
    # fusion_composition/actions.py).
    ("src/domains/auto_edit/actions.py", "_fusion_expression_set_ok", "SetExpression"): 1,
    ("src/domains/auto_edit/actions.py", "_fusion_input_set_ok", "SetInput"): 1,
    ("src/domains/fusion_composition/actions.py", "_safe_set_fusion_inputs", "SetInput"): 2,
    ("src/domains/fusion_composition/actions.py", "fusion_comp", "AddModifier"): 1,
    ("src/domains/fusion_composition/actions.py", "fusion_comp", "SetAttrs"): 4,
    ("src/domains/fusion_composition/actions.py", "fusion_comp", "SetInput"): 2,
    ("src/domains/fusion_composition/actions.py", "fusion_comp", "SetPos"): 3,

    # ── Verified by read-back instead of by the return (2) ────────────────────
    # These DO matter, and #113 Tier 3 fixed them — but the fix was to verify the
    # observed state, not to check the return, so the bare call still appears
    # here. Do not "fix" these by wrapping the return; the read-back beneath each
    # one is the real check.
    #
    # _run_inline_lua clears four sentinel slots before RunScript; a clear that
    #   did not take leaves the previous run's __mcp_done__ == "1", so the poll
    #   exits immediately and returns the PREVIOUS script's output as this one's.
    #   Now read back with GetData and refused if the sentinel is still set.
    # _timeline_thumbnail_contact_sheet moves the playhead per sample; a playhead
    #   that did not move produced thumbnails from the wrong frame labelled with
    #   correct-looking timecodes. Now compared with GetCurrentTimecode and the
    #   sample is skipped with an error rather than captured at the wrong frame.
    ("src/domains/extension_authoring/actions.py", "_run_inline_lua", "SetData"): 1,
    ("src/domains/timeline_edit/actions.py", "_timeline_thumbnail_contact_sheet", "SetCurrentTimecode"): 1,

    # ── Context navigation whose real outcome is captured elsewhere (5) ───────
    # media_pool_item navigates to the clip's folder purely so SetSelectedClip
    #   can see it; the actual result is `select_ok = bool(mp.SetSelectedClip(...))`,
    #   which IS checked. The navigation failing on its own is not an outcome.
    # _restore_current_folder and _resolve_restore_state put the user's UI back
    #   where it was. Nothing downstream depends on them, they cannot corrupt
    #   anything, and _resolve_restore_state already reports what it managed to
    #   restore via its `restored` dict (its timeline restore was hardened in
    #   Tier 1 because that one WAS claimed as restored regardless).
    ("src/domains/media_pool_ingest/actions.py", "_restore_current_folder", "SetCurrentFolder"): 1,
    ("src/domains/media_pool_ingest/actions.py", "media_pool_item", "SetCurrentFolder"): 1,
    ("src/server.py", "_resolve_restore_state", "SetCurrentFolder"): 1,
    ("src/server.py", "_resolve_restore_state", "SetCurrentTimecode"): 1,
    ("src/server.py", "_resolve_restore_state", "SetSelectedClip"): 1,

    # ── Diagnostic live probes, not tool paths (12) ───────────────────────────
    # *_live_probe.py harnesses are run by hand against a real Resolve to record
    # what the API does; they report their own findings and are not reachable
    # from MCP dispatch. They stay listed so a NEW discarded return in one is
    # still a deliberate choice rather than an accident.
    ("src/domains/audio_fairlight/utils/audio_fairlight_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/color_grade/utils/color_grade_live_probe.py", "run_probe", "AppendToTimeline"): 1,
    ("src/domains/color_grade/utils/color_grade_live_probe.py", "run_probe", "SetCurrentTimecode"): 1,
    ("src/domains/color_grade/utils/color_grade_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/fusion_composition/utils/fusion_composition_live_probe.py", "run_probe", "SetCurrentTimecode"): 1,
    ("src/domains/fusion_composition/utils/fusion_composition_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/render_deliver/utils/render_deliver_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/review_annotation/utils/review_annotation_live_probe.py", "run_probe", "SetCurrentTimecode"): 1,
    ("src/domains/review_annotation/utils/review_annotation_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/timeline_conform_interchange/utils/timeline_conform_live_probe.py", "run_probe", "AppendToTimeline"): 1,
    ("src/domains/timeline_conform_interchange/utils/timeline_conform_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/timeline_edit/utils/timeline_kernel_live_probe.py", "run_probe", "SetClipsLinked"): 1,

}


def _fmt(key, count):
    path, func, method = key
    return f"{path}::{func}() — {method}() x{count}"


class DiscardedMutatorReturnsTest(unittest.TestCase):
    # Parsing all of src/ takes ~1s; do it once for the class, not per test.
    @classmethod
    def setUpClass(cls):
        cls.found = scan_discarded_mutator_returns()

    def test_no_new_discarded_mutator_returns(self):
        """A dropped Resolve return is an unconditional 'it worked'. Don't add one."""
        new = []
        for key, count in sorted(self.found.items()):
            accepted = ACCEPTED_DISCARDED_RETURNS.get(key, 0)
            if count > accepted:
                new.append(_fmt(key, count - accepted))

        self.assertEqual(
            [], new,
            "new discarded mutator return(s) — Resolve reports failure by RETURN VALUE, "
            "not by raising, so dropping it silently reports success (the shape behind "
            "#110's ungated deletes and #111 findings 5 and 6). Check the return, or add "
            "it to ACCEPTED_DISCARDED_RETURNS in this file WITH a reason in the diff:\n  "
            + "\n  ".join(new),
        )

    def test_baseline_has_no_stale_entries(self):
        """As #113 fixes sites, the baseline must ratchet down rather than rot."""
        stale = []
        for key, accepted in sorted(ACCEPTED_DISCARDED_RETURNS.items()):
            count = self.found.get(key, 0)
            if count < accepted:
                stale.append(f"{_fmt(key, accepted)} — now {count}")

        self.assertEqual(
            [], stale,
            "ACCEPTED_DISCARDED_RETURNS lists call sites that are no longer there. If you "
            "fixed them (thank you) or renamed the enclosing function, prune/update the "
            "dict so the baseline keeps ratcheting down:\n  " + "\n  ".join(stale),
        )

    def test_scanner_still_detects_the_known_shape(self):
        """Guard the guard: a scanner that silently matches nothing proves nothing."""
        self.assertGreater(
            len(self.found), 0,
            "the scan found no discarded mutator returns at all — the scanner is broken, "
            "not the codebase (see ACCEPTED_DISCARDED_RETURNS for the known baseline)",
        )
        # Anchor on a site that is still in the baseline. This used to point at
        # timeline_edit._timeline_insert_edit_impl's SetCurrentTimeline — the
        # Tier-1 exemplar — which is now fixed, so the anchor moved to a Tier-3
        # entry that is expected to stay. Re-anchor again if Tier 3 is ever swept.
        self.assertIn(
            ("src/domains/fusion_composition/actions.py", "fusion_comp", "SetInput"),
            self.found,
            "a known baseline entry is missing from the scan — the AST matcher regressed",
        )

    def test_cleanup_blocks_are_excluded(self):
        """`finally:`/`except:` restores are best-effort by design, not findings."""
        source = (
            "class T:\n"
            "    def go(self):\n"
            "        try:\n"
            "            self.obj.SetCurrentTimeline(a)\n"   # counted
            "        except Exception:\n"
            "            self.obj.SetCurrentTimeline(b)\n"   # excluded
            "        finally:\n"
            "            self.obj.SetCurrentTimeline(c)\n"   # excluded
        )
        tree = ast.parse(source)
        skip = _cleanup_ranges(tree)
        counted = [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and _is_mutator(n.value.func.attr)
            and not any(lo <= n.lineno <= hi for lo, hi in skip)
        ]
        self.assertEqual([4], counted, "only the try-body call should count")

    def test_mutator_name_matching(self):
        for name in ("SetInput", "DeleteTimelines", "AddKeyframe", "AppendToTimeline",
                     "CreateEmptyTimeline", "SetCurrentTimeline"):
            with self.subTest(name=name):
                self.assertTrue(_is_mutator(name))
        # Not mutators: lowercase, no second capital, or unrelated verbs.
        for name in ("Settings", "Adder", "Deleted", "settings", "GetName", "Set", ""):
            with self.subTest(name=name):
                self.assertFalse(_is_mutator(name))


class RatchetIsNotVacuousTest(unittest.TestCase):
    """The baseline comparison itself is exercised (#121 task 2).

    The tests above check the *scanner* (it finds something; it excludes cleanup
    blocks; the name matcher is right). None of them checks the thing that
    actually gates a pull request: that a NEW site over the accepted count makes
    the assertion fail, and that a FIXED site makes the stale check fail. Both
    directions are driven here against a synthetic src/ tree.
    """

    def _scan_tree(self, body: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "src").mkdir()
        (root / "src" / "synthetic_mutators.py").write_text(textwrap.dedent(body), encoding="utf-8")
        module = sys.modules[__name__]
        return mock.patch.multiple(module, SRC=root / "src", REPO_ROOT=root)

    def _run(self, method):
        # setUpClass caches the scan, so drive the scan+assert by hand.
        case = DiscardedMutatorReturnsTest(method)
        case.found = scan_discarded_mutator_returns()
        with self.assertRaises(AssertionError) as caught:
            getattr(case, method)()
        return str(caught.exception)

    def test_a_new_discarded_return_fails_the_ratchet(self):
        source = """
        def apply_edit(tl, item):
            tl.SetCurrentTimecode("01:00:00:00")
            return True
        """
        with self._scan_tree(source), mock.patch.dict(ACCEPTED_DISCARDED_RETURNS, {}, clear=True):
            message = self._run("test_no_new_discarded_mutator_returns")
        self.assertIn("src/synthetic_mutators.py::apply_edit()", message)
        self.assertIn("SetCurrentTimecode", message)

    def test_a_second_call_at_the_same_site_fails_even_when_one_is_accepted(self):
        # The count matters, not just the key: two dropped returns where one was
        # reviewed is still one unreviewed drop.
        source = """
        def apply_edit(tl, item):
            tl.SetCurrentTimecode("01:00:00:00")
            tl.SetCurrentTimecode("02:00:00:00")
        """
        accepted = {("src/synthetic_mutators.py", "apply_edit", "SetCurrentTimecode"): 1}
        with self._scan_tree(source), mock.patch.dict(
            ACCEPTED_DISCARDED_RETURNS, accepted, clear=True
        ):
            message = self._run("test_no_new_discarded_mutator_returns")
        self.assertIn("SetCurrentTimecode() x1", message)

    def test_a_fixed_site_fails_the_stale_check(self):
        source = """
        def apply_edit(tl, item):
            ok = tl.SetCurrentTimecode("01:00:00:00")
            return ok
        """
        accepted = {("src/synthetic_mutators.py", "apply_edit", "SetCurrentTimecode"): 1}
        with self._scan_tree(source), mock.patch.dict(
            ACCEPTED_DISCARDED_RETURNS, accepted, clear=True
        ):
            message = self._run("test_baseline_has_no_stale_entries")
        self.assertIn("now 0", message)

    def test_a_checked_return_is_not_reported(self):
        # The converse — assigning the return must clear the finding entirely,
        # or the guard would fire on correct code and get tuned into silence.
        source = """
        def apply_edit(tl, item):
            if not tl.SetCurrentTimecode("01:00:00:00"):
                return False
            return True
        """
        with self._scan_tree(source):
            self.assertEqual(scan_discarded_mutator_returns(), collections.Counter())


if __name__ == "__main__":
    unittest.main()
