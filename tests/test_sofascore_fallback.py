import unittest
from unittest.mock import patch

import main


class SofaScoreFallbackTests(unittest.TestCase):
    def test_upstream_forbidden_uses_browser_compatible_fetcher(self):
        relay = main.NormalizedResponse(
            403,
            '{"error": {"code": 403, "reason": "Forbidden"}}',
            lambda: {"error": {"code": 403, "reason": "Forbidden"}},
            "home_relay",
            1,
        )
        direct = main.NormalizedResponse(
            200, '{"scheduled": []}', lambda: {"scheduled": []}, "curl_cffi", 1
        )

        with (
            patch.object(main, "SOFASCORE_FETCHER", "home_relay"),
            patch.object(main, "fetch_with_home_relay", return_value=relay),
            patch.object(main, "fetch_sofascore_with_curl_cffi", return_value=direct) as fallback,
        ):
            result = main.choose_fetcher("https://www.sofascore.com/api/v1/example", None)

        self.assertIs(result, direct)
        fallback.assert_called_once()

    def test_relay_auth_failure_does_not_use_fallback(self):
        relay = main.NormalizedResponse(
            401,
            '{"error": {"reason": "invalid_relay_token"}}',
            lambda: {"error": {"reason": "invalid_relay_token"}},
            "home_relay",
            1,
        )

        with (
            patch.object(main, "SOFASCORE_FETCHER", "home_relay"),
            patch.object(main, "fetch_with_home_relay", return_value=relay),
            patch.object(main, "fetch_sofascore_with_curl_cffi") as fallback,
        ):
            result = main.choose_fetcher("https://www.sofascore.com/api/v1/example", None)

        self.assertIs(result, relay)
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
