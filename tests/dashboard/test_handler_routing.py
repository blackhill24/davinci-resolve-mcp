"""Request handling on the dashboard's HTTP boundary (#121 task 1).

`src/dashboard/handler.py` was the single largest untested surface in the repo —
8% of 647 statements — and it is the boundary the control panel talks to, so
untested request handling is a different risk class from untested Resolve glue.
The uncovered part was not the Resolve-dependent payload building; it was the
plumbing every route depends on: origin rejection, loopback-only refusals, body
parsing, parameter validation, status codes, and the ETag short-circuit.

None of that needs Resolve, or a socket. `_Exchange` drives a real `Handler`
with `BytesIO` in place of the socket files, which is enough for
`BaseHTTPRequestHandler.send_response` and friends to produce a real HTTP
response that the test then parses.

Deliberately NOT covered here: routes whose body is "call a Resolve-dependent
helper and serialise it". Those are the helpers' own tests; asserting they were
called would be asserting on a mock this file configured itself (#121 §3).
"""
from __future__ import annotations

import http.client
import io
import json
import types
import unittest
from unittest import mock

from src.dashboard import handler as handler_mod
from src.dashboard.handler import Handler


class _Exchange:
    """One request/response against a real Handler, with BytesIO for the socket."""

    def __init__(self, method, path, *, body=None, headers=None, client="127.0.0.1"):
        self.handler = Handler.__new__(Handler)
        raw_body = b"" if body is None else json.dumps(body).encode("utf-8")
        merged = {"Host": "127.0.0.1:8899"}
        merged.update(headers or {})
        if raw_body:
            merged.setdefault("Content-Length", str(len(raw_body)))
        self.handler.headers = merged
        self.handler.path = path
        self.handler.command = method
        self.handler.request_version = "HTTP/1.1"
        self.handler.requestline = f"{method} {path} HTTP/1.1"
        self.handler.client_address = (client, 51234)
        self.handler.server = types.SimpleNamespace(server_address=("127.0.0.1", 8899))
        self.handler.rfile = io.BytesIO(raw_body)
        self.handler.wfile = io.BytesIO()
        self.handler.state = _fake_state()

    def run(self):
        if self.handler.command == "GET":
            self.handler.do_GET()
        else:
            self.handler.do_POST()
        return self._parse()

    def _parse(self):
        raw = self.handler.wfile.getvalue()
        response = http.client.HTTPResponse(_FakeSocket(raw), method=self.handler.command)
        response.begin()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload


class _FakeSocket:
    def __init__(self, raw):
        self._raw = raw

    def makefile(self, *_args, **_kwargs):
        return io.BytesIO(self._raw)


def _fake_state():
    """A DashboardState stand-in — a plain object, never a MagicMock (#119).

    A MagicMock here would answer every attribute, so a route reading a field
    that no longer exists would keep passing.
    """
    state = types.SimpleNamespace()
    state.project_name = "Test Project"
    state.project_id = "test-project"
    state.project_root = "/nonexistent/analysis/test-project"
    state.base_analysis_root = "/nonexistent/analysis"
    import threading

    state.lock = threading.Lock()
    return state


def _error_of(payload):
    return json.loads(payload)["error"]


class OriginGuardTest(unittest.TestCase):
    """The DNS-rebinding/CSRF guard, exercised through the real entry points."""

    def test_get_from_a_foreign_host_is_forbidden(self):
        status, _headers, payload = _Exchange(
            "GET", "/api/boot", headers={"Host": "evil.example"}
        ).run()
        self.assertEqual(403, status)
        self.assertIn("non-localhost", _error_of(payload))

    def test_post_from_a_foreign_origin_is_forbidden(self):
        status, _headers, payload = _Exchange(
            "POST", "/api/mcp/install",
            body={"client_id": "claude-code"},
            headers={"Origin": "http://evil.example"},
        ).run()
        self.assertEqual(403, status)
        self.assertIn("non-localhost", _error_of(payload))

    def test_the_guard_does_not_reject_a_same_origin_request(self):
        # Without this, every assertion above would also hold for a guard that
        # rejected everything.
        status, _headers, _payload = _Exchange(
            "GET", "/", headers={"Origin": "http://127.0.0.1:8899"}
        ).run()
        self.assertEqual(200, status)


