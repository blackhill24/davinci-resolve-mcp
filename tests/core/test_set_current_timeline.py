"""`_set_current_timeline` verifies the switch by read-back (#113 Tier 1).

Resolve targets the CURRENT timeline implicitly in several APIs —
`AppendToTimeline` takes no target argument, render jobs bind to whatever is
current — so a switch that silently fails sends the next mutation to the WRONG
timeline. `timeline_edit._timeline_insert_edit_impl` documents that exact bug
being found live: "the clip silently appended onto the original, colliding with
content the ripple never touched there". Every one of those 18 call sites
discarded `SetCurrentTimeline`'s return value.

Verification is by READ-BACK rather than by trusting the return, per
`src/core/readback.py` ("many setters return True regardless of effect"). That
choice is what makes turning these sites into hard failures safe: a build that
returns a falsy value on a switch that DID take effect still reports True here,
so a working flow cannot start failing. The tests below pin both halves of that
— trust the observed state, not the reported one.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from src.core.timeline_lookup import _set_current_timeline  # noqa: E402


class TimelineStub:
    def __init__(self, uid="tl-1", raise_on_id=False):
        self._uid = uid
        self._raise_on_id = raise_on_id

    def GetUniqueId(self):
        if self._raise_on_id:
            raise RuntimeError("GetUniqueId unavailable")
        return self._uid


class ProjectStub:
    """Models Resolve: SetCurrentTimeline moves `current` unless told otherwise."""

    def __init__(self, *, current=None, reported=True, switch_works=True,
                 set_raises=False, get_raises=False):
        self.current = current
        self.reported = reported
        self.switch_works = switch_works
        self.set_raises = set_raises
        self.get_raises = get_raises
        self.set_calls = []

    def SetCurrentTimeline(self, tl):
        self.set_calls.append(tl)
        if self.set_raises:
            raise RuntimeError("SetCurrentTimeline exploded")
        if self.switch_works:
            self.current = tl
        return self.reported

    def GetCurrentTimeline(self):
        if self.get_raises:
            raise RuntimeError("GetCurrentTimeline unavailable")
        return self.current


class SetCurrentTimelineTest(unittest.TestCase):
    def test_switch_that_takes_effect_reports_true(self):
        tl = TimelineStub("wanted")
        proj = ProjectStub(current=TimelineStub("other"))
        self.assertTrue(_set_current_timeline(proj, tl))
        self.assertIs(proj.current, tl)

    def test_switch_that_silently_fails_reports_false(self):
        """The finding: Resolve says True, the timeline never changed."""
        tl = TimelineStub("wanted")
        proj = ProjectStub(current=TimelineStub("other"), reported=True, switch_works=False)
        self.assertFalse(
            _set_current_timeline(proj, tl),
            "a reported-True switch that did not take effect must not read as success",
        )

    def test_falsy_return_is_overridden_by_a_successful_readback(self):
        """Read-back is the authority — this is what keeps the change safe.

        A Resolve build that returns None/False from SetCurrentTimeline while
        actually switching must NOT turn the 18 Tier-1 call sites into errors.
        """
        tl = TimelineStub("wanted")
        proj = ProjectStub(current=TimelineStub("other"), reported=False, switch_works=True)
        self.assertTrue(_set_current_timeline(proj, tl))

    def test_already_current_reports_true(self):
        tl = TimelineStub("wanted")
        proj = ProjectStub(current=tl)
        self.assertTrue(_set_current_timeline(proj, tl))

    def test_raising_setter_still_verified_by_readback(self):
        """A raise during the set doesn't matter if the state ends up right."""
        tl = TimelineStub("wanted")
        proj = ProjectStub(current=tl, set_raises=True)
        self.assertTrue(_set_current_timeline(proj, tl))

    def test_raising_setter_with_wrong_state_reports_false(self):
        tl = TimelineStub("wanted")
        proj = ProjectStub(current=TimelineStub("other"), set_raises=True)
        self.assertFalse(_set_current_timeline(proj, tl))

    def test_unreadable_current_falls_back_to_the_reported_value(self):
        """No read-back available — the return value is all there is."""
        tl = TimelineStub("wanted")
        self.assertTrue(
            _set_current_timeline(ProjectStub(reported=True, get_raises=True), tl))
        self.assertFalse(
            _set_current_timeline(ProjectStub(reported=False, get_raises=True), tl))

    def test_timeline_without_a_usable_id_falls_back_to_the_reported_value(self):
        tl = TimelineStub(raise_on_id=True)
        self.assertTrue(_set_current_timeline(ProjectStub(reported=True), tl))
        self.assertFalse(_set_current_timeline(ProjectStub(reported=False), tl))

    def test_no_current_timeline_after_the_switch_reports_false(self):
        proj = ProjectStub(current=None, switch_works=False)
        self.assertFalse(_set_current_timeline(proj, TimelineStub("wanted")))

    def test_none_arguments_are_false_and_never_raise(self):
        self.assertFalse(_set_current_timeline(None, TimelineStub()))
        self.assertFalse(_set_current_timeline(ProjectStub(), None))
        self.assertFalse(_set_current_timeline(None, None))

    def test_identity_is_by_id_not_object(self):
        """Resolve hands back a fresh wrapper object for the same timeline."""
        tl = TimelineStub("same-uid")
        proj = ProjectStub(current=TimelineStub("other"), switch_works=False)
        # The switch "fails" to move our object, but a DIFFERENT object with the
        # same id is current — that is the same timeline, so it must read True.
        proj.current = TimelineStub("same-uid")
        self.assertTrue(_set_current_timeline(proj, tl))


if __name__ == "__main__":
    unittest.main()
