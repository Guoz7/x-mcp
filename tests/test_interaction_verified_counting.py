"""Interaction batch must count every verified write kind, not only follows."""

from __future__ import annotations

import unittest

import server


class InteractionVerifiedCountingTest(unittest.TestCase):
    def test_success_states_cover_all_four_write_kinds(self):
        for state in ("following", "liked", "reposted", "replied"):
            self.assertIn(
                state,
                server._INTERACTION_SUCCESS_STATES,
                "%s is a verified end state and must be countable" % state,
            )

    def test_success_states_map_to_distinct_buckets(self):
        buckets = list(server._INTERACTION_SUCCESS_STATES.values())
        self.assertEqual(len(buckets), len(set(buckets)))

    def test_unknown_state_is_not_a_success_bucket(self):
        self.assertNotIn("uncertain", server._INTERACTION_SUCCESS_STATES)
        self.assertNotIn("pending", server._INTERACTION_SUCCESS_STATES)


if __name__ == "__main__":
    unittest.main()
