"""Test helpers for the structured error envelope landed in A1.

The legacy shape was `{"error": "<prose>"}`. The new shape is
`{"error": {"message": str, "code": str, "category": str,
            "retryable": bool, "remediation": str?}}`.

These helpers let tests assert against the new shape without each test
needing to know the full structure.
"""
from __future__ import annotations
from typing import Any, Dict


def err_message(result: Dict[str, Any]) -> str:
    """Return the human-readable message from a result error, whatever the shape."""
    err = result.get("error") if isinstance(result, dict) else None
    if isinstance(err, dict):
        return str(err.get("message", ""))
    if isinstance(err, str):
        return err
    return ""


def err_code(result: Dict[str, Any]) -> str:
    err = result.get("error") if isinstance(result, dict) else None
    return str(err.get("code", "")) if isinstance(err, dict) else ""


def err_category(result: Dict[str, Any]) -> str:
    err = result.get("error") if isinstance(result, dict) else None
    return str(err.get("category", "")) if isinstance(err, dict) else ""


def is_err(result: Dict[str, Any]) -> bool:
    return isinstance(result, dict) and "error" in result and bool(result["error"])


def assert_error_mentions(case, result: Dict[str, Any], *fragments: str) -> str:
    """Assert `result` failed FOR THE STATED REASON, not merely that it failed.

    `assertIn("error", result)` is the shape #121 §3 calls
    "error-envelope-passes-for-the-wrong-reason": it holds whatever went wrong,
    so a test written for `install(language="ruby")` keeps passing after the tool
    starts rejecting every name, every category, or every call. The sweep found
    82 such assertions.

    Pass the fragments that identify the specific failure — usually the invalid
    value and the parameter it belongs to. Matching is case-insensitive because
    the messages are prose, not identifiers.

        assert_error_mentions(self, r, "language", "ruby")

    Returns the message, so a caller can make further assertions on it.
    """
    case.assertTrue(
        is_err(result),
        f"expected an error result, got: {result!r}",
    )
    message = err_message(result)
    lowered = message.lower()
    missing = [f for f in fragments if f.lower() not in lowered]
    case.assertEqual(
        [], missing,
        f"error message does not identify the expected cause {missing!r} — "
        f"the call may be failing for an unrelated reason. Got: {message!r}",
    )
    return message
