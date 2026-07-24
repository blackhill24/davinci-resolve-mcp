"""Tests for the platform crash guards (issue #90).

Timeline.CreateSubtitlesFromAudio hard-crashes the whole Resolve process on
Linux (Studio 21.0.2.4, reproduced 2/2), so subtitle_generation_guard refuses
the call there unless RESOLVE_ALLOW_SUBTITLE_GENERATION opts in. Other
platforms are unproven either way and proceed unguarded.
"""
import unittest
from unittest import mock

from src.core.platform import (
    ENV_ALLOW_SUBTITLE_GENERATION,
    subtitle_generation_guard,
)


class SubtitleGenerationGuardTest(unittest.TestCase):
    def test_blocks_on_linux_by_default(self):
        with mock.patch("src.core.platform.get_platform", return_value="linux"), \
             mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(ENV_ALLOW_SUBTITLE_GENERATION, None)
            guard = subtitle_generation_guard()
        self.assertIsNotNone(guard)
        self.assertEqual(guard["blocked_call"], "Timeline.CreateSubtitlesFromAudio")
        self.assertEqual(guard["override_env"], ENV_ALLOW_SUBTITLE_GENERATION)
        self.assertIn("issues/90", guard["issue"])

    def test_env_opt_in_allows_on_linux(self):
        for value in ("1", "true", "YES"):
            with mock.patch("src.core.platform.get_platform", return_value="linux"), \
                 mock.patch.dict("os.environ", {ENV_ALLOW_SUBTITLE_GENERATION: value}):
                self.assertIsNone(subtitle_generation_guard(), value)

    def test_falsy_env_value_still_blocks(self):
        for value in ("0", "", "no", "false"):
            with mock.patch("src.core.platform.get_platform", return_value="linux"), \
                 mock.patch.dict("os.environ", {ENV_ALLOW_SUBTITLE_GENERATION: value}):
                self.assertIsNotNone(subtitle_generation_guard(), value)

    def test_other_platforms_unguarded(self):
        for platform_name in ("darwin", "windows"):
            with mock.patch("src.core.platform.get_platform", return_value=platform_name):
                self.assertIsNone(subtitle_generation_guard(), platform_name)


if __name__ == "__main__":
    unittest.main()
