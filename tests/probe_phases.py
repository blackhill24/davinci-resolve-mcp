"""Shared phase plumbing for the two-phase GUI probes (issue #154).

Five harnesses leaked a disposable project on every single sweep. The cause was
not a missing `finally`, which is what a reader of the leak report would assume
— every one of them already had a `cleanup` phase that deletes the project
properly. The cause is their *calling convention*: they are interactive probes
whose phases are `setup` → (a human does something in the Resolve GUI) →
`diff` → `cleanup`, and `main()` defaults to `setup` when invoked with no
arguments. `scripts/run_live_suite.py` invokes every harness with no arguments,
so a sweep runs `setup` alone — which is *designed* to leave the project
standing for the human — and the `cleanup` that would have removed it is never
reached. Every sweep, forever, by construction.

So the fix is not to bolt a delete onto `setup`; that would break the probe's
whole reason to exist. It is to give the no-argument invocation its own phase:
build the fixture, then tear it down, because a sweep is exactly the case where
nobody is going to perform the GUI step. Running `setup` explicitly still leaves
the project in place, which is what a person at the keyboard wants.

`run_sweep` is that phase, and its `finally` also covers the second half of
#154's question: a `setup` that fails *after* creating the project used to leak
harder than one that succeeded, since it returned before writing the state file
its cleanup reads. Cleanup here is driven by the callback, not the state file,
so a partial setup is still cleaned up.
"""

from __future__ import annotations

from typing import Any, Callable, Optional


def run_sweep(setup: Callable[[], int], cleanup: Callable[[], Any]) -> int:
    """Run a probe's `setup` and then always tear it down. Returns setup's code.

    Cleanup failure is reported but does not mask setup's verdict: the sweep's
    question is whether the probe's setup path works, and answering "fail"
    because the teardown was untidy would hide the thing being measured.
    """
    try:
        return setup()
    finally:
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001 — teardown must not raise over setup
            print(f"[sweep] cleanup failed: {type(exc).__name__}: {exc}")


def delete_probe_project(resolve: Any, name: str,
                         switch_to: Optional[str] = None) -> bool:
    """Delete a probe's disposable project, or return False if it is already gone.

    Always routed through `delete_project_safely` with the `resolve` handle:
    `DeleteProject` refuses the currently loaded project, and deleting while the
    UI sits on the Fusion page terminates Resolve outright (#153/#157). The
    handle is optional on the helper for backward compatibility, which makes it
    easy to omit by accident — this wrapper exists so no probe can.
    """
    from src.domains.project_lifecycle.utils.project_cleanup import delete_project_safely

    if resolve is None or not name:
        return False
    pm = resolve.GetProjectManager()
    if pm is None:
        return False
    if name not in (pm.GetProjectListInCurrentFolder() or []):
        return False
    outcome = delete_project_safely(pm, name, resolve=resolve, switch_to=switch_to)
    if outcome.get("success"):
        print(f"deleted project {name}")
    else:
        print(f"! could not delete project {name}: {outcome.get('detail')}")
    return bool(outcome.get("success"))
