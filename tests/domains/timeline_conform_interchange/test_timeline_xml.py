"""Unit tests for the FCP7/xmeml timeline sanitizer (src.domains.timeline_conform_interchange.utils.timeline_xml).

Pure parsing — no DaVinci Resolve connection required.
"""

import os
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET

from src.domains.timeline_conform_interchange.utils.timeline_xml import (
    analyze_timeline_xml,
    match_references,
    sanitize_timeline_xml,
    scan_candidates,
    _pathurl_to_disk,
)


def _xmeml(present_path, missing_path):
    """Build an xmeml with one linked clip, one reference-by-id reuse of it, one
    missing-media clip, one generator (file w/o pathurl), and one no-file clip."""
    present_url = "file://localhost" + urllib.parse.quote(present_path)
    missing_url = "file://localhost" + urllib.parse.quote(missing_path)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
<sequence id="seq1">
<name>TEST_SEQ</name>
<rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
<media>
<video>
<track>
<clipitem id="ci1"><name>good</name><start>0</start><end>100</end><in>0</in><out>100</out>
<file id="f1"><name>good.mov</name><pathurl>{present_url}</pathurl>
<duration>500</duration></file></clipitem>
<clipitem id="ci2"><name>good-reuse</name><start>100</start><end>200</end><in>0</in><out>100</out>
<file id="f1"/></clipitem>
<clipitem id="ci3"><name>missing.mov</name><start>200</start><end>300</end><in>0</in><out>100</out>
<file id="f2"><name>missing.mov</name><pathurl>{missing_url}</pathurl>
<duration>500</duration></file></clipitem>
<clipitem id="ci4"><name>Universal Counting Leader</name><start>300</start><end>400</end>
<file id="f3"><name>Slug</name><mediaSource>Slug</mediaSource></file></clipitem>
<clipitem id="ci5"><name>Title 1</name><start>400</start><end>500</end></clipitem>
</track>
</video>
<audio>
<track>
<clipitem id="ca1"><name>mix.wav</name><start>0</start><end>500</end><in>0</in><out>500</out>
<file id="f2"/></clipitem>
</track>
</audio>
</media>
</sequence>
</xmeml>
"""


class TimelineXmlSanitizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xmltest_")
        # a real present media file
        self.present = os.path.join(self.tmp, "good.mov")
        with open(self.present, "wb") as fh:
            fh.write(b"\x00" * 16)
        self.missing = os.path.join(self.tmp, "does_not_exist.mov")
        self.xml_path = os.path.join(self.tmp, "seq.xml")
        with open(self.xml_path, "w", encoding="utf-8") as fh:
            fh.write(_xmeml(self.present, self.missing))

    def test_pathurl_to_disk(self):
        self.assertEqual(_pathurl_to_disk("file://localhost/a%20b/c.mov"), "/a b/c.mov")
        self.assertEqual(_pathurl_to_disk("file:///a%20b/c.mov"), "/a b/c.mov")
        self.assertIsNone(_pathurl_to_disk(None))

    def test_analyze_counts(self):
        rep = analyze_timeline_xml(self.xml_path)
        self.assertEqual(rep["timeline_name"], "TEST_SEQ")
        # kept: ci1, ci2 (reuse of present file) -> 2 video
        self.assertEqual(rep["kept"], 2)
        # missing: ci3 (video) + ca1 (audio, reference to missing f2) -> 2
        self.assertEqual(rep["missing_media_count"], 2)
        # generators: ci4 (file w/o pathurl) + ci5 (no file) -> 2
        self.assertEqual(rep["generator_count"], 2)
        self.assertTrue(rep["needs_sanitize"])

    def test_sanitize_removes_offending_clips(self):
        res = sanitize_timeline_xml(self.xml_path, out_dir=self.tmp)
        self.assertTrue(os.path.exists(res["output_path"]))
        self.assertEqual(res["kept"], 2)
        self.assertEqual(res["removed_total"], 4)

        root = ET.fromstring(open(res["output_path"], encoding="utf-8").read())
        clip_ids = [ci.get("id") for ci in root.iter("clipitem")]
        # only the present-media clips survive
        self.assertEqual(set(clip_ids), {"ci1", "ci2"})
        # the reference-by-id reuse still resolves to the present file definition
        self.assertIn("f1", [f.get("id") for f in root.iter("file")])

    def test_sanitize_output_is_valid_xml_with_doctype(self):
        res = sanitize_timeline_xml(self.xml_path, out_dir=self.tmp)
        raw = open(res["output_path"], encoding="utf-8").read()
        self.assertIn("<!DOCTYPE xmeml>", raw)
        # parses without error
        ET.fromstring(raw)

    def test_clean_timeline_needs_no_sanitize(self):
        # XML where every clip points at present media
        clean = _xmeml(self.present, self.present).replace("does_not_exist", "good")
        p = os.path.join(self.tmp, "clean.xml")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(clean)
        rep = analyze_timeline_xml(p)
        self.assertEqual(rep["missing_media_count"], 0)

    def test_missing_sequence_raises(self):
        p = os.path.join(self.tmp, "bad.xml")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write('<?xml version="1.0"?><xmeml version="4"></xmeml>')
        with self.assertRaises(ValueError):
            analyze_timeline_xml(p)


class MatchReferencesTest(unittest.TestCase):
    """The name-matching tiers behind relink_search_roots (no Resolve, no disk needed
    beyond the scan test)."""

    def test_exact_beats_looser_tiers(self):
        res = match_references(
            [{"name": "A001_C003.mov"}],
            ["/media/A001_C003.mov", "/media/proxy/a001-c003.mxf"],
        )
        item = res["items"][0]
        self.assertEqual(item["status"], "matched")
        self.assertEqual(item["method"], "exact")
        self.assertEqual(item["assetId"], "/media/A001_C003.mov")

    def test_ext_agnostic_match(self):
        res = match_references([{"name": "shot.mov"}], ["/media/shot.mxf"])
        item = res["items"][0]
        self.assertEqual(item["status"], "matched")
        self.assertEqual(item["method"], "ext_agnostic")
        self.assertEqual(item["confidence"], 0.9)

    def test_normalized_match(self):
        res = match_references([{"name": "A001_C003.mov"}], ["/media/a001-c003.mxf"])
        self.assertEqual(res["items"][0]["method"], "normalized")

    def test_duplicate_names_are_ambiguous_not_guessed(self):
        res = match_references(
            [{"name": "shot.mov"}], ["/vol1/shot.mov", "/vol2/shot.mov"],
        )
        item = res["items"][0]
        self.assertEqual(item["status"], "ambiguous")
        self.assertEqual(item["assetIds"], ["/vol1/shot.mov", "/vol2/shot.mov"])
        self.assertNotIn("assetId", item)

    def test_ambiguity_in_a_strict_tier_does_not_fall_through(self):
        # Two exact hits must NOT be resolved by a looser tier finding one "winner".
        res = match_references(
            [{"name": "shot.mov"}],
            ["/vol1/shot.mov", "/vol2/shot.mov", "/vol3/shot.mxf"],
        )
        self.assertEqual(res["items"][0]["status"], "ambiguous")
        self.assertEqual(res["items"][0]["method"], "exact")

    def test_unmatched(self):
        res = match_references([{"name": "nowhere.mov"}], ["/media/other.mov"])
        self.assertEqual(res["items"][0]["status"], "unmatched")
        self.assertEqual(res["items"][0]["confidence"], 0.0)

    def test_same_candidate_reached_twice_is_not_ambiguous(self):
        # One file listed under two roots' walks would dedupe to a single match.
        res = match_references([{"name": "shot.mov"}], ["/media/shot.mov"] * 2)
        self.assertEqual(res["items"][0]["status"], "matched")


class ScanCandidatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="scantest_")
        os.makedirs(os.path.join(self.tmp, "day1", "cam_a"))
        for rel in ("day1/top.mov", "day1/cam_a/deep.mov"):
            with open(os.path.join(self.tmp, rel), "wb") as fh:
                fh.write(b"\x00")

    def test_scan_finds_nested_media(self):
        scan = scan_candidates([self.tmp])
        names = sorted(os.path.basename(p) for p in scan["candidates"])
        self.assertEqual(names, ["deep.mov", "top.mov"])
        self.assertFalse(scan["truncated"])
        self.assertEqual(scan["roots_missing"], [])

    def test_max_depth_stops_the_walk(self):
        # max_depth counts levels BELOW the root, matching build_relink_plan's scan:
        # 0 = the root dir only, 1 = one subdirectory deep.
        day1 = os.path.join(self.tmp, "day1")
        flat = scan_candidates([day1], max_depth=0)
        self.assertEqual([os.path.basename(p) for p in flat["candidates"]], ["top.mov"])
        deep = scan_candidates([day1], max_depth=1)
        self.assertEqual(sorted(os.path.basename(p) for p in deep["candidates"]),
                         ["deep.mov", "top.mov"])

    def test_missing_root_is_reported_not_raised(self):
        scan = scan_candidates([os.path.join(self.tmp, "nope")])
        self.assertEqual(len(scan["roots_missing"]), 1)
        self.assertEqual(scan["candidates"], [])

    def test_max_files_truncates(self):
        scan = scan_candidates([self.tmp], max_files=1)
        self.assertTrue(scan["truncated"])


class SanitizeRelinkTest(unittest.TestCase):
    """End-to-end: search_roots rescues a missing clip instead of dropping it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relinktest_")
        self.present = os.path.join(self.tmp, "good.mov")
        with open(self.present, "wb") as fh:
            fh.write(b"\x00" * 16)
        # the XML points at this path, which does not exist...
        self.missing = os.path.join(self.tmp, "old_vol", "missing.mov")
        # ...but the same-named file DOES exist under the search root
        self.root = os.path.join(self.tmp, "archive")
        os.makedirs(self.root)
        self.found = os.path.join(self.root, "missing.mov")
        with open(self.found, "wb") as fh:
            fh.write(b"\x00" * 16)
        self.xml_path = os.path.join(self.tmp, "seq.xml")
        with open(self.xml_path, "w", encoding="utf-8") as fh:
            fh.write(_xmeml(self.present, self.missing))

    def test_relink_keeps_the_clip_and_rewrites_the_pathurl(self):
        res = sanitize_timeline_xml(self.xml_path, out_dir=self.tmp,
                                    search_roots=[self.root])
        self.assertEqual(res["relinked_count"], 1)
        self.assertEqual(res["relinked"][0]["new_path"], self.found)
        self.assertEqual(res["relinked"][0]["method"], "exact")
        # ci3 (video) + ca1 (audio reuse of the same file id) both survive now
        self.assertEqual(res["missing_media_count"], 0)
        self.assertEqual(res["kept"], 4)
        root = ET.fromstring(open(res["output_path"], encoding="utf-8").read())
        clip_ids = {ci.get("id") for ci in root.iter("clipitem")}
        self.assertEqual(clip_ids, {"ci1", "ci2", "ci3", "ca1"})
        self.assertEqual(res["scan"]["roots_missing"], [])

    def test_min_confidence_can_reject_a_loose_tier(self):
        os.rename(self.found, os.path.join(self.root, "missing.mxf"))
        strict = sanitize_timeline_xml(self.xml_path, out_dir=self.tmp,
                                       search_roots=[self.root], min_confidence=0.95)
        self.assertEqual(strict["relinked_count"], 0)
        self.assertEqual(strict["missing_media_count"], 2)
        loose = sanitize_timeline_xml(self.xml_path, out_dir=self.tmp,
                                      search_roots=[self.root], min_confidence=0.7)
        self.assertEqual(loose["relinked_count"], 1)
        self.assertEqual(loose["relinked"][0]["method"], "ext_agnostic")

    def test_ambiguous_candidates_are_reported_and_clip_dropped(self):
        second = os.path.join(self.root, "dupe")
        os.makedirs(second)
        with open(os.path.join(second, "missing.mov"), "wb") as fh:
            fh.write(b"\x00")
        res = sanitize_timeline_xml(self.xml_path, out_dir=self.tmp,
                                    search_roots=[self.root])
        self.assertEqual(res["relinked_count"], 0)
        self.assertEqual(res["ambiguous_count"], 1)
        self.assertEqual(len(res["ambiguous"][0]["candidates"]), 2)
        self.assertEqual(res["missing_media_count"], 2)

    def test_no_search_roots_reports_no_scan(self):
        res = sanitize_timeline_xml(self.xml_path, out_dir=self.tmp)
        self.assertIsNone(res["scan"])
        self.assertEqual(res["relinked_count"], 0)


if __name__ == "__main__":
    unittest.main()
