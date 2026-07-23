"""Offline oracle + unit tests for src/domains/audio_fairlight/utils/subtitle_codec (issues #22/#30).

The two embedded ground-truth blobs are real subtitle cue EffectFiltersBA
payloads exported from Resolve 21.0.2.4 (captured by tests/live_subtitle_probe.py,
pinned by tests/live_srt_import_probe.py's oracle):
  cue1 text="HELLO ALPHA CUE", cue2="SECOND BRAVO CUE".
Re-encoding cue1 with cue2's text MUST byte-match cue2 — that single check pins
the whole codec (zstd framing, protobuf cascade, BMD length fields).

Style tests (3.2.6) exercise the same blobs: template style is
font="Open Sans", size=58.0, color="#ffffff".
"""

import binascii
import unittest

from src.domains.audio_fairlight.utils import subtitle_codec as sc

# Ground truth (same blobs as tests/live_srt_import_probe.py).
CUE1 = ("00000002000000a48128b52ffd602d00cd0400f24a212b804b330726311223153058f3df6"
        "20662fd1f51f8bdc336e83928d1c9ef444200ec9c98ad3d5c3ac046640aa753d6e2b6e216"
        "9bd17ac113261348e36d3812019111078e272a9c2044718cdaabb5a9cd5eac6ef0866534e"
        "2c3e6530a91fa6c56a9d4a874e70b0118eece7ca0c42bf67748c0298e02b98ee73e105c85"
        "592ad303e7c12c202f05001b30a979185c9d8bc4a24a88c9ee08")
CUE2 = ("00000002000000a48128b52ffd602f00cd0400c2ca2029802d660e26311223d54c92b418e"
        "d016efe6f99b1df96449caac9b593461ed0461c02707428cddb2d53a15095e375e395fa61"
        "70050c7b202e64016040652542e48c0c2ff834eed13a9536add58ed10f0eb17c5a7436a31"
        "725567c9b1329469d4fe90d7718f0f076e604314ea9bb53049eb5ec3512d74a129a09b39c"
        "4011da08b3a0b806000d18f9d06a1183ab739160560931d91d01")

zstd_missing = not sc.zstd_available()


@unittest.skipIf(zstd_missing, "zstandard not installed")
class OracleTest(unittest.TestCase):
    def test_text_swap_reproduces_ground_truth_byte_for_byte(self):
        rebuilt = sc.author_cue_effblob(CUE1, "HELLO ALPHA CUE", "SECOND BRAVO CUE")
        self.assertEqual(binascii.unhexlify(rebuilt), binascii.unhexlify(CUE2.upper()))

    def test_arbitrary_lengths_embed_the_text(self):
        for text in ("Hi.", "A considerably longer subtitle line to stress lengths.",
                     "Unicode: café — naïve — 日本語"):
            blob = sc.author_cue_effblob(CUE1, "HELLO ALPHA CUE", text)
            dec = sc._decompress_effblob(blob)
            self.assertIn(text.encode("utf-16-le"), dec, text)

    def test_missing_template_text_raises(self):
        with self.assertRaises(ValueError):
            sc.author_cue_effblob(CUE1, "NOT THE TEMPLATE TEXT", "x")


