"""Static guard: the panel's choice dropdowns offer every value the backend accepts.

A `<select>` that is missing one of the backend's allowed values does not fail
loudly — it silently destroys the setting. `setControlValue` assigns
`el.value = 'photo'`; with no matching `<option>` the browser sets the select to
`''` (selectedIndex -1) and the field renders blank. The save handler then posts
the whole preferences block back, so saving ANY neighbouring preference writes
`default_post_operation_page: ''`, which `_normalize_setup_choice` rejects and
falls back to `stay_put`. The user's choice is gone with no error anywhere.

That is exactly what happened when the `photo` page (26 May 2026 API) was added
to all seven backend page lists and not to the dropdown. This guard pins the
dropdown to the backend list so the next page Blackmagic adds cannot repeat it.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
PANEL = ROOT / "src" / "dashboard" / "static" / "panel.html"
KERNEL = ROOT / "src" / "core" / "tool_kernel.py"


def _select_options(source: str, select_id: str) -> list[str]:
    """Values of the <option> elements inside the <select> with this id."""
    match = re.search(rf'<select id="{select_id}">(.*?)</select>', source, re.S)
    assert match, f"could not find <select id=\"{select_id}\"> in panel.html"
    return re.findall(r'<option value="([^"]+)"', match.group(1))


def _post_operation_page_choices() -> list[str]:
    """The allowed list `_media_analysis_effective_preferences` normalizes against."""
    source = KERNEL.read_text(encoding="utf-8")
    match = re.search(
        r'effective\["default_post_operation_page"\] = _normalize_setup_choice\(\s*'
        r'effective\.get\("default_post_operation_page"\),\s*'
        r'\[(.*?)\]',
        source,
        re.S,
    )
    assert match, "could not find the default_post_operation_page allowed list in tool_kernel.py"
    return re.findall(r'"([^"]+)"', match.group(1))


class PanelChoiceDriftTest(unittest.TestCase):
    def test_post_operation_page_dropdown_matches_backend(self):
        panel = PANEL.read_text(encoding="utf-8")
        self.assertEqual(
            _select_options(panel, "prefPostOperationPage"),
            _post_operation_page_choices(),
            "prefPostOperationPage's options drifted from the backend's allowed list; a "
            "value the backend accepts but the dropdown lacks is silently reset to "
            "stay_put on the next save.",
        )


if __name__ == "__main__":
    unittest.main()
