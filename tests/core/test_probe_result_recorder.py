"""The shared probe result recorder asserts, it does not declare (#119 tasks 8, 9).

Before this, `_record_tool_result` existed in eleven copies across seven divergent
variants, and six of them returned the caller's `expected_status` on *every* branch:

    errored        -> recorded status 'unsupported'
    half_failed    -> recorded status 'unsupported'
    FULLY_WORKED   -> recorded status 'unsupported'

so the real outcome was discarded. Two things became invisible: a genuine new fault
in a step declared `expected_status="unsupported"`, and a capability Blackmagic
newly ships. Neither ever incremented the `error` count, so the harness exit gate
(`if report["counts"].get("error", 0): return 1`) could not see them either.

These tests pin the corrected contract: the observation is made first, the
expectation is compared against it, and a mismatch **in either direction** is an
`error` that fails the harness.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from src.domains.timeline_edit.utils.timeline_kernel_probe import (  # noqa: E402
    PROBE_STATUSES,
    ProbeRecorder,
    observe_result,
    record_tool_result,
)


def _statuses(recorder):
    return [r["status"] for r in recorder.records]


class ObserveResultTest(unittest.TestCase):
    """Classification from the result alone — no expectation involved."""

    def test_non_dict_is_an_error(self):
        self.assertEqual("error", observe_result(None)["status"])
        self.assertEqual("error", observe_result("boom")["status"])

    def test_error_key_is_an_error(self):
        self.assertEqual("error", observe_result({"error": "Resolve connection lost"})["status"])

    def test_success_false_is_partial(self):
        self.assertEqual("partially_supported", observe_result({"success": False})["status"])

    def test_readback_contradiction_is_partial(self):
        obs = observe_result({"success": True, "verified": False})
        self.assertEqual("partially_supported", obs["status"])
        self.assertIn("readback", obs["reason"])

    def test_a_failed_sub_result_is_partial(self):
        obs = observe_result({"success": True, "results": [{"success": True},
                                                           {"success": False}]})
        self.assertEqual("partially_supported", obs["status"])

    def test_plain_success_is_supported(self):
        self.assertEqual("supported", observe_result({"success": True})["status"])
        self.assertEqual("supported", observe_result({"node_count": 7})["status"])

    def test_every_status_it_can_emit_is_a_known_probe_status(self):
        for result in (None, {"error": "x"}, {"success": False}, {"success": True}):
            with self.subTest(result=result):
                self.assertIn(observe_result(result)["status"], PROBE_STATUSES)


class ExpectedStatusAssertsTest(unittest.TestCase):
    """The §3 finding, restated as tests."""

    def setUp(self):
        self.recorder = ProbeRecorder()

    def _record(self, result, **kwargs):
        return record_tool_result(self.recorder, "c", "step", result, **kwargs)

    def test_the_three_outcomes_no_longer_collapse_to_one_status(self):
        """The issue's own reproduction: all three used to record 'unsupported'."""
        self._record({"error": "Resolve connection lost"}, expected_status="unsupported")
        self._record({"success": False}, expected_status="unsupported")
        self._record({"success": True, "node_count": 7}, expected_status="unsupported")

        # Was: unsupported / unsupported / unsupported — every outcome masked.
        # Now: the dropped connection and the unexpected success both reach the gate.
        self.assertEqual(["error", "unsupported", "error"], _statuses(self.recorder))
        self.assertEqual(2, self.recorder.counts()["error"])

    def test_an_unexpected_success_is_an_error_and_says_why(self):
        record = self._record({"success": True, "node_count": 7},
                              expected_status="unsupported")
        self.assertEqual("error", record["status"])
        self.assertEqual("supported", record["details"]["observed"])
        self.assertEqual("unsupported", record["details"]["expected_status"])
        self.assertIn("api-limitations", record["details"]["reason"])

    def test_an_unexpected_success_increments_the_error_count_the_gate_reads(self):
        self._record({"success": True}, expected_status="unsupported")
        self.assertEqual(1, self.recorder.counts()["error"])

    def test_a_confirmed_boundary_keeps_the_real_observation_as_evidence(self):
        record = self._record({"error": "not supported on this version"},
                              expected_status="unsupported")
        self.assertEqual("unsupported", record["status"])
        self.assertEqual("error", record["details"]["observed"])
        self.assertEqual(0, self.recorder.counts()["error"])

    def test_expecting_support_and_getting_an_error_still_records_the_error(self):
        record = self._record({"error": "boom"}, expected_status="supported")
        self.assertEqual("error", record["status"])
        self.assertEqual(1, self.recorder.counts()["error"])

    def test_expected_boundary_is_the_same_claim_as_expected_status_unsupported(self):
        a = record_tool_result(self.recorder, "c", "a", {"success": True},
                               expected_boundary=True)
        b = record_tool_result(self.recorder, "c", "b", {"success": True},
                               expected_status="unsupported")
        self.assertEqual(a["status"], b["status"])
        self.assertEqual("error", a["status"])

    def test_an_unknown_expected_status_is_rejected_at_the_callsite(self):
        with self.assertRaises(ValueError):
            self._record({"success": True}, expected_status="probably_fine")

    def test_a_soft_fail_claim_is_confirmed_by_any_soft_fail_observation(self):
        """Which flavour of not-working a boundary produces is not adjudicable here.

        The probe author knows whether a rejection is a version gate or a page gate;
        the recorder only sees `success=False`. Enforcing the exact flavour would be
        false precision, so only the success axis is decisive.
        """
        for expected in ("unsupported", "partially_supported",
                         "version_or_page_dependent", "read_only"):
            with self.subTest(expected=expected):
                rec = ProbeRecorder()
                record_tool_result(rec, "c", "step", {"success": False},
                                   expected_status=expected)
                self.assertEqual([expected], _statuses(rec))
                self.assertEqual(0, rec.counts()["error"])

    def test_an_api_rejection_confirms_a_boundary_claim(self):
        """`{"error": "SetX requires Resolve 20+"}` is the API answering, not a crash."""
        record = self._record({"error": "AddFlag requires DaVinci Resolve 20+"},
                              expected_status="unsupported")
        self.assertEqual("unsupported", record["status"])
        self.assertEqual(0, self.recorder.counts()["error"])


