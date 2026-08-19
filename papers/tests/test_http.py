"""http 모듈: polite 헤더, Retry-After 존중 백오프, 실패 흡수."""

import contextlib
import io
import unittest
from unittest import mock

from papers import http


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, bad_json=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """호출 순서대로 응답을 돌려주는 세션. 요청 인자를 기록한다."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if not self._responses:
            raise AssertionError("예상보다 많이 호출되었습니다")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class UserAgentTest(unittest.TestCase):
    def test_includes_mailto_when_email_is_set(self):
        with mock.patch.dict("os.environ", {"OPENALEX_EMAIL": "a@b.com"}, clear=False):
            self.assertIn("mailto:a@b.com", http.user_agent())

    def test_omits_mailto_when_email_is_absent(self):
        with mock.patch.dict("os.environ", {"OPENALEX_EMAIL": ""}, clear=False):
            self.assertNotIn("mailto", http.user_agent())


class RetryDelayTest(unittest.TestCase):
    def test_prefers_retry_after_header_over_backoff(self):
        response = FakeResponse(429, headers={"Retry-After": "39"})
        # attempt 0 의 지수 백오프는 2초지만 헤더가 이긴다
        self.assertEqual(http.retry_delay(response, 0), 39.0)

    def test_caps_retry_after_at_max_sleep(self):
        response = FakeResponse(429, headers={"Retry-After": "3600"})
        self.assertEqual(http.retry_delay(response, 0), http.MAX_SLEEP)

    def test_ignores_unparsable_retry_after(self):
        response = FakeResponse(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        self.assertEqual(http.retry_delay(response, 0), 2.0)

    def test_falls_back_to_exponential_backoff(self):
        self.assertEqual(http.retry_delay(None, 0), 2.0)
        self.assertEqual(http.retry_delay(None, 1), 4.0)
        self.assertEqual(http.retry_delay(None, 2), 8.0)

    def test_caps_exponential_backoff_at_max_sleep(self):
        self.assertEqual(http.retry_delay(None, 20), http.MAX_SLEEP)


class GetJsonTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(http.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        http._last_call.clear()
        # 경고는 의도된 동작이지만 테스트 출력을 어지럽히므로 붙잡아 둔다
        self.stderr = io.StringIO()

    def _run(self, responses, **kwargs):
        session = FakeSession(responses)
        with mock.patch.object(http, "session", return_value=session):
            with contextlib.redirect_stderr(self.stderr):
                result = http.get_json("https://api.example.org/works", **kwargs)
        return result, session

    def test_returns_parsed_body_on_200(self):
        result, session = self._run([FakeResponse(200, {"ok": True})])
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.calls), 1)

    def test_returns_none_on_404_without_retrying(self):
        result, session = self._run([FakeResponse(404)])
        self.assertIsNone(result)
        self.assertEqual(len(session.calls), 1)

    def test_retries_on_429_then_succeeds(self):
        result, session = self._run([
            FakeResponse(429, headers={"Retry-After": "40"}),
            FakeResponse(200, {"ok": True}),
        ])
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(session.calls), 2)
        self.sleep.assert_any_call(40.0)

    def test_returns_none_after_exhausting_retries(self):
        result, session = self._run([FakeResponse(503) for _ in range(http.MAX_RETRIES)])
        self.assertIsNone(result)
        self.assertEqual(len(session.calls), http.MAX_RETRIES)

    def test_survives_network_exception_and_retries(self):
        import requests
        result, session = self._run([
            requests.RequestException("연결 끊김"),
            FakeResponse(200, {"ok": True}),
        ])
        self.assertEqual(result, {"ok": True})

    def test_returns_none_on_unparsable_json(self):
        result, _ = self._run([FakeResponse(200, bad_json=True)])
        self.assertIsNone(result)

    def test_does_not_retry_unexpected_status(self):
        result, session = self._run([FakeResponse(418)])
        self.assertIsNone(result)
        self.assertEqual(len(session.calls), 1)

    def test_warns_on_stderr_when_giving_up(self):
        self._run([FakeResponse(503) for _ in range(http.MAX_RETRIES)])
        self.assertIn("재시도 실패", self.stderr.getvalue())

    def test_passes_params_and_headers_through(self):
        _, session = self._run(
            [FakeResponse(200, {})],
            params={"filter": "x"},
            headers={"x-api-key": "k"},
        )
        self.assertEqual(session.calls[0]["params"], {"filter": "x"})
        self.assertEqual(session.calls[0]["headers"], {"x-api-key": "k"})


if __name__ == "__main__":
    unittest.main()
