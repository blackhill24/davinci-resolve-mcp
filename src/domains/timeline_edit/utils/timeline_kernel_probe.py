"""Helpers for timeline edit kernel capability probe reports."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import re
from typing import Any, Dict, Iterable, List, Optional


PROBE_STATUSES = {
    "supported",
    "partially_supported",
    "read_only",
    "write_only_unverifiable",
    "version_or_page_dependent",
    "unsupported",
    "not_applicable",
    "error",
}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_timeline_item_property_keys(api_text: str) -> List[str]:
    """Return documented TimelineItem GetProperty/SetProperty keys in doc order."""
    start_marker = "The supported keys with their accepted values are:"
    end_marker = "Values beyond the range will be clipped"
    start = api_text.find(start_marker)
    if start < 0:
        return []
    end = api_text.find(end_marker, start)
    section = api_text[start:end if end >= 0 else None]
    keys: List[str] = []
    for match in re.finditer(r'^\s+"([^"]+)"\s*:', section, flags=re.MULTILINE):
        key = match.group(1)
        if key not in keys:
            keys.append(key)
    return keys


def parse_api_class_methods(api_text: str, class_name: str) -> List[str]:
    """Return method names documented under a top-level API class section."""
    lines = api_text.splitlines()
    start_index: Optional[int] = None
    for index, line in enumerate(lines):
        if line.strip() == class_name:
            start_index = index + 1
            break
    if start_index is None:
        return []

    methods: List[str] = []
    for line in lines[start_index:]:
        stripped = line.strip()
        if stripped and not line.startswith(" ") and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", stripped):
            break
        match = re.match(r"\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if match:
            method = match.group(1)
            if method not in methods:
                methods.append(method)
    return methods


def ordered_unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def values_match(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return bool(actual) is expected
    try:
        return abs(float(actual) - float(expected)) <= 0.001
    except (TypeError, ValueError):
        return actual == expected


class ProbeRecorder:
    """Collects normalized capability probe records and renders reports."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def record(
        self,
        category: str,
        name: str,
        status: str,
        *,
        details: Optional[Dict[str, Any]] = None,
        evidence: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if status not in PROBE_STATUSES:
            raise ValueError(f"unknown probe status: {status}")
        item = {
            "category": category,
            "name": name,
            "status": status,
            "details": details or {},
        }
        if evidence is not None:
            item["evidence"] = evidence
        self.records.append(item)
        return item

    def record_exception(self, category: str, name: str, exc: Exception, *, details: Optional[Dict[str, Any]] = None):
        payload = dict(details or {})
        payload["exception"] = repr(exc)
        return self.record(category, name, "error", details=payload)

    def counts(self) -> Dict[str, int]:
        counts = Counter(record["status"] for record in self.records)
        return {status: counts.get(status, 0) for status in sorted(PROBE_STATUSES)}

    def to_report(self, metadata: Dict[str, Any], artifacts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "metadata": metadata,
            "artifacts": artifacts or {},
            "counts": self.counts(),
            "records": self.records,
        }


# An error that means "the harness itself is broken" rather than "the API said no".
# A probe step is allowed to *expect* a capability boundary; it is never allowed to
# expect the connection to drop or the call to blow up. Matched case-insensitively
# against the error message, so these never satisfy an expectation (#119 §3, whose
# worked example is precisely "Resolve connection lost" recorded as `unsupported`).
_INFRASTRUCTURE_ERROR_MARKERS = (
    "could not connect",
    "connection lost",
    "not connected",
    "auto-launch failed",
    "is not running",
    "resolve is busy",
    "traceback (most recent call last)",
    "unexpected exception",
    "internal error",
)


def _is_infrastructure_error(message: Any) -> bool:
    text = str(message).lower()
    return any(marker in text for marker in _INFRASTRUCTURE_ERROR_MARKERS)