class InfrastructureFailureNeverConfirmsTest(unittest.TestCase):
    """§3 consequence 1, by name: 'connection lost' must not record as expected."""

    def setUp(self):
        self.recorder = ProbeRecorder()

    def test_connection_lost_is_an_error_even_when_a_boundary_was_expected(self):
        record = record_tool_result(
            self.recorder, "c", "step",
            {"error": "Could not connect to DaVinci Resolve. It was not running and "
                      "auto-launch failed."},
            expected_status="unsupported")
        self.assertEqual("error", record["status"])
        self.assertTrue(record["details"]["infrastructure"])
        self.assertEqual(1, self.recorder.counts()["error"])

    def test_a_non_dict_result_is_an_error_even_when_a_boundary_was_expected(self):
        record = record_tool_result(self.recorder, "c", "step", None,
                                    expected_boundary=True)
        self.assertEqual("error", record["status"])
        self.assertEqual(1, self.recorder.counts()["error"])

    def test_each_infrastructure_marker_defeats_an_expectation(self):
        for message in ("Resolve connection lost mid-probe",
                        "Resolve is busy with a long operation: render",
                        "Traceback (most recent call last): ...",
                        "unexpected exception in dispatch"):
            with self.subTest(message=message):
                rec = ProbeRecorder()
                record_tool_result(rec, "c", "step", {"error": message},
                                   expected_status="unsupported")
                self.assertEqual(1, rec.counts()["error"])

    def test_a_structured_error_envelope_is_unwrapped_before_matching(self):
        rec = ProbeRecorder()
        record_tool_result(
            rec, "c", "step",
            {"error": {"message": "Could not connect to DaVinci Resolve.",
                       "code": "NOT_CONNECTED", "category": "connection"}},
            expected_status="unsupported")
        self.assertEqual(1, rec.counts()["error"])


class NoExpectationTest(unittest.TestCase):
    """Without a claim, the observation is recorded verbatim."""

    def setUp(self):
        self.recorder = ProbeRecorder()

    def test_records_what_happened(self):
        for result, expected in (
            ({"success": True}, "supported"),
            ({"success": False}, "partially_supported"),
            ({"error": "x"}, "error"),
            (None, "error"),
        ):
            with self.subTest(result=result):
                rec = ProbeRecorder()
                record_tool_result(rec, "c", "step", result)
                self.assertEqual([expected], _statuses(rec))

    def test_partial_on_false_false_downgrades_to_unsupported(self):
        record_tool_result(self.recorder, "c", "step", {"success": False},
                           partial_on_false=False)
        self.assertEqual(["unsupported"], _statuses(self.recorder))

    def test_extra_boundary_check_can_reclassify_a_nominal_success(self):
        record_tool_result(
            self.recorder, "c", "step", {"imported": 0},
            expected_boundary=True,
            extra_boundary_check=lambda r: "zero imported" if r.get("imported") == 0 else None,
        )
        self.assertEqual(["unsupported"], _statuses(self.recorder))
        self.assertEqual(0, self.recorder.counts()["error"])

    def test_extra_boundary_check_is_not_consulted_for_a_failed_call(self):
        called = []
        record_tool_result(self.recorder, "c", "step", {"error": "boom"},
                           extra_boundary_check=lambda r: called.append(r))
        self.assertEqual([], called)


