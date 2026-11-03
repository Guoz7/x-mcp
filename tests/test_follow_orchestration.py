import unittest

from follow_orchestration import (
    derive_from_ledger,
    initial_policy,
    next_decision,
    observe_event,
)


class FollowOrchestrationTests(unittest.TestCase):
    def test_retry_after_blocks_then_allows_one_calibration(self):
        state = observe_event(
            initial_policy(), code="X_RATE_LIMITED", observed_at_epoch=1000, retry_after_seconds=60
        )
        self.assertFalse(next_decision(state, now_epoch=1059, fresh_target_available=True)["allow_write"])
        decision = next_decision(state, now_epoch=1060, fresh_target_available=True)
        self.assertEqual(decision["wave_size"], 1)
        self.assertTrue(decision["calibration_required"])

    def test_unknown_recovery_never_invents_a_timer(self):
        state = observe_event(initial_policy(), code="X_RATE_LIMITED", observed_at_epoch=1000)
        decision = next_decision(state, now_epoch=100000, fresh_target_available=True)
        self.assertFalse(decision["allow_write"])
        self.assertEqual(decision["reason"], "x_rate_limited_without_recovery_time")

    def test_successes_ramp_only_through_calibration_sizes(self):
        state = initial_policy()
        sizes = []
        for _ in range(4):
            sizes.append(next_decision(state, now_epoch=0, fresh_target_available=True)["wave_size"])
            state = observe_event(state, code="verified_success", observed_at_epoch=0)
        self.assertEqual(sizes, [1, 3, 5, 10])

    def test_new_rate_limit_resets_stable_stage(self):
        state = initial_policy()
        for _ in range(3):
            state = observe_event(state, code="verified_success", observed_at_epoch=0)
        state = observe_event(state, code="X_RATE_LIMITED", observed_at_epoch=100, retry_after_seconds=5)
        self.assertEqual(state["stage_index"], 0)
        self.assertEqual(next_decision(state, now_epoch=105, fresh_target_available=True)["wave_size"], 1)

    def test_uncertain_is_not_rate_recovery(self):
        state = observe_event(initial_policy(), code="ACTION_UNCERTAIN", observed_at_epoch=0)
        decision = next_decision(state, now_epoch=0, fresh_target_available=True)
        self.assertTrue(decision["allow_write"])
        self.assertNotIn("rate", decision["reason"])

    def test_no_fresh_target_never_allows_write(self):
        decision = next_decision(initial_policy(), now_epoch=0, fresh_target_available=False)
        self.assertFalse(decision["allow_write"])
        self.assertEqual(decision["wave_size"], 0)

    def test_ledger_rate_limit_is_durable_policy_input(self):
        state = derive_from_ledger({
            "actions": {
                "a": {
                    "type": "follow_profile", "status": "uncertain",
                    "completed_at": "1970-01-01T00:16:40Z",
                    "error": {"code": "X_RATE_LIMITED", "details": {}},
                }
            }
        })
        self.assertEqual(state["mode"], "manual_recovery")

    def test_ledger_reset_epoch_is_a_durable_recovery_boundary(self):
        state = derive_from_ledger({
            "actions": {
                "a": {
                    "type": "follow_profile", "status": "uncertain",
                    "completed_at": "1970-01-01T00:16:40Z",
                    "error": {
                        "code": "X_RATE_LIMITED",
                        "details": {"rate_limit": {"reset_epoch": 1100}},
                    },
                }
            }
        })
        self.assertEqual(state["cooldown_until_epoch"], 1100.0)
        self.assertEqual(next_decision(state, now_epoch=1099, fresh_target_available=True)["reason"], "cooldown_active")
        self.assertEqual(next_decision(state, now_epoch=1100, fresh_target_available=True)["wave_size"], 1)

    def test_invalid_policy_state_fails_closed(self):
        decision = next_decision(
            {"mode": "cooldwn", "stage_index": 3},
            now_epoch=0,
            fresh_target_available=True,
        )
        self.assertFalse(decision["allow_write"])
        self.assertEqual(decision["reason"], "invalid_policy_state")

    def test_cooldown_without_deadline_fails_closed(self):
        decision = next_decision(
            {"mode": "cooldown", "cooldown_until_epoch": None},
            now_epoch=100,
            fresh_target_available=True,
        )
        self.assertFalse(decision["allow_write"])
        self.assertEqual(decision["reason"], "invalid_cooldown_deadline")

if __name__ == "__main__":
    unittest.main()