def confirm_and_retry(call, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Drive a destructive action through both halves of its confirm-token gate.

    The guard answers the first call with CONFIRMATION_REQUIRED plus a one-shot
    token, and only does the work on the re-call. A probe that stops at the gate
    records the guard *working* as an error, so run both halves and report the
    second — keeping the gate's preview attached as evidence that it did fire.

    ``call`` is the domain tool, invoked as ``call(action, params)``.
    """
    first = call(action, params)
    if not isinstance(first, dict):
        return first
    token = first.get("confirm_token")
    error = first.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if not token or code != "CONFIRMATION_REQUIRED":
        return first
    second = call(action, {**params, "confirm_token": token})
    if isinstance(second, dict):
        return {**second, "confirm_gate": {"code": code, "preview": first.get("preview")}}
    return second


def observe_result(result: Any) -> Dict[str, Any]:
    """Classify a tool result into a probe status from the result ALONE.

    Deliberately takes no expectation: what the call actually did is separate from
    what the probe hoped it would do. `record_tool_result` composes the two.

    Returns ``{"status": <PROBE_STATUSES member>, "reason": str|None}``, plus
    ``"infrastructure": True`` when the failure is the harness/connection rather
    than the API answering.
    """
    if not isinstance(result, dict):
        return {"status": "error", "reason": "non-dict result",
                "result": repr(result), "infrastructure": True}
    error = result.get("error")
    if error:
        message = error.get("message", error) if isinstance(error, dict) else error
        observation = {"status": "error", "reason": message}
        if _is_infrastructure_error(message):
            observation["infrastructure"] = True
        return observation
    if "success" in result and result["success"] is not True:
        return {"status": "partially_supported", "reason": "success returned false"}
    if result.get("verified") is False:
        return {
            "status": "partially_supported",
            "reason": "readback contradiction — API reported success but verification failed",
        }
    rows = result.get("results")
    if isinstance(rows, list) and any(
        isinstance(row, dict) and row.get("success") is False for row in rows
    ):
        return {"status": "partially_supported", "reason": "a sub-result reported success=false"}
    return {"status": "supported", "reason": None}


def _expectation_confirmed(expected: str, observation: Dict[str, Any]) -> bool:
    """Did the observation bear out the probe's claim?

    Two rules, and deliberately no more:

    * **The success axis is decisive.** A claim that the call would not fully work
      is contradicted by it fully working, and vice versa. Which *flavour* of
      not-working a boundary produces (`error` vs `partially_supported` vs
      `version_or_page_dependent`) is not something an observation can adjudicate —
      the probe author knows that, the recorder does not — so a soft-fail claim is
      confirmed by any soft-fail observation.
    * **Infrastructure failures confirm nothing.** A dropped connection, a crash or
      a non-dict result is the harness breaking, not the API answering, so it can
      never satisfy an expectation regardless of direction. That is the case §3
      calls out by name.
    """
    if observation.get("infrastructure"):
        return False
    observed = observation["status"]
    return (observed == "supported") == (expected == "supported")


def record_tool_result(
    recorder: "ProbeRecorder",
    category: str,
    name: str,
    result: Any,
    *,
    expected_status: Optional[str] = None,
    expected_boundary: bool = False,
    partial_on_false: bool = True,
    extra_boundary_check: Optional[Any] = None,
    classify_as: Optional[str] = None,
    classification_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """The one result recorder for every live probe (#119 tasks 8 and 9).

    Replaces eleven copy-pasted `_record_tool_result` definitions across seven
    divergent variants — a fix applied to one never reached the other ten, which is
    the direct mechanism behind "fixes never stick".

    **`expected_status` asserts; it does not declare.** Six of the eleven copies
    returned the caller's `expected_status` on *all* branches, including full
    success, so the real outcome was discarded: a probe step declared
    `expected_status="unsupported"` recorded `unsupported` whether the call errored,
    half-failed, or worked perfectly. Two regressions were invisible as a result —
    a genuine new fault in such a step, and a capability Blackmagic newly ships
    (which would keep `docs/reference/api-limitations.md` ossified forever).

    Here the observation is made first and compared against the expectation:

    * expectation confirmed -> record the expected status, with the real
      observation kept in ``details["observed"]`` as evidence it was checked;
    * expectation contradicted **in either direction** -> record ``error``, so the
      harness gate (``if report["counts"].get("error", 0): return 1``) fails the
      run. An unexpected *success* is a failure of the expectation, not a pass.

    Parameters
    ----------
    expected_status:
        What the probe claims this call will do (usually ``"unsupported"``).
        ``None`` means "no claim" — record whatever was observed.
    expected_boundary:
        Legacy spelling of ``expected_status="unsupported"``, kept because several
        probes read better that way.
    partial_on_false:
        When a call returns ``success=False`` with no claim attached, record
        ``partially_supported`` (default) or ``unsupported``.
    extra_boundary_check:
        Optional ``(result) -> str|None`` returning a reason when a nominally
        successful result is really a boundary (media_pool's ``imported == 0``).
    classify_as / classification_reason:
        A probe-side **downgrade**, not a claim. Some steps inspect evidence the
        recorder cannot see — the interchange round-trip calls succeed, and the
        probe then compares the re-imported timeline against the original and finds
        lost media links — so "the call worked" and "the capability works" genuinely
        differ. Passing ``classify_as`` records the probe's classification and keeps
        the raw observation in ``details["observed"]``.

        It may only move a **supported** observation to a non-supported status: a
        downgrade is a judgement the probe has evidence for, while an upgrade would
        be laundering a failure into a pass, which is the §3 defect wearing a
        different hat. Anything else raises. A reason is mandatory, so the report
        always says why the raw outcome was not taken at face value.
    """
    if expected_boundary and expected_status is None:
        expected_status = "unsupported"
    if expected_status is not None and expected_status not in PROBE_STATUSES:
        raise ValueError(f"unknown expected_status: {expected_status}")
    if classify_as is not None:
        if classify_as not in PROBE_STATUSES:
            raise ValueError(f"unknown classify_as: {classify_as}")
        if classify_as == "supported":
            raise ValueError(
                "classify_as may only downgrade; upgrading an outcome to 'supported' "
                "would launder a failure into a pass")
        if not classification_reason:
            raise ValueError("classify_as requires a classification_reason")
    elif classification_reason is not None:
        raise ValueError("classification_reason given without classify_as")

    observation = observe_result(result)
    observed = observation["status"]
    details: Dict[str, Any] = {}
    if observation.get("reason"):
        details["reason"] = observation["reason"]
    if "result" in observation:
        details["result"] = observation["result"]

    if observed == "supported" and extra_boundary_check is not None:
        extra_reason = extra_boundary_check(result)
        if extra_reason:
            observed = "unsupported"
            observation = dict(observation, status=observed, reason=extra_reason)
            details["reason"] = extra_reason

    if classify_as is not None:
        if observed != "supported":
            raise ValueError(
                f"classify_as is a downgrade lever, but the call already observed "
                f"{observed!r}; drop it and let the observation stand")
        details["observed"] = observed
        details["classification_reason"] = classification_reason
        observed = classify_as
        observation = dict(observation, status=observed)

    evidence = result if isinstance(result, dict) else None

    # No claim attached — record what happened.
    if expected_status is None:
        if observed == "partially_supported" and not partial_on_false:
            observed = "unsupported"
        return recorder.record(category, name, observed, details=details or None,
                               evidence=evidence)

    details["expected_status"] = expected_status
    details["observed"] = observed

    if _expectation_confirmed(expected_status, observation):
        return recorder.record(category, name, expected_status, details=details,
                               evidence=evidence)

    # Contradicted. Either the harness broke, the step regressed, or the capability
    # started working — all three must reach the exit gate, none may be swallowed.
    note = f"expectation not met: declared {expected_status!r}, observed {observed!r}"
    if observation.get("infrastructure"):
        note += (" — infrastructure failure (connection/crash), which can never "
                 "satisfy an expectation")
        details["infrastructure"] = True
    elif observed == "supported":
        note += (" — the capability now works; update the probe and "
                 "docs/reference/api-limitations.md")
    if observation.get("reason"):
        details["observed_reason"] = observation["reason"]
    details["reason"] = note
    return recorder.record(category, name, "error", details=details, evidence=evidence)


def render_markdown_report(report: Dict[str, Any]) -> str:
    metadata = report.get("metadata", {})
    counts = report.get("counts", {})
    records = report.get("records", [])
    artifacts = report.get("artifacts", {})

    title = metadata.get("title", "Timeline Edit Kernel Capability Probe")

    lines = [
        f"# {title}",
        "",
        "## Run",
        "",
        f"- Timestamp: `{metadata.get('timestamp_utc', '')}`",
        f"- Resolve: `{metadata.get('product', '')} {metadata.get('version_string', '')}`",
        f"- Python: `{metadata.get('python', '')}`",
        f"- Platform: `{metadata.get('platform', '')}`",
        f"- Project: `{metadata.get('project_name', '')}`",
        "",
        "## Counts",
        "",
    ]
    for status in sorted(PROBE_STATUSES):
        lines.append(f"- `{status}`: {counts.get(status, 0)}")

    if artifacts:
        lines.extend(["", "## Artifacts", ""])
        for key, value in artifacts.items():
            lines.append(f"- `{key}`: `{value}`")

    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        by_category.setdefault(record["category"], []).append(record)

    lines.extend(["", "## Records", ""])
    for category in sorted(by_category):
        lines.extend([f"### {category}", ""])
        lines.append("| Name | Status | Notes |")
        lines.append("|---|---:|---|")
        for record in by_category[category]:
            details = record.get("details", {})
            note_parts = []
            for key in ("reason", "read", "write", "readback", "restore", "page", "item_type"):
                if key in details:
                    note_parts.append(f"{key}={json.dumps(details[key], default=str)}")
            if not note_parts and details:
                note_parts.append(json.dumps(details, default=str, sort_keys=True)[:220])
            notes = "; ".join(note_parts).replace("|", "\\|")
            lines.append(
                f"| `{record['name']}` | `{record['status']}` | {notes} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
