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
import unittest

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
# Discarded mutator returns still accepted: 41 keys, 52 call sites. Tracked for
# triage in #113 — this is NOT an assertion that they are all correct.
#
#   Tier 1 — DONE. All 18 SetCurrentTimeline-before-a-mutation sites on tool paths
#            now go through `_set_current_timeline()` (read-back verified). The 6
#            remaining SetCurrentTimeline entries below are all in *_live_probe.py,
#            which are diagnostic harnesses rather than tool paths.
#   Tier 2 — OPEN. Destructive / user-visible mutations still reported as success:
#            DeleteTimelines (orchestration x2), DeleteStills (color_grade),
#            DeleteMarkerByCustomData (media_analysis x2), SetStartTimecode
#            (timeline_edit, media_pool_ingest, granular/media_pool).
#   Tier 3 — OPEN (decide + document as ignorable, don't necessarily fix): the
#            Fusion SetInput/SetAttrs/SetPos/AddModifier cluster,
#            SetCurrentFolder/SetSelectedClip/SetCurrentTimecode state restores,
#            SetCurrentRenderMode, _run_inline_lua's SetData.
#
# Shrink this dict as #113 lands. Do not grow it without a reason in the diff.
ACCEPTED_DISCARDED_RETURNS = {
    ("src/domains/audio_fairlight/utils/audio_fairlight_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/color_grade/actions.py", "gallery_stills", "DeleteStills"): 1,
    ("src/domains/color_grade/utils/color_grade_live_probe.py", "run_probe", "AppendToTimeline"): 1,
    ("src/domains/color_grade/utils/color_grade_live_probe.py", "run_probe", "SetCurrentTimecode"): 1,
    ("src/domains/color_grade/utils/color_grade_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/extension_authoring/actions.py", "_run_inline_lua", "SetData"): 4,
    ("src/domains/fusion_composition/actions.py", "_fusion_add_mask", "SetAttrs"): 1,
    ("src/domains/fusion_composition/actions.py", "_fusion_add_mask", "SetInput"): 1,
    ("src/domains/fusion_composition/actions.py", "_fusion_comp_bulk_set_expressions", "SetExpression"): 1,
    ("src/domains/fusion_composition/actions.py", "_fusion_comp_bulk_set_inputs", "SetInput"): 2,
    ("src/domains/fusion_composition/actions.py", "_fusion_set_point_input", "SetInput"): 1,
    ("src/domains/fusion_composition/actions.py", "_fusion_set_text_plus", "SetInput"): 1,
    ("src/domains/fusion_composition/actions.py", "_safe_add_fusion_tool", "SetAttrs"): 1,
    ("src/domains/fusion_composition/actions.py", "_safe_set_fusion_inputs", "SetInput"): 2,
    ("src/domains/fusion_composition/actions.py", "fusion_comp", "AddModifier"): 1,
    ("src/domains/fusion_composition/actions.py", "fusion_comp", "SetAttrs"): 4,
    ("src/domains/fusion_composition/actions.py", "fusion_comp", "SetInput"): 2,
    ("src/domains/fusion_composition/actions.py", "fusion_comp", "SetPos"): 3,
    ("src/domains/fusion_composition/utils/fusion_composition_live_probe.py", "run_probe", "SetCurrentTimecode"): 1,
    ("src/domains/fusion_composition/utils/fusion_composition_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/media_analysis/actions.py", "_apply_media_analysis_clip_markers", "DeleteMarkerByCustomData"): 1,
    ("src/domains/media_analysis/actions.py", "_apply_sync_event_markers", "DeleteMarkerByCustomData"): 1,
    ("src/domains/media_pool_ingest/actions.py", "_restore_current_folder", "SetCurrentFolder"): 1,
    ("src/domains/media_pool_ingest/actions.py", "_setup_multicam_timeline", "SetStartTimecode"): 1,
    ("src/domains/media_pool_ingest/actions.py", "media_pool_item", "SetCurrentFolder"): 1,
    ("src/domains/orchestration/actions.py", "_orchestrate_gc_snapshots_live", "DeleteTimelines"): 1,
    ("src/domains/orchestration/actions.py", "_orchestrate_restore_snapshot", "DeleteTimelines"): 1,
    ("src/domains/render_deliver/actions.py", "_build_proxies", "SetCurrentRenderMode"): 1,
    ("src/domains/render_deliver/utils/render_deliver_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/review_annotation/utils/review_annotation_live_probe.py", "run_probe", "SetCurrentTimecode"): 1,
    ("src/domains/review_annotation/utils/review_annotation_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/timeline_conform_interchange/utils/timeline_conform_live_probe.py", "run_probe", "AppendToTimeline"): 1,
    ("src/domains/timeline_conform_interchange/utils/timeline_conform_live_probe.py", "run_probe", "SetCurrentTimeline"): 1,
    ("src/domains/timeline_edit/actions.py", "_timeline_create_variant_from_ranges", "SetStartTimecode"): 1,
    ("src/domains/timeline_edit/actions.py", "_timeline_render_in_place_impl", "SetCurrentRenderMode"): 1,
    ("src/domains/timeline_edit/actions.py", "_timeline_thumbnail_contact_sheet", "SetCurrentTimecode"): 1,
    ("src/domains/timeline_edit/utils/timeline_kernel_live_probe.py", "run_probe", "SetClipsLinked"): 1,
    ("src/granular/media_pool.py", "setup_multicam_timeline", "SetStartTimecode"): 1,
    ("src/server.py", "_resolve_restore_state", "SetCurrentFolder"): 1,
    ("src/server.py", "_resolve_restore_state", "SetCurrentTimecode"): 1,
    ("src/server.py", "_resolve_restore_state", "SetSelectedClip"): 1,
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


if __name__ == "__main__":
    unittest.main()
