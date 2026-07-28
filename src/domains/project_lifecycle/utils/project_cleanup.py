"""Best-effort deletion of disposable Resolve projects.

DeleteProject is flaky on some Resolve builds: it silently returns False when
the target is (or was very recently) the current project, and occasionally on
the first attempt even when it isn't. Disposable test projects then linger in
the project library. This helper centralizes the mitigation so every
disposable-project flow gets it for free:

1. **park off the Fusion page** when a `resolve` handle is available (see
   below) — this one is not about flakiness, it is about Resolve surviving,
2. make sure the target is not the current project (load a fallback project,
   or close the target if no fallback is available),
3. retry the delete once after a short pause,
4. report the leftover by name when it still fails, so callers can surface it
   instead of silently leaking.

**The page matters more than the retries do (#153, #157).** Deleting a project
while Resolve's UI is on the Fusion page showing that project's composition
does not fail — it *terminates Resolve*, with no dialog, no core dump and
nothing in the journal beyond ordinary Qt chatter. It was bisected to exactly
this call on Studio 21.0.2.4: identical runs differing only in an
`OpenPage("edit")` beforehand exit and survive respectively, 2/2 each. So when
callers can supply a `resolve` handle, the page is parked first.

The handle is optional and the park is best-effort because this helper is
called from a dozen live harnesses that only ever had a `pm`. A caller without
one keeps the old behaviour rather than being broken — but every caller that
can pass `resolve` should, and the two `src/` callers do.
"""

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("davinci-resolve-mcp.project_cleanup")

# Anything but "fusion". Edit is the page every other flow already parks on.
SAFE_DELETE_PAGE = "edit"


def park_off_fusion_page(resolve: Any) -> Optional[bool]:
    """Move Resolve off the Fusion page before a delete. Best-effort.

    Returns True when the page was changed, False when it did not need to be,
    and None when it could not be determined — never raises. A cleanup path
    must not fail because the page query did, and the caller's own delete is
    still worth attempting either way.
    """
    if resolve is None:
        return None
    try:
        current = resolve.GetCurrentPage()
    except Exception as exc:
        logger.warning("Could not read the current page before delete: %s", exc)
        current = None
    if current is not None and str(current).lower() != "fusion":
        return False
    try:
        # Park unconditionally when the page could not be read: an unknown page
        # might be Fusion, and parking off a page that was already safe costs
        # nothing, while guessing wrong costs the whole application.
        return bool(resolve.OpenPage(SAFE_DELETE_PAGE))
    except Exception as exc:
        logger.warning("Could not park off the Fusion page before delete: %s", exc)
        return None


def delete_project_safely(
    pm: Any,
    name: str,
    *,
    resolve: Any = None,
    switch_to: Optional[str] = None,
    retries: int = 1,
    delay_seconds: float = 1.0,
) -> Dict[str, Any]:
    """Delete project `name` via project-manager handle `pm`, working around
    DeleteProject flakiness. Returns {success, attempts, leftover, detail}.

    `resolve`: the Resolve app handle. When given, the UI is parked off the
    Fusion page first — deleting from that page terminates Resolve outright
    (#153/#157). Optional only for backward compatibility with callers that
    have no handle; pass it whenever you can.
    `switch_to`: project to load first when `name` is current (e.g. the
    project that was open before the disposable one was created). Without it,
    the current project is closed instead.
    """
    attempts = 0
    detail = ""
    parked = park_off_fusion_page(resolve)
    try:
        current = None
        try:
            project = pm.GetCurrentProject()
            current = project.GetName() if project else None
        except Exception:
            current = None
        if current == name:
            switched = False
            if switch_to and switch_to != name:
                try:
                    switched = bool(pm.LoadProject(switch_to))
                except Exception:
                    switched = False
            if not switched:
                try:
                    project = pm.GetCurrentProject()
                    if project is not None:
                        pm.CloseProject(project)
                except Exception:
                    pass

        last_error = None
        for attempt in range(1 + max(0, int(retries))):
            attempts = attempt + 1
            try:
                if bool(pm.DeleteProject(name)):
                    return {"success": True, "attempts": attempts, "leftover": None,
                            "detail": "", "parked_off_fusion": parked}
                last_error = "DeleteProject returned False"
            except Exception as exc:
                last_error = str(exc)
            if attempt < retries:
                time.sleep(max(0.0, delay_seconds))
        detail = last_error or "DeleteProject failed"
    except Exception as exc:
        detail = str(exc)
    return {"success": False, "attempts": attempts, "leftover": name, "detail": detail,
            "parked_off_fusion": parked}
