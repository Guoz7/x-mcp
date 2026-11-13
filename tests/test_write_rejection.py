"""A refused write is a definite answer, not an unknown outcome.

X reports refusals such as code 283 ("denied by scarecrow") inside an HTTP 200
body. Treating that as uncertain made a policy block look like a lost
acknowledgement: the batch stopped on uncertainty and the ledger recorded a
result nobody could act on.
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from browser_executor import Browser


class WriteRejectionTest(unittest.TestCase):
    def _response(self, status=200, payload=None, raises=False):
        response = Mock()
        response.status = status
        if raises:
            response.json.side_effect = ValueError("no body")
        else:
            response.json.return_value = payload
        return response

    def test_scarecrow_refusal_is_reported_with_its_code(self):
        response = self._response(payload={
            "data": {},
            "errors": [{
                "code": 283,
                "message": "Authorization: User 1 favoriting tweet 2 was denied by scarecrow",
                "kind": "Permissions",
                "extensions": {"code": 283, "kind": "Permissions", "name": "AuthorizationError"},
            }],
        })
        found = Browser._read_write_rejection(response)
        self.assertIsNotNone(found)
        self.assertEqual(str(found.get("code")), "283")
        self.assertIn("scarecrow", str(found.get("message")))

    def test_a_successful_write_is_not_a_rejection(self):
        response = self._response(payload={"data": {"favorite_tweet": "Done"}})
        self.assertIsNone(Browser._read_write_rejection(response))

    def test_an_empty_errors_array_is_not_a_rejection(self):
        response = self._response(payload={"data": {}, "errors": []})
        self.assertIsNone(Browser._read_write_rejection(response))

    def test_a_non_200_status_is_left_to_the_other_handlers(self):
        response = self._response(status=429, payload={"errors": [{"code": 88}]})
        self.assertIsNone(Browser._read_write_rejection(response))

    def test_an_unparseable_body_is_not_a_rejection(self):
        response = self._response(raises=True)
        self.assertIsNone(Browser._read_write_rejection(response))

    def test_a_bare_error_string_still_yields_a_message(self):
        response = self._response(payload={"errors": ["denied"]})
        found = Browser._read_write_rejection(response)
        self.assertIsNotNone(found)
        self.assertIn("denied", str(found.get("message")))


if __name__ == "__main__":
    unittest.main()