@unittest.skipIf(zstd_missing, "zstandard not installed")
class StyleTest(unittest.TestCase):
    def test_read_cue_style_of_ground_truth(self):
        style = sc.read_cue_style(CUE1)
        self.assertEqual(style.get("font"), "Open Sans")
        self.assertEqual(style.get("color"), "#ffffff")
        self.assertAlmostEqual(style.get("size"), 58.0, places=2)

    def test_color_override_roundtrips(self):
        blob = sc.author_cue_effblob(CUE1, "HELLO ALPHA CUE", "COLORED", color="#ffcc00")
        style = sc.read_cue_style(blob)
        self.assertEqual(style.get("color"), "#ffcc00")
        self.assertEqual(style.get("font"), "Open Sans")

    def test_size_override_roundtrips(self):
        blob = sc.author_cue_effblob(CUE1, "HELLO ALPHA CUE", "SIZED", size=72)
        style = sc.read_cue_style(blob)
        self.assertAlmostEqual(style.get("size"), 72.0, places=2)

    def test_font_override_roundtrips_including_length_change(self):
        blob = sc.author_cue_effblob(
            CUE1, "HELLO ALPHA CUE", "FONTED", font="DejaVu Sans Mono", size=40)
        style = sc.read_cue_style(blob)
        self.assertEqual(style.get("font"), "DejaVu Sans Mono")
        self.assertAlmostEqual(style.get("size"), 40.0, places=2)
        # Text survived alongside the style edits.
        self.assertIn("FONTED".encode("utf-16-le"), sc._decompress_effblob(blob))

    def test_bad_color_rejected(self):
        with self.assertRaises(ValueError):
            sc.author_cue_effblob(CUE1, "HELLO ALPHA CUE", "x", color="red")

    def test_hex_shaped_text_does_not_shadow_the_real_color(self):
        # Regression (bug_005): cue text with a #rrggbb-shaped substring must not
        # be read as the style color. The real color sits at the leaf tail.
        blob = sc.author_cue_effblob(CUE1, "HELLO ALPHA CUE", "Error #FF0000 in Room #123456")
        style = sc.read_cue_style(blob)
        self.assertEqual(style.get("color"), "#ffffff")  # template's real color
        self.assertEqual(style.get("font"), "Open Sans")
        self.assertAlmostEqual(style.get("size"), 58.0, places=2)

    def test_color_override_survives_hex_shaped_text(self):
        blob = sc.author_cue_effblob(
            CUE1, "HELLO ALPHA CUE", "Set #00ff00 now", color="#ffcc00")
        style = sc.read_cue_style(blob)
        self.assertEqual(style.get("color"), "#ffcc00")
        self.assertEqual(style.get("font"), "Open Sans")


class SrtParseTest(unittest.TestCase):
    SRT = """1
00:00:01,000 --> 00:00:02,500
Hello there

2
00:00:03,000 --> 00:00:04,000
Two lines
joined here

3
00:00:05.000 --> 00:00:06.000
Dot separators too
"""

    def test_parse(self):
        cues = sc.parse_srt(self.SRT)
        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[0], (1.0, 2.5, "Hello there"))
        self.assertEqual(cues[1][2], "Two lines joined here")
        self.assertEqual(cues[2][:2], (5.0, 6.0))

    def test_empty_and_malformed_blocks_skipped(self):
        self.assertEqual(sc.parse_srt("junk\n\n42\nnot a timecode\ntext"), [])


