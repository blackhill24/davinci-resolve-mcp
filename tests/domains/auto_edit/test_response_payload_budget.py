"""#206: plan_cut/revise_cut's response must stay well under the MCP host's
per-result token budget even on a large montage. A 107-segment real montage
plan produced a 107,048-character result and the host could not read its own
checkpoint. These test the two pieces the auto_edit action combines to fix
that: cut_ir.compact_plan_for_response (drops the segment list, keeps every
headline field) and the summary renderers' max_rows head/tail cap.
"""
import json
import unittest

from src.domains.auto_edit.utils import auto_edit, cut_ir, montage_edit


def _synthetic_segments(n):
    # A long, repeated editorial note is exactly what blew up the real
    # 107-segment montage — the same shot's description recurring dozens of
    # times across the plan.
    description = (
        "A fairly long editorial note about this specific shot, repeated "
        "because the same clip recurs often across a montage cut list. "
    ) * 3
    return [
        {
            "role": "broll",
            "source_start_frame": i * 100,
            "source_end_frame": i * 100 + 48,
            "record_start_frame": i * 48,
            "look_bucket": "bucket-a",
            "section": "verse",
            "beat_length": 2,
            "evidence": {"description": description, "pacing": "kinetic",
                         "basis": "select_potential"},
        }
        for i in range(n)
    ]


def _synthetic_montage_plan(n):
    plan = cut_ir.make_cut_list(
        segments=_synthetic_segments(n), fps=24.0, brief_id="brief-1", revision=0)
    plan.update({
        "plan_id": "synthetic-plan-250",
        "problems": [],
        "grid_available": True,
        "look_bucket_basis": "scout",
        "look_buckets": {"bucket-a": {"slope": [1, 1, 1]}},
        "flat_footage_clips": [],
        "tempo_bpm": 108.0,
        "onset_count": 240,
    })
    return plan


class CompactPlanForResponseTest(unittest.TestCase):
    def test_drops_segments_keeps_headline_fields(self):
        plan = _synthetic_montage_plan(250)
        compact = cut_ir.compact_plan_for_response(plan)
        self.assertNotIn("segments", compact)
        self.assertEqual(compact["segment_count"], 250)
        for key in ("grid_available", "look_bucket_basis", "flat_footage_clips",
                    "look_buckets", "problems", "plan_id", "estimates"):
            self.assertIn(key, compact, f"{key!r} missing from the compacted plan")

    def test_does_not_mutate_the_original_plan(self):
        plan = _synthetic_montage_plan(5)
        cut_ir.compact_plan_for_response(plan)
        self.assertEqual(len(plan["segments"]), 5)


class ResponsePayloadBudgetTest(unittest.TestCase):
    def test_compact_plan_plus_capped_summary_stays_under_budget(self):
        plan = _synthetic_montage_plan(250)
        compact = cut_ir.compact_plan_for_response(plan)
        summary = montage_edit.render_montage_summary(plan, max_rows=cut_ir.SUMMARY_MAX_ROWS)
        response = {"success": True, "plan_id": plan["plan_id"], "plan": compact, "summary": summary}
        serialized = json.dumps(response)
        # The real 107-segment montage that triggered #206 produced 107,048
        # characters; 250 synthetic segments here is more pathological still
        # (longer, repeated descriptions), and the bound must hold regardless
        # of segment count.
        self.assertLess(len(serialized), 20_000, serialized[:200])

    def test_full_uncapped_summary_still_available_for_get_cut_summary(self):
        plan = _synthetic_montage_plan(250)
        full_summary = montage_edit.render_montage_summary(plan)  # max_rows=None, get_cut_summary's path
        self.assertEqual(full_summary.count("| broll "), 250)
        self.assertNotIn("more cut(s) omitted", full_summary)

    def test_capped_summary_notes_the_omission_and_points_at_get_cut_summary(self):
        plan = _synthetic_montage_plan(250)
        summary = montage_edit.render_montage_summary(plan, max_rows=cut_ir.SUMMARY_MAX_ROWS)
        self.assertEqual(summary.count("| broll "), cut_ir.SUMMARY_MAX_ROWS)
        self.assertIn("more cut(s) omitted", summary)
        self.assertIn('get_cut_summary(plan_id, format="markdown")', summary)

    def test_talking_head_summary_bounded_the_same_way(self):
        excerpt = "word " * 20
        segments = [
            {"role": "speech", "source_start_frame": i * 100, "source_end_frame": i * 100 + 48,
             "record_start_frame": i * 48, "transcript_excerpt": excerpt}
            for i in range(120)
        ]
        plan = cut_ir.make_cut_list(segments=segments, fps=24.0, brief_id="brief-2", revision=0)
        plan["plan_id"] = "synthetic-talking-head"
        capped = auto_edit.render_cut_summary(plan, max_rows=cut_ir.SUMMARY_MAX_ROWS)
        uncapped = auto_edit.render_cut_summary(plan)
        self.assertLess(len(capped), len(uncapped))
        self.assertIn("more cut(s) omitted", capped)
        self.assertEqual(uncapped.count(excerpt), 120)
        self.assertEqual(capped.count(excerpt), cut_ir.SUMMARY_MAX_ROWS)


if __name__ == "__main__":
    unittest.main()