class ClassifyAsIsADowngradeNotAClaimTest(unittest.TestCase):
    """The distinction the live suite forced out on the first run after task 8.

    `live_timeline_conform_validation` calls `probe_interchange_roundtrip`, which
    *succeeds*, and then compares the re-imported timeline against the original and
    finds lost media links. "The call worked" and "the capability works" genuinely
    differ there, and the recorder cannot see the difference. Encoding that as
    `expected_status="partially_supported"` made it look like a falsified claim and
    failed the harness; it is a downgrade with evidence, which is a different thing.

    The lever is deliberately one-directional: it can only move a success down. An
    upgrade would launder a failure into a pass — §3's defect wearing a new hat.
    """

    def setUp(self):
        self.recorder = ProbeRecorder()

    def test_a_successful_call_can_be_downgraded_with_a_reason(self):
        record = record_tool_result(
            self.recorder, "interchange.roundtrip", "roundtrip_fcpxml",
            {"success": True, "comparison": {"difference_count": 3}},
            classify_as="partially_supported",
            classification_reason="re-imported timeline differs in 3 places")

        self.assertEqual("partially_supported", record["status"])
        self.assertEqual("supported", record["details"]["observed"])
        self.assertEqual("re-imported timeline differs in 3 places",
                         record["details"]["classification_reason"])
        self.assertEqual(0, self.recorder.counts()["error"])

    def test_a_downgrade_must_carry_a_reason(self):
        with self.assertRaises(ValueError):
            record_tool_result(self.recorder, "c", "step", {"success": True},
                               classify_as="partially_supported")

    def test_it_cannot_launder_a_failure_into_a_pass(self):
        with self.assertRaises(ValueError):
            record_tool_result(self.recorder, "c", "step", {"error": "boom"},
                               classify_as="supported",
                               classification_reason="looks fine to me")

    def test_it_cannot_be_used_on_a_call_that_already_failed(self):
        """Nothing to downgrade — the observation already says so."""
        with self.assertRaises(ValueError):
            record_tool_result(self.recorder, "c", "step", {"success": False},
                               classify_as="unsupported",
                               classification_reason="whatever")

    def test_an_unknown_classification_is_rejected(self):
        with self.assertRaises(ValueError):
            record_tool_result(self.recorder, "c", "step", {"success": True},
                               classify_as="mostly_ok", classification_reason="x")

    def test_a_reason_without_a_classification_is_rejected(self):
        with self.assertRaises(ValueError):
            record_tool_result(self.recorder, "c", "step", {"success": True},
                               classification_reason="orphaned reason")

    def test_expected_status_still_asserts_alongside_the_new_lever(self):
        """The task-8 guarantee is unchanged for genuine claims."""
        record = record_tool_result(self.recorder, "c", "boundary", {"success": True},
                                    expected_status="unsupported")
        self.assertEqual("error", record["status"])


class SingleDefinitionTest(unittest.TestCase):
    """§4: eleven copies is the bug — guard the collapse structurally."""

    _PROBE_GLOB = "src/domains/*/utils/*_live_probe.py"

    def _probe_files(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        return sorted(root.glob(self._PROBE_GLOB))

    def test_no_probe_reimplements_the_recorder(self):
        """A local `def _record_tool_result` may only delegate, never re-derive."""
        import ast

        for path in self._probe_files():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in tree.body:
                if not (isinstance(node, ast.FunctionDef)
                        and node.name == "_record_tool_result"):
                    continue
                with self.subTest(probe=path.name):
                    calls = {
                        n.func.id
                        for n in ast.walk(node)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    }
                    self.assertIn(
                        "record_tool_result", calls,
                        f"{path.name} defines its own recorder instead of delegating "
                        f"to timeline_kernel_probe.record_tool_result (#119 task 9)")
                    self.assertNotIn(
                        "recorder.record",
                        ast.unparse(node),
                        f"{path.name} calls recorder.record directly — that is the "
                        f"copy-paste shape this collapse removed")

    def test_the_probe_files_were_actually_found(self):
        """A glob that matches nothing would make the test above vacuous."""
        self.assertGreaterEqual(len(self._probe_files()), 10)


if __name__ == "__main__":
    unittest.main()