@unittest.skipIf(zstd_missing, "zstandard not installed")
class AuthoringTest(unittest.TestCase):
    def _seq_xml(self):
        cue = ('<Sm2TiGenerator DbId="11111111-1111-1111-1111-111111111111">'
               "<Name>HELLO ALPHA CUE</Name><Start>86424</Start><Duration>36</Duration>"
               f"<EffectFiltersBA>{CUE1}</EffectFiltersBA></Sm2TiGenerator>")
        return ('<Sm2SequenceContainer DbId="s1"><VideoTrackVec/><AudioTrackVec/>'
                '<SubtitleTrackVec><Element><Sm2TiTrack DbId="st1"><SubType>3</SubType>'
                f"<Items><Element>{cue}</Element></Items></Sm2TiTrack></Element>"
                "</SubtitleTrackVec></Sm2SequenceContainer>")

    def test_find_template_cue(self):
        t = sc.find_template_cue(self._seq_xml())
        self.assertIsNotNone(t)
        self.assertEqual(t["text"], "HELLO ALPHA CUE")
        self.assertEqual(t["eff_hex"], CUE1)

    def test_author_subtitle_track_replaces_items(self):
        cues = [(1.0, 2.0, "First cue"), (3.0, 4.5, "Second cue")]
        xml, n = sc.author_subtitle_track(
            self._seq_xml(), cues, fps=24, start_frame=86400)
        self.assertEqual(n, 2)
        self.assertNotIn("HELLO ALPHA CUE</Name>", xml)
        self.assertIn("<Name>First cue</Name>", xml)
        self.assertIn("<Start>86424</Start>", xml)   # 86400 + 1.0*24
        self.assertIn("<Duration>24</Duration>", xml)
        self.assertIn("<Start>86472</Start>", xml)   # 86400 + 3.0*24
        self.assertIn("<Duration>36</Duration>", xml)
        # Each authored cue got a fresh DbId and its own blob.
        self.assertNotIn("11111111-1111-1111-1111-111111111111", xml)

    def test_author_with_style(self):
        xml, _ = sc.author_subtitle_track(
            self._seq_xml(), [(0.0, 1.0, "Styled")], fps=24, start_frame=86400,
            style={"color": "#00ff00", "size": 48})
        m = sc._EFF_RE.search(xml)
        style = sc.read_cue_style(m.group(1))
        self.assertEqual(style.get("color"), "#00ff00")
        self.assertAlmostEqual(style.get("size"), 48.0, places=2)

    def test_append_mode_keeps_existing_cues(self):
        xml, n = sc.author_subtitle_track(
            self._seq_xml(), [(10.0, 11.0, "Appended cue")], fps=24, start_frame=86400,
            mode="append")
        self.assertEqual(n, 1)
        # The template cue survives AND the new cue landed after it.
        self.assertIn("<Name>HELLO ALPHA CUE</Name>", xml)
        self.assertIn("<Name>Appended cue</Name>", xml)
        self.assertLess(xml.index("HELLO ALPHA CUE"), xml.index("Appended cue"))

    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            sc.author_subtitle_track(
                self._seq_xml(), [(0.0, 1.0, "x")], fps=24, start_frame=86400,
                mode="merge")

    def test_no_template_raises(self):
        bare = '<Sm2SequenceContainer DbId="s1"><SubtitleTrackVec/></Sm2SequenceContainer>'
        with self.assertRaises(ValueError):
            sc.author_subtitle_track(bare, [(0.0, 1.0, "x")], fps=24, start_frame=86400)

    def test_builtin_template_authors_cues_into_a_bare_timeline(self):
        # The embedded-template path import_srt uses when the timeline has no
        # subtitle track at all (proven live: synthetic cue survives reimport).
        bare = ('<Sm2SequenceContainer DbId="s3"><VideoTrackVec/><AudioTrackVec/>'
                "<SubtitleTrackVec/><GeometryTrackVec/></Sm2SequenceContainer>")
        seeded = sc.ensure_subtitle_track(bare)
        self.assertIn("<SubtitleTrackVec><Element>", seeded)
        xml, n = sc.author_subtitle_track(
            seeded, [(0.5, 2.0, "Embedded path")], fps=24, start_frame=86400,
            template=sc.builtin_template())
        self.assertEqual(n, 1)
        self.assertIn("<Name>Embedded path</Name>", xml)
        self.assertIn("<Start>86412</Start>", xml)

    def test_embedded_append_on_bare_timeline_does_not_leak_template_cue(self):
        # Regression: mode='append' on a fresh timeline (no existing subtitle
        # track) once leaked the synthetic HELLO ALPHA CUE seed at frame 86400
        # because builtin_track_block seeded <Items> with the template cue and
        # _items_repl's append branch preserved it.
        bare = ('<Sm2SequenceContainer DbId="s4"><VideoTrackVec/><AudioTrackVec/>'
                "<SubtitleTrackVec/><GeometryTrackVec/></Sm2SequenceContainer>")
        seeded = sc.ensure_subtitle_track(bare)
        xml, n = sc.author_subtitle_track(
            seeded, [(0.5, 2.0, "Only cue")], fps=24, start_frame=86400,
            template=sc.builtin_template(), mode="append")
        self.assertEqual(n, 1)
        self.assertIn("<Name>Only cue</Name>", xml)
        self.assertNotIn(sc._TEMPLATE_TEXT, xml)

    def test_ensure_subtitle_track_is_a_noop_when_populated(self):
        xml = self._seq_xml()
        self.assertEqual(sc.ensure_subtitle_track(xml), xml)

    def test_transplant_subtitle_track(self):
        bare = ('<Sm2SequenceContainer DbId="s2"><VideoTrackVec/><AudioTrackVec/>'
                "<SubtitleTrackVec/><GeometryTrackVec/></Sm2SequenceContainer>")
        merged = sc.transplant_subtitle_track(bare, self._seq_xml())
        self.assertIn("<SubtitleTrackVec><Element>", merged)
        t = sc.find_template_cue(merged)
        self.assertIsNotNone(t)


if __name__ == "__main__":
    unittest.main()
