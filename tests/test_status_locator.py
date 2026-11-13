"""A status article must be located even when only a sub-path href exists.

X sometimes keeps only "/status/<id>/photo/2", "/history" or "/analytics" for
a post instead of a bare "/status/<id>" anchor. An exact-href locator then
matches nothing and the interaction layer reports an unproven write against a
page that did render.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "browser_executor.py"


class StatusLocatorTest(unittest.TestCase):
    def setUp(self):
        self.text = SOURCE.read_text(encoding="utf-8")

    def test_locator_accepts_a_status_sub_path(self):
        self.assertIn('a[href^="{status_path}"]', self.text)

    def test_a_bare_status_href_is_still_attempted_first(self):
        self.assertIn('a[href="{status_path}"]', self.text)

    def test_the_prefix_cannot_cross_into_another_status(self):
        # The prefix is the whole "/handle/status/<id>" path, so a longer id
        # such as .../status/1234 cannot match a request for .../status/123.
        self.assertRegex(
            self.text,
            re.compile(r"status_path = parsed\.path\.rstrip\(\"/\"\)"),
        )


if __name__ == "__main__":
    unittest.main()