class LoopbackOnlyRoutesTest(unittest.TestCase):
    """Privileged POST routes must refuse a non-loopback peer.

    The Host/Origin guard is header-based; this one is peer-address based, and
    each privileged route re-checks it individually — so each is checked here.
    """

    PRIVILEGED = [
        ("/api/browse/directory", {}),
        ("/api/launch/claude-code", {}),
        ("/api/update/apply", {}),
        ("/api/update/rollback", {}),
        ("/api/restart_needed/clear", {}),
        ("/api/mcp/install", {"client_id": "claude-code"}),
        ("/api/mcp/uninstall", {"client_id": "claude-code"}),
    ]

    def test_every_privileged_route_refuses_a_remote_peer(self):
        for path, body in self.PRIVILEGED:
            with self.subTest(path=path):
                status, _headers, payload = _Exchange(
                    "POST", path, body=body, client="10.0.0.9"
                ).run()
                self.assertEqual(403, status)
                self.assertIn("loopback", _error_of(payload).lower())

    def test_a_loopback_peer_gets_past_the_guard(self):
        # The converse. Patched at the route's own helper so nothing is launched;
        # the assertion is that the request was NOT refused as remote.
        with mock.patch.object(
            handler_mod, "_mcp_install_payload", return_value={"success": True, "installed": True}
        ):
            status, _headers, payload = _Exchange(
                "POST", "/api/mcp/install", body={"client_id": "claude-code"}
            ).run()
        self.assertEqual(200, status)
        self.assertTrue(json.loads(payload)["success"])


