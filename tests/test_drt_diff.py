"""Unit tests for the raw container differ (src/utils/drt_diff).

Pure and offline: build two tiny zip archives that stand in for exported
``.drt`` files and assert the diff surfaces exactly the changed entry and the
changed line — the discovery signal issue #14's ground-truth method relies on.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from src.utils import drt_diff


def _write_zip(path: str, entries: dict) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


class DiffContainersTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="drt-diff-test-")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def _pair(self, before: dict, after: dict):
        a = os.path.join(self.dir, "a.drt")
        b = os.path.join(self.dir, "b.drt")
        _write_zip(a, before)
        _write_zip(b, after)
        return a, b

    def test_identical_archives_have_no_changes(self):
        same = {"SeqContainer1.xml": "<Clip Volume='1.0'/>", "project.xml": "<P/>"}
        a, b = self._pair(same, same)
        out = drt_diff.diff_containers(a, b)
        self.assertEqual(out["added"], [])
        self.assertEqual(out["removed"], [])
        self.assertEqual(out["changed"], [])
        self.assertEqual(out["unchanged"], 2)

    def test_changed_text_entry_surfaces_the_edited_field(self):
        a, b = self._pair(
            {"SeqContainer1.xml": "<Clip>\n  <Volume>1.0</Volume>\n</Clip>"},
            {"SeqContainer1.xml": "<Clip>\n  <Volume>0.25</Volume>\n</Clip>"},
        )
        out = drt_diff.diff_containers(a, b)
        self.assertEqual(len(out["changed"]), 1)
        change = out["changed"][0]
        self.assertEqual(change["name"], "SeqContainer1.xml")
        self.assertEqual(change["kind"], "text")
        self.assertTrue(any("0.25" in line for line in change["added_lines"]))
        self.assertTrue(any("1.0" in line for line in change["removed_lines"]))

    def test_added_and_removed_entries(self):
        a, b = self._pair(
            {"gone.xml": "<x/>", "keep.xml": "<k/>"},
            {"keep.xml": "<k/>", "fresh.xml": "<y/>"},
        )
        out = drt_diff.diff_containers(a, b)
        self.assertEqual(out["added"], ["fresh.xml"])
        self.assertEqual(out["removed"], ["gone.xml"])

    def test_name_filter_scopes_the_comparison(self):
        # project.xml churns but is filtered out; only the SeqContainer counts.
        a, b = self._pair(
            {"SeqContainer1.xml": "<v>1</v>", "project.xml": "<a/>"},
            {"SeqContainer1.xml": "<v>2</v>", "project.xml": "<b/>"},
        )
        out = drt_diff.diff_containers(a, b, name_filter="SeqContainer")
        self.assertEqual(len(out["changed"]), 1)
        self.assertEqual(out["changed"][0]["name"], "SeqContainer1.xml")

    def test_binary_entry_reports_hashes_not_text(self):
        a, b = self._pair(
            {"blob.bin": b"\x00\x01\x02"},
            {"blob.bin": b"\x00\x01\x03"},
        )
        out = drt_diff.diff_containers(a, b)
        self.assertEqual(len(out["changed"]), 1)
        change = out["changed"][0]
        self.assertEqual(change["kind"], "binary")
        self.assertNotEqual(change["sha256_before"], change["sha256_after"])

    def test_uuid_renamed_container_pairs_as_a_content_change(self):
        # Resolve names the timeline SeqContainer/<uuid>.xml with a fresh uuid per
        # export; the same timeline with an edit must read as ONE changed entry,
        # not add+remove (the live finding that motivated pair_renamed).
        a, b = self._pair(
            {"SeqContainer/a6eaaacb-b35c-4afe-b4fa-7d1f243f7902.xml": "<Clip><Volume>1.0</Volume></Clip>",
             "project.xml": "<P/>"},
            {"SeqContainer/26280fb1-89c4-40ca-a8df-c1ef777160ff.xml": "<Clip><Volume>0.25</Volume></Clip>",
             "project.xml": "<P/>"},
        )
        out = drt_diff.diff_containers(a, b, name_filter="SeqContainer")
        self.assertEqual(out["added"], [])
        self.assertEqual(out["removed"], [])
        self.assertEqual(len(out["changed"]), 1)
        change = out["changed"][0]
        self.assertEqual(change["name"], "SeqContainer/26280fb1-89c4-40ca-a8df-c1ef777160ff.xml")
        self.assertEqual(change["renamed_from"], "SeqContainer/a6eaaacb-b35c-4afe-b4fa-7d1f243f7902.xml")
        self.assertTrue(any("0.25" in line for line in change["added_lines"]))

    def test_uuid_renamed_but_identical_content_is_unchanged(self):
        # A no-op edit (e.g. SetProperty returned False) → containers differ only
        # by filename; pairing must recognize identical content as unchanged.
        a, b = self._pair(
            {"SeqContainer/a6eaaacb-b35c-4afe-b4fa-7d1f243f7902.xml": "<Clip><Volume>1.0</Volume></Clip>"},
            {"SeqContainer/26280fb1-89c4-40ca-a8df-c1ef777160ff.xml": "<Clip><Volume>1.0</Volume></Clip>"},
        )
        out = drt_diff.diff_containers(a, b, name_filter="SeqContainer")
        self.assertEqual(out["changed"], [])
        self.assertEqual(out["added"], [])
        self.assertEqual(out["removed"], [])
        self.assertEqual(out["unchanged"], 1)

    def test_pair_renamed_can_be_disabled(self):
        a, b = self._pair(
            {"SeqContainer/a6eaaacb-b35c-4afe-b4fa-7d1f243f7902.xml": "<v>1</v>"},
            {"SeqContainer/26280fb1-89c4-40ca-a8df-c1ef777160ff.xml": "<v>2</v>"},
        )
        out = drt_diff.diff_containers(a, b, pair_renamed=False)
        self.assertEqual(out["added"], ["SeqContainer/26280fb1-89c4-40ca-a8df-c1ef777160ff.xml"])
        self.assertEqual(out["removed"], ["SeqContainer/a6eaaacb-b35c-4afe-b4fa-7d1f243f7902.xml"])

    def test_significant_lines_filters_id_churn(self):
        # A real Resolve no-op re-export churns DbId/Sequence uuids but no content;
        # significant_lines() must drop those so a genuine edit stands alone.
        change = {
            "kind": "text",
            "added_lines": [
                '   <Sm2TiTrack DbId="59931842-47f1-4129-a915-9cde01398c92">',
                "    <Sequence>191bf3fa-221e-4ca1-bb29-07a82841afd8</Sequence>",
                "    <Volume>0.25</Volume>",
            ],
            "removed_lines": [
                '   <Sm2TiTrack DbId="71318a4c-c841-42c9-a0d5-89b5beb50cb8">',
                "    <Volume>1.0</Volume>",
            ],
        }
        sig = drt_diff.significant_lines(change)
        self.assertEqual(sig["added"], ["    <Volume>0.25</Volume>"])
        self.assertEqual(sig["removed"], ["    <Volume>1.0</Volume>"])

    def test_significant_lines_empty_when_only_churn(self):
        change = {
            "kind": "text",
            "added_lines": ['<x DbId="6d3a244f-cf51-413c-b6ee-3ba2aabd4353">'],
            "removed_lines": ['<x DbId="2f836199-b902-4e7f-8842-fa25ae7284a4">'],
        }
        sig = drt_diff.significant_lines(change)
        self.assertEqual(sig["added"], [])
        self.assertEqual(sig["removed"], [])

    def test_significant_lines_drops_subtype_churn(self):
        # Verified live on Resolve 21.0.2: exporting the SAME timeline twice with
        # NO edit still flips <SubType> (garbage uninitialized int), which slipped
        # past the id-churn filter and made the ducking probe cry a false "SIGNAL
        # FOUND" on a no-op. It must be treated as churn, not a real edit.
        change = {
            "kind": "text",
            "added_lines": ["    <SubType>3342389</SubType>"],
            "removed_lines": ["    <SubType>1694526720</SubType>"],
        }
        sig = drt_diff.significant_lines(change)
        self.assertEqual(sig["added"], [])
        self.assertEqual(sig["removed"], [])

    def test_non_zip_input_returns_error(self):
        junk = os.path.join(self.dir, "not.drt")
        with open(junk, "w") as fh:
            fh.write("i am not a zip")
        out = drt_diff.diff_containers(junk, junk)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
