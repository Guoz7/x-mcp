"""A dead owner session must not keep the signed-in profile locked forever."""

from __future__ import annotations

import unittest

import server


class FakeBrowser:
    timed_out = False

    def __init__(self, profile=None):
        self.profile = profile

    def close(self):
        pass


class OwnerSessionReclaimTest(unittest.TestCase):
    def _server(self):
        instance = object.__new__(server.XStarter)
        instance._browser_lock = __import__("threading").RLock()
        instance._browser_closed = False
        instance._browsers = {}
        instance._owner_session = None
        instance._owner_profile = "FIXTURE_PROFILE"
        instance._browser_profile_root = None
        instance._browser_namespace = "test"
        instance.bridge = object()
        instance._make_browser = lambda profile: FakeBrowser(profile)
        return instance

    def test_second_session_is_refused_while_the_holder_is_live(self):
        instance = self._server()
        instance._owner_session = "session-a"
        instance._browsers["session-a"] = FakeBrowser()
        instance._browser_for = server.XStarter._browser_for.__get__(instance)
        with self.assertRaises(server.StarterError) as caught:
            instance._browser_for({"_mcp_session_id": "session-b"})
        self.assertEqual(caught.exception.code, "BROWSER_OWNER_EXCLUSIVE")

    def test_a_dead_holder_does_not_lock_out_a_new_session(self):
        instance = self._server()
        instance._owner_session = "session-a"
        # session-a's browser is gone: it never called browser_close.
        instance._browser_for = server.XStarter._browser_for.__get__(instance)
        original = server.Browser
        server.Browser = FakeBrowser
        try:
            browser = instance._browser_for({"_mcp_session_id": "session-b"})
        finally:
            server.Browser = original
        self.assertIsNotNone(browser)
        self.assertEqual(instance._owner_session, "session-b")


if __name__ == "__main__":
    unittest.main()