class ParameterValidationTest(unittest.TestCase):
    def test_mcp_install_without_a_client_id_is_a_400(self):
        status, _headers, payload = _Exchange("POST", "/api/mcp/install", body={}).run()
        self.assertEqual(400, status)
        self.assertIn("client_id", _error_of(payload))

    def test_mcp_uninstall_without_a_client_id_is_a_400(self):
        status, _headers, payload = _Exchange("POST", "/api/mcp/uninstall", body={}).run()
        self.assertEqual(400, status)
        self.assertIn("client_id", _error_of(payload))

    def test_a_blank_client_id_is_rejected_like_a_missing_one(self):
        status, _headers, payload = _Exchange(
            "POST", "/api/mcp/install", body={"client_id": "   "}
        ).run()
        self.assertEqual(400, status)
        self.assertIn("client_id", _error_of(payload))

    def test_clip_export_requires_a_non_empty_list(self):
        for body in ({}, {"clip_ids": []}, {"clip_ids": "not-a-list"}):
            with self.subTest(body=body):
                status, _headers, payload = _Exchange(
                    "POST", "/api/clips/export", body=body
                ).run()
                self.assertEqual(400, status)
                self.assertIn("clip_ids", _error_of(payload))

    def test_clip_export_reports_an_exporter_failure_as_a_500(self):
        with mock.patch.object(
            handler_mod, "export_clip_selection", side_effect=RuntimeError("disk full")
        ):
            status, _headers, payload = _Exchange(
                "POST", "/api/clips/export", body={"clip_ids": ["c1"]}
            ).run()
        self.assertEqual(500, status)
        self.assertIn("disk full", _error_of(payload))

    def test_clip_export_sends_an_attachment_on_success(self):
        with mock.patch.object(
            handler_mod, "export_clip_selection",
            return_value=(b"id,name\n", "text/csv", "clips.csv"),
        ):
            status, headers, payload = _Exchange(
                "POST", "/api/clips/export", body={"clip_ids": ["c1"]}
            ).run()
        self.assertEqual(200, status)
        self.assertEqual("text/csv", headers["Content-Type"])
        self.assertIn('filename="clips.csv"', headers["Content-Disposition"])
        self.assertEqual(b"id,name\n", payload)

    def test_the_update_strategy_falls_back_instead_of_trusting_the_client(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return {"success": True}

        with mock.patch.object(handler_mod, "_update_apply_payload", side_effect=_capture):
            _Exchange("POST", "/api/update/apply", body={"strategy": "rm -rf /"}).run()
        self.assertEqual("refuse_on_dirty", seen["strategy"])

    def test_a_known_update_strategy_is_passed_through(self):
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return {"success": True}

        with mock.patch.object(handler_mod, "_update_apply_payload", side_effect=_capture):
            _Exchange(
                "POST", "/api/update/apply", body={"strategy": "  STASH_IF_NEEDED  "}
            ).run()
        self.assertEqual("stash_if_needed", seen["strategy"])


class BodyParsingTest(unittest.TestCase):
    """`_body()` must never raise — every POST route calls it first."""

    def _handler_with(self, raw, *, content_length=None):
        exchange = _Exchange("POST", "/api/mcp/install")
        exchange.handler.rfile = io.BytesIO(raw)
        length = len(raw) if content_length is None else content_length
        exchange.handler.headers = {"Host": "127.0.0.1:8899", "Content-Length": str(length)}
        return exchange.handler

    def test_no_body_is_an_empty_dict(self):
        self.assertEqual({}, self._handler_with(b"", content_length=0)._body())

    def test_a_missing_content_length_is_an_empty_dict(self):
        handler = self._handler_with(b'{"a": 1}')
        handler.headers = {"Host": "127.0.0.1:8899"}
        self.assertEqual({}, handler._body())

    def test_malformed_json_is_an_empty_dict_not_a_crash(self):
        self.assertEqual({}, self._handler_with(b"{not json")._body())

    def test_a_non_object_json_body_is_an_empty_dict(self):
        # A bare list would otherwise reach `body.get(...)` and raise.
        self.assertEqual({}, self._handler_with(b"[1, 2, 3]")._body())
        self.assertEqual({}, self._handler_with(b'"a string"')._body())

    def test_a_json_object_is_returned_as_is(self):
        self.assertEqual({"a": 1}, self._handler_with(b'{"a": 1}')._body())


class NotFoundTest(unittest.TestCase):
    def test_an_unknown_get_route_is_a_404(self):
        status, _headers, payload = _Exchange("GET", "/api/nope").run()
        self.assertEqual(404, status)
        self.assertIn("Not found", _error_of(payload))

    def test_an_unknown_post_route_is_a_404(self):
        status, _headers, payload = _Exchange("POST", "/api/nope", body={}).run()
        self.assertEqual(404, status)
        self.assertIn("Not found", _error_of(payload))

    def test_a_route_that_raises_becomes_a_500_rather_than_a_dropped_connection(self):
        with mock.patch.object(Handler, "_route_get", side_effect=RuntimeError("kaboom")):
            status, _headers, payload = _Exchange("GET", "/api/boot").run()
        self.assertEqual(500, status)
        self.assertIn("kaboom", _error_of(payload))


class ResponseHelpersTest(unittest.TestCase):
    def test_serve_file_reports_a_missing_file_as_404(self):
        exchange = _Exchange("GET", "/whatever")
        exchange.handler._serve_file("/nonexistent/frame.jpg", content_type="image/jpeg")
        status, _headers, payload = exchange._parse()
        self.assertEqual(404, status)
        self.assertIn("not found", _error_of(payload).lower())

    def test_serve_file_sends_the_bytes_and_the_content_type(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            handle.write(b"\xff\xd8jpegbytes")
            path = handle.name
        try:
            exchange = _Exchange("GET", "/whatever")
            exchange.handler._serve_file(path, content_type="image/jpeg")
            status, headers, payload = exchange._parse()
        finally:
            import os

            os.unlink(path)
        self.assertEqual(200, status)
        self.assertEqual("image/jpeg", headers["Content-Type"])
        self.assertEqual(b"\xff\xd8jpegbytes", payload)

    def test_the_etag_short_circuit_skips_the_body_on_a_match(self):
        first = _Exchange("GET", "/whatever")
        first.handler._json_etag({"success": True, "clips": [1, 2, 3]})
        _status, headers, payload = first._parse()
        etag = headers["ETag"]
        self.assertIn("clips", json.loads(payload))

        second = _Exchange("GET", "/whatever", headers={"If-None-Match": etag})
        second.handler._json_etag({"success": True, "clips": [1, 2, 3]})
        _status2, headers2, payload2 = second._parse()
        body = json.loads(payload2)
        self.assertTrue(body["unchanged"])
        self.assertNotIn("clips", body)
        self.assertEqual(etag, headers2["ETag"])

    def test_a_changed_payload_gets_a_new_etag_and_a_full_body(self):
        # The half that matters: an ETag that never changed would freeze the panel
        # on stale data forever.
        first = _Exchange("GET", "/whatever")
        first.handler._json_etag({"success": True, "clips": [1, 2, 3]})
        etag = first._parse()[1]["ETag"]

        second = _Exchange("GET", "/whatever", headers={"If-None-Match": etag})
        second.handler._json_etag({"success": True, "clips": [1, 2, 3, 4]})
        _status, headers, payload = second._parse()
        self.assertNotEqual(etag, headers["ETag"])
        self.assertEqual([1, 2, 3, 4], json.loads(payload)["clips"])

    def test_the_index_route_serves_the_panel_html(self):
        status, headers, payload = _Exchange("GET", "/").run()
        self.assertEqual(200, status)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"<", payload)
        self.assertEqual(int(headers["Content-Length"]), len(payload))


if __name__ == "__main__":
    unittest.main()
