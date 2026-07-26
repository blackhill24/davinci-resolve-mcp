#!/usr/bin/env python3
"""Per-module coverage floors for the two modules #121 task 1 named.

Deliberately NOT a repo-wide floor. #121's own warning is the reason:

    "A module at 90% whose tests assert on mocks they configured themselves is
     worse than one at 40% with real assertions, because the number argues
     against looking at it."

A repo average would also hide exactly the two modules this exists for — the
average was 63% while `src/dashboard/handler.py` sat at 8%. So this checks a
short, named list, each entry with a reason, and it ratchets: raising a floor
after real coverage lands is expected; lowering one is the thing to argue about
in review.

Live-probe modules are excluded by `.coveragerc`, so their numbers are not noise
in the report this reads.

Usage
-----

    .venv/bin/python -m coverage run -m pytest -q
    .venv/bin/python scripts/coverage_floor.py

Exit codes: 0 all floors met, 1 a module fell below its floor, 2 the harness
could not run (usually: no coverage data — run the suite under coverage first).
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# module path -> (floor %, why this module has a floor at all)
FLOORS = {
    "src/dashboard/handler.py": (
        35,
        "the HTTP boundary the control panel talks to — untested request handling "
        "is a different risk class from untested Resolve glue (#121 task 1). Was 8% "
        "before tests/dashboard/test_handler_routing.py.",
    ),
    "src/domains/timeline_edit/actions.py": (
        30,
        "the biggest domain module (2878 statements); its live harnesses cover the "
        "happy paths, so the floor guards the refusal/error branches that only a "
        "faithful double can reach (#121 task 1).",
    ),
}


def _coverage_json() -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "-", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        print("could not read coverage data — run the suite under coverage first:\n"
              "    .venv/bin/python -m coverage run -m pytest -q\n"
              f"{proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(proc.stdout)


def main() -> int:
    data = _coverage_json()
    files = data.get("files", {})
    if not files:
        print("coverage report contains no files — nothing was measured", file=sys.stderr)
        return 2

    failures, report = [], []
    for path, (floor, why) in FLOORS.items():
        entry = files.get(path)
        if entry is None:
            failures.append(f"{path}: not present in the coverage report — moved or renamed? "
                            "Update FLOORS in this script rather than dropping the floor.")
            continue
        percent = entry["summary"]["percent_covered"]
        report.append(f"  {path:52} {percent:5.1f}%  (floor {floor}%)")
        if percent < floor:
            failures.append(f"{path}: {percent:.1f}% is below its {floor}% floor — {why}")

    print("Per-module coverage floors:")
    print("\n".join(report))
    print()
    if failures:
        print("COVERAGE FLOOR FAILED:")
        for line in failures:
            print(f"  - {line}")
        print("\nAdd assertions that exercise the uncovered branches. Do not lower the floor "
              "to make this pass, and do not chase the number with tests that assert on "
              "mocks they configured themselves.")
        return 1
    print("COVERAGE FLOORS PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
