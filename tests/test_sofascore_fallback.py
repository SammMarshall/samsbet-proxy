import unittest
from unittest.mock import patch

import main


class SofaScoreFallbackTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        cooldown = main.Relay403Cooldown(300)
        cooldown.clock = lambda: self.now
        patcher = patch.object(main, "relay_403_cooldown", cooldown)
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_upstream_forbidden_tries_residential_proxy_first(self):
        relay = main.NormalizedResponse(
            403,
            '{"error": {"code": 403, "reason": "Forbidden"}}',
            lambda: {"error": {"code": 403, "reason": "Forbidden"}},
            "home_relay",
            1,
        )
        residential = main.NormalizedResponse(
            200, '{"scheduled": []}', lambda: {"scheduled": []}, "curl_cffi_proxy", 1
        )
        proxies = {"http": "http://proxy.example:1234", "https": "http://proxy.example:1234"}

        with (
            patch.object(main, "SOFASCORE_FETCHER", "home_relay"),
            patch.object(main, "fetch_with_home_relay", return_value=relay),
            patch.object(main, "fetch_sofascore_with_curl_cffi", return_value=residential) as fallback,
        ):
            result = main.choose_fetcher("https://www.sofascore.com/api/v1/example", proxies)

        self.assertIs(result, residential)
        fallback.assert_called_once_with(
            "https://www.sofascore.com/api/v1/example", proxies=proxies
        )

    def test_logs_sofascore_url_for_relay_and_proxy_attempts(self):
        url = "https://www.sofascore.com/api/v1/example?page=2"
        relay = main.NormalizedResponse(
            403,
            '{"error": {"code": 403, "reason": "Forbidden"}}',
            lambda: {"error": {"code": 403, "reason": "Forbidden"}},
            "home_relay",
            1,
        )
        proxy = main.NormalizedResponse(200, "{}", lambda: {}, "curl_cffi_proxy", 1)
        proxies = {"https": "http://proxy.example:1234"}

        with (
            patch.object(main, "SOFASCORE_FETCHER", "home_relay"),
            patch.object(main, "fetch_with_home_relay", return_value=relay),
            patch.object(main, "fetch_sofascore_with_curl_cffi", return_value=proxy),
            self.assertLogs(main.logger, level="INFO") as logs,
        ):
            main.choose_fetcher(url, proxies)

        self.assertTrue(any(f"via home_relay url={url}" in line for line in logs.output))
        self.assertTrue(any(f"route=http://proxy.example:1234 url={url}" in line for line in logs.output))

    def test_residential_proxy_uses_existing_certificate_policy(self):
        proxies = {"https": "http://proxy.example:1234"}
        with patch("curl_cffi.requests.get") as request:
            request.return_value.status_code = 200
            request.return_value.text = "{}"
            main.fetch_sofascore_with_curl_cffi(
                "https://www.sofascore.com/api/v1/example", proxies=proxies
            )

        request.assert_called_once_with(
            "https://www.sofascore.com/api/v1/example",
            impersonate="chrome",
            timeout=main.REQUEST_TIMEOUT,
            proxies=proxies,
            verify=False,
        )

    def test_forbidden_skips_relay_for_five_minutes_and_renews_on_next_403(self):
        forbidden = main.NormalizedResponse(
            403,
            '{"error": {"code": 403, "reason": "Forbidden"}}',
            lambda: {"error": {"code": 403, "reason": "Forbidden"}},
            "home_relay",
            1,
        )
        relay_ok = main.NormalizedResponse(
            200, '{"scheduled": []}', lambda: {"scheduled": []}, "home_relay", 1
        )
        proxy_ok = main.NormalizedResponse(
            200, '{"scheduled": []}', lambda: {"scheduled": []}, "curl_cffi_proxy", 1
        )
        url = "https://www.sofascore.com/api/v1/example"
        proxies = {"https": "http://proxy.example:1234"}

        with (
            patch.object(main, "SOFASCORE_FETCHER", "home_relay"),
            patch.object(main, "fetch_with_home_relay", side_effect=[forbidden, forbidden, relay_ok, relay_ok]) as relay,
            patch.object(main, "fetch_sofascore_with_curl_cffi", return_value=proxy_ok) as fallback,
        ):
            self.assertIs(main.choose_fetcher(url, proxies), proxy_ok)
            self.now = 299
            self.assertIs(main.choose_fetcher(url, proxies), proxy_ok)
            self.assertEqual(relay.call_count, 1)

            self.now = 300
            self.assertIs(main.choose_fetcher(url, proxies), proxy_ok)
            self.now = 599
            self.assertIs(main.choose_fetcher(url, proxies), proxy_ok)
            self.assertEqual(relay.call_count, 2)

            self.now = 600
            self.assertIs(main.choose_fetcher(url, proxies), relay_ok)
            self.now = 601
            self.assertIs(main.choose_fetcher(url, proxies), relay_ok)

        self.assertEqual(relay.call_count, 4)
        self.assertEqual(fallback.call_count, 4)

    def test_only_one_request_probes_relay_after_cooldown(self):
        cooldown = main.relay_403_cooldown
        forbidden = main.NormalizedResponse(
            403,
            '{"error": {"code": 403, "reason": "Forbidden"}}',
            lambda: {"error": {"code": 403, "reason": "Forbidden"}},
            "home_relay",
            1,
        )
        cooldown.after_response(forbidden, is_probe=False)
        self.now = 300

        self.assertEqual(cooldown.before_request(), (True, True))
        self.assertEqual(cooldown.before_request(), (False, False))

        cooldown.after_response(forbidden, is_probe=True)
        self.assertEqual(cooldown.before_request(), (False, False))

    def test_residential_bad_endpoint_is_retried_before_direct_challenge(self):
        url = "https://www.sofascore.com/api/v1/unique-tournament/390/season/89840/standings/total"
        proxies = {"https": "http://proxy.example:1234"}
        bad_endpoint = main.NormalizedResponse(
            402, "bad_endpoint: residential failed", lambda: {}, "curl_cffi_proxy", 1
        )
        proxy_ok = main.NormalizedResponse(
            200, '{"standings": []}', lambda: {"standings": []}, "curl_cffi_proxy", 1
        )
        proxy_responses = iter([bad_endpoint, proxy_ok])

        def fetch_browser(_url, proxies=None):
            if proxies is None:
                self.fail("A tentativa direta não deve encobrir bad_endpoint")
            return next(proxy_responses)
        self.now = 1
        main.relay_403_cooldown.open_until = 300

        with (
            patch.object(main, "SOFASCORE_FETCHER", "home_relay"),
            patch.object(main, "MAX_RETRIES", 2),
            patch.object(main, "RETRY_SLEEP", 0),
            patch.object(main, "fetch_with_home_relay") as relay,
            patch.object(
                main,
                "fetch_sofascore_with_curl_cffi",
                side_effect=fetch_browser,
            ) as browser,
        ):
            response = main.fetch_with_retry(url, proxies)

        self.assertIs(response, proxy_ok)
        relay.assert_not_called()
        self.assertEqual(browser.call_count, 2)


if __name__ == "__main__":
    unittest.main()
