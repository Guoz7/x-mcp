"""An interaction must open the status it is about, not the author's profile.

The batch payload names a status in `post_url`; only a follow carries
`profile_url`. Reading the wrong field sent every like and repost to the
author's timeline, where the target article does not exist, so the executor
returned ACTION_UNCERTAIN and X's actual answer (for example a refusal) was
never observed.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server.py"


class InteractionTargetTest(unittest.TestCase):
    def setUp(self):
        self.text = SERVER.read_text(encoding="utf-8")

    def _interaction_call(self) -> str:
        """The interaction batch call, which passes a status target."""
        needle = "arget_url = str("
        start = self.text.index(needle)
        return self.text[max(0, start - 200):start + 900]

    def test_post_url_is_read_before_profile_url(self):
        block = self._interaction_call()
        self.assertIn('payload.get("post_url")', block)

    def test_post_url_is_tried_first_in_the_fallback_chain(self):
        block = self._interaction_call()
        order = [
            block.index('payload.get("post_url")'),
            block.index('payload.get("profile_url")'),
        ]
        self.assertLess(order[0], order[1], "post_url must be preferred over profile_url")

    def test_author_falls_back_to_handle_for_follows(self):
        block = self._interaction_call()
        self.assertRegex(
            block,
            re.compile(r'payload\.get\("author"\)\s*or\s*payload\.get\("handle"'),
        )

    def test_the_interaction_call_no_longer_reads_only_profile_url(self):
        self.assertNotIn(
            'post_url=str(payload.get("profile_url")),',
            self.text,
            "an interaction must not pass the author profile as its target",
        )


if __name__ == "__main__":
    unittest.main()
