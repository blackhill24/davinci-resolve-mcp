"""#205: _safe_import_media must refuse before touching the media pool when
Resolve is parked on the Project Manager, instead of failing deep inside
folder/import calls with a generic, wrongly-retryable error."""
import tempfile
import unittest

import src.domains.media_pool_ingest.actions as _dom_media_pool_ingest


class _ParkedResolve:
    def GetCurrentPage(self):
        return None


class _OpenPageResolve:
    def GetCurrentPage(self):
        return "edit"


class _ExplodingMediaPool:
    """Any call means the parked guard failed to short-circuit."""

    def __getattr__(self, name):
        raise AssertionError(f"media pool method {name!r} called despite parked Project Manager")


class SafeImportMediaParkedGuardTest(unittest.TestCase):
    def setUp(self):
        self._original_get_resolve = _dom_media_pool_ingest.get_resolve
        self.addCleanup(setattr, _dom_media_pool_ingest, "get_resolve", self._original_get_resolve)

    def test_refuses_before_touching_media_pool_when_parked(self):
        _dom_media_pool_ingest.get_resolve = lambda: _ParkedResolve()
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            result = _dom_media_pool_ingest._safe_import_media(
                _ExplodingMediaPool(), {"paths": [f.name], "target_folder": "Footage"})
        self.assertIn("error", result)
        body = result["error"]
        self.assertEqual(body["code"], "PROJECT_MANAGER_PARKED")
        self.assertEqual(body["category"], "precondition")
        self.assertFalse(body["retryable"])

    def test_dry_run_short_circuits_before_the_parked_check(self):
        # dry_run never touches Resolve at all, parked or not — it should
        # still report what it would import rather than refusing.
        _dom_media_pool_ingest.get_resolve = lambda: _ParkedResolve()
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            result = _dom_media_pool_ingest._safe_import_media(
                _ExplodingMediaPool(),
                {"paths": [f.name], "target_folder": "Footage", "dry_run": True})
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("would_import"), [f.name])


if __name__ == "__main__":
    unittest.main()
