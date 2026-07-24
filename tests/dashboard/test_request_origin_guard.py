"""Host/Origin guard on the dashboard handler (DNS-rebinding / CSRF).

The dashboard binds loopback, but the user's browser is a loopback client too:
any web page it renders can fire form/fetch POSTs at 127.0.0.1, and a DNS
rebinding page (attacker hostname resolving to 127.0.0.1) can read responses.
``_request_origin_ok`` closes both by requiring a localhost Host header and,
when a browser supplies one, a localhost Origin.
"""

import types

from src.dashboard.state import _header_hostname, _request_origin_ok


def _fake_handler(headers=None, bound="127.0.0.1"):
    handler = types.SimpleNamespace()
    handler.headers = headers or {}
    handler.server = types.SimpleNamespace(server_address=(bound, 8899))
    return handler


# ── _header_hostname ─────────────────────────────────────────────────────────

def test_header_hostname_plain_host():
    assert _header_hostname("127.0.0.1:8899") == "127.0.0.1"
    assert _header_hostname("localhost") == "localhost"
    assert _header_hostname("LOCALHOST:80") == "localhost"


def test_header_hostname_ipv6_bracketed():
    assert _header_hostname("[::1]:8899") == "::1"


def test_header_hostname_origin_url():
    assert _header_hostname("http://127.0.0.1:8899") == "127.0.0.1"
    assert _header_hostname("http://evil.example") == "evil.example"


def test_header_hostname_garbage():
    assert _header_hostname("") is None


# ── happy paths ──────────────────────────────────────────────────────────────

def test_no_host_no_origin_allowed():
    # curl / raw clients — no browser mediation, loopback bind gates access.
    assert _request_origin_ok(_fake_handler({}))


def test_localhost_host_allowed():
    for host in ("127.0.0.1:8899", "localhost:8899", "[::1]:8899", "127.0.0.1"):
        assert _request_origin_ok(_fake_handler({"Host": host})), host


def test_same_origin_browser_request_allowed():
    handler = _fake_handler({"Host": "127.0.0.1:8899", "Origin": "http://127.0.0.1:8899"})
    assert _request_origin_ok(handler)
    handler = _fake_handler({"Host": "localhost:8899", "Origin": "http://localhost:8899"})
    assert _request_origin_ok(handler)


# ── attacks ──────────────────────────────────────────────────────────────────

def test_dns_rebinding_host_blocked():
    # Attacker domain resolves to 127.0.0.1; Host still names the attacker.
    assert not _request_origin_ok(_fake_handler({"Host": "rebind.evil.example:8899"}))


def test_cross_site_origin_blocked():
    handler = _fake_handler({"Host": "127.0.0.1:8899", "Origin": "https://evil.example"})
    assert not _request_origin_ok(handler)


def test_null_origin_blocked():
    # Sandboxed-iframe / file:// pages send Origin: null — still attacker-reachable.
    handler = _fake_handler({"Host": "127.0.0.1:8899", "Origin": "null"})
    assert not _request_origin_ok(handler)


def test_lookalike_host_blocked():
    assert not _request_origin_ok(_fake_handler({"Host": "localhost.evil.example"}))
    assert not _request_origin_ok(_fake_handler({"Host": "127.0.0.1.evil.example"}))


# ── LAN opt-in mode ──────────────────────────────────────────────────────────

def test_non_loopback_bind_disables_guard():
    # Operator explicitly bound a LAN address; legitimate Hosts are unknowable.
    handler = _fake_handler({"Host": "192.168.1.5:8899"}, bound="0.0.0.0")
    assert _request_origin_ok(handler)


def test_handler_wires_guard_into_do_get_and_do_post():
    import inspect

    from src.dashboard.handler import Handler

    assert "_request_origin_ok" in inspect.getsource(Handler.do_GET)
    assert "_request_origin_ok" in inspect.getsource(Handler.do_POST)
