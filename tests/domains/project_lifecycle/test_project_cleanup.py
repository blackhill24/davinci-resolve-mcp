import unittest

from src.domains.project_lifecycle.utils.project_cleanup import (
    delete_project_safely,
    park_off_fusion_page,
)


class _FakeResolve:
    """Resolve app stand-in that records page changes and can fail on demand.

    Deliberately hand-rolled rather than a MagicMock: the code under test asks
    whether the page *is* Fusion and then whether OpenPage worked, and a mock
    answers both truthily no matter what, so every assertion here would pass
    against a broken implementation (tests/GUARDS.md).
    """

    def __init__(self, page="fusion", open_ok=True, raise_on_get=False,
                 raise_on_open=False):
        self.page = page
        self.open_ok = open_ok
        self.raise_on_get = raise_on_get
        self.raise_on_open = raise_on_open
        self.opened = []

    def GetCurrentPage(self):
        if self.raise_on_get:
            raise RuntimeError("bridge is gone")
        return self.page

    def OpenPage(self, page):
        if self.raise_on_open:
            raise RuntimeError("bridge is gone")
        self.opened.append(page)
        if self.open_ok:
            self.page = page
        return self.open_ok


class _FakeProject:
    def __init__(self, name):
        self._name = name

    def GetName(self):
        return self._name


class _FakePM:
    """Minimal ProjectManager stand-in with scriptable DeleteProject results."""

    def __init__(self, current=None, delete_results=(True,), load_ok=True):
        self.current = current
        self.delete_results = list(delete_results)
        self.load_ok = load_ok
        self.loaded = []
        self.closed = []
        self.delete_calls = 0

    def GetCurrentProject(self):
        return _FakeProject(self.current) if self.current else None

    def LoadProject(self, name):
        self.loaded.append(name)
        if self.load_ok:
            self.current = name
        return self.load_ok

    def CloseProject(self, project):
        self.closed.append(project.GetName())
        self.current = None
        return True

    def DeleteProject(self, name):
        self.delete_calls += 1
        if self.delete_results:
            return self.delete_results.pop(0)
        return False


class DeleteProjectSafelyTests(unittest.TestCase):
    def test_simple_success(self):
        pm = _FakePM(current="other", delete_results=[True])
        out = delete_project_safely(pm, "zz_pilot")
        self.assertTrue(out["success"])
        self.assertEqual(out["attempts"], 1)
        self.assertIsNone(out["leftover"])
        self.assertEqual(pm.loaded, [])

    def test_retry_after_false_then_success(self):
        pm = _FakePM(current="other", delete_results=[False, True])
        out = delete_project_safely(pm, "zz_pilot", delay_seconds=0)
        self.assertTrue(out["success"])
        self.assertEqual(out["attempts"], 2)

    def test_switches_away_when_target_is_current(self):
        pm = _FakePM(current="zz_pilot", delete_results=[True])
        out = delete_project_safely(pm, "zz_pilot", switch_to="real_project",
                                    delay_seconds=0)
        self.assertTrue(out["success"])
        self.assertEqual(pm.loaded, ["real_project"])
        self.assertEqual(pm.closed, [])

    def test_closes_current_without_fallback(self):
        pm = _FakePM(current="zz_pilot", delete_results=[True])
        out = delete_project_safely(pm, "zz_pilot", delay_seconds=0)
        self.assertTrue(out["success"])
        self.assertEqual(pm.closed, ["zz_pilot"])

    def test_close_fallback_when_load_fails(self):
        pm = _FakePM(current="zz_pilot", delete_results=[True], load_ok=False)
        out = delete_project_safely(pm, "zz_pilot", switch_to="real_project",
                                    delay_seconds=0)
        self.assertTrue(out["success"])
        self.assertEqual(pm.loaded, ["real_project"])
        self.assertEqual(pm.closed, ["zz_pilot"])

    def test_reports_leftover_by_name_on_persistent_failure(self):
        pm = _FakePM(current="other", delete_results=[False, False])
        out = delete_project_safely(pm, "zz_pilot", delay_seconds=0)
        self.assertFalse(out["success"])
        self.assertEqual(out["attempts"], 2)
        self.assertEqual(out["leftover"], "zz_pilot")
        self.assertTrue(out["detail"])

    def test_exception_in_delete_is_reported_not_raised(self):
        class _BoomPM(_FakePM):
            def DeleteProject(self, name):
                raise RuntimeError("api wedged")

        pm = _BoomPM(current="other")
        out = delete_project_safely(pm, "zz_pilot", delay_seconds=0)
        self.assertFalse(out["success"])
        self.assertEqual(out["leftover"], "zz_pilot")
        self.assertIn("api wedged", out["detail"])

    def test_never_loads_target_as_fallback(self):
        pm = _FakePM(current="zz_pilot", delete_results=[True])
        delete_project_safely(pm, "zz_pilot", switch_to="zz_pilot",
                              delay_seconds=0)
        self.assertEqual(pm.loaded, [])
        self.assertEqual(pm.closed, ["zz_pilot"])



class ParkOffFusionPageTests(unittest.TestCase):
    """#153/#157: deleting a project from the Fusion page terminates Resolve.

    These are the guard on the one call that stops that happening, so they
    assert on the *page transition*, not merely that nothing raised.
    """

    def test_parks_when_the_page_is_fusion(self):
        resolve = _FakeResolve(page="fusion")
        self.assertTrue(park_off_fusion_page(resolve))
        self.assertEqual(resolve.opened, ["edit"])

    def test_leaves_a_safe_page_alone(self):
        resolve = _FakeResolve(page="color")
        self.assertIs(park_off_fusion_page(resolve), False)
        self.assertEqual(resolve.opened, [], "no need to touch a safe page")

    def test_an_unreadable_page_is_parked_anyway(self):
        """An unknown page might be Fusion. Parking a safe page costs nothing;
        guessing wrong costs the whole application."""
        resolve = _FakeResolve(raise_on_get=True)
        self.assertTrue(park_off_fusion_page(resolve))
        self.assertEqual(resolve.opened, ["edit"])

    def test_no_handle_is_not_an_error(self):
        self.assertIsNone(park_off_fusion_page(None))

    def test_a_failing_openpage_never_raises_into_the_cleanup_path(self):
        self.assertIsNone(park_off_fusion_page(_FakeResolve(raise_on_open=True)))


class DeleteParksOffFusionTests(unittest.TestCase):
    def test_the_page_is_parked_before_delete_is_attempted(self):
        resolve = _FakeResolve(page="fusion")
        pm = _FakePM(current="other", delete_results=[True])
        out = delete_project_safely(pm, "zz_pilot", resolve=resolve)
        self.assertTrue(out["success"])
        self.assertTrue(out["parked_off_fusion"])
        self.assertEqual(resolve.opened, ["edit"])

    def test_ordering_the_park_happens_before_the_delete_call(self):
        """Parking *after* the delete would be useless — Resolve is already gone."""
        order = []
        resolve = _FakeResolve(page="fusion")
        real_open = resolve.OpenPage
        resolve.OpenPage = lambda page: (order.append("park"), real_open(page))[1]
        pm = _FakePM(current="other", delete_results=[True])
        real_delete = pm.DeleteProject
        pm.DeleteProject = lambda name: (order.append("delete"), real_delete(name))[1]
        delete_project_safely(pm, "zz_pilot", resolve=resolve)
        self.assertEqual(order, ["park", "delete"])

    def test_a_caller_with_no_handle_keeps_working(self):
        """The dozen live harnesses that only ever had a `pm` must not break."""
        pm = _FakePM(current="other", delete_results=[True])
        out = delete_project_safely(pm, "zz_pilot")
        self.assertTrue(out["success"])
        self.assertIsNone(out["parked_off_fusion"])

    def test_a_dead_bridge_during_the_park_still_attempts_the_delete(self):
        resolve = _FakeResolve(raise_on_get=True, raise_on_open=True)
        pm = _FakePM(current="other", delete_results=[True])
        out = delete_project_safely(pm, "zz_pilot", resolve=resolve)
        self.assertTrue(out["success"], "the park is best-effort, not a gate")
        self.assertEqual(pm.delete_calls, 1)

if __name__ == "__main__":
    unittest.main()
