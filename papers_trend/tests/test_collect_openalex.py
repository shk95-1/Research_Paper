"""collect_openalex 의 새 로직만 덮는다: 예산 헤더 파싱, 중단 사유 결정, get_page 의
예산 소진 처리. 이 파일 도입 전에는 collect_openalex.py 에 테스트가 전혀 없었으므로
전체 커버리지는 이후 태스크의 몫이고 여기서는 2026 API 약관 대응만 검증한다.

    python -m unittest discover -s papers_trend/tests -t .
"""

import contextlib
import io
import unittest
from unittest import mock

from papers_trend import collect_openalex as co


class ParseBudgetRemainingTest(unittest.TestCase):
    def test_reads_the_ratelimit_remaining_header_as_an_int(self):
        self.assertEqual(co.parse_budget_remaining({"x-ratelimit-remaining": "97600"}), 97600)

    def test_returns_none_when_the_header_is_absent(self):
        self.assertIsNone(co.parse_budget_remaining({}))

    def test_returns_none_for_no_headers_at_all(self):
        self.assertIsNone(co.parse_budget_remaining(None))

    def test_returns_none_for_an_unparsable_value(self):
        self.assertIsNone(co.parse_budget_remaining({"x-ratelimit-remaining": "not-a-number"}))

    def test_reads_zero_correctly_rather_than_treating_it_as_falsy(self):
        self.assertEqual(co.parse_budget_remaining({"x-ratelimit-remaining": "0"}), 0)


class DetermineStoppedReasonTest(unittest.TestCase):
    def test_is_none_on_normal_completion(self):
        self.assertIsNone(co.determine_stopped_reason(budget_exhausted=False, hit_max_pages=False))

    def test_is_budget_exhausted_when_the_budget_ran_out(self):
        self.assertEqual(
            co.determine_stopped_reason(budget_exhausted=True, hit_max_pages=False),
            "budget_exhausted",
        )

    def test_is_max_pages_when_the_page_cap_was_hit(self):
        self.assertEqual(
            co.determine_stopped_reason(budget_exhausted=False, hit_max_pages=True),
            "max_pages",
        )

    def test_budget_exhausted_wins_when_both_happened(self):
        # 페이지 상한에 닿은 바로 그 페이지가 마침 예산도 소진시켰다면,
        # 다음 실행이 마주할 진짜 문제는 예산이다.
        self.assertEqual(
            co.determine_stopped_reason(budget_exhausted=True, hit_max_pages=True),
            "budget_exhausted",
        )


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, bad_json=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._bad_json = bad_json
        self.text = ""

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        if not self._responses:
            raise AssertionError("예상보다 많이 호출되었습니다")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class GetPageBudgetTest(unittest.TestCase):
    """get_page 가 요청 파라미터/예산 정보를 어떻게 다루는지만 검증한다."""

    def setUp(self):
        patcher = mock.patch.object(co.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        self.stderr = io.StringIO()

    def _get_page(self, session, params, api_key):
        with contextlib.redirect_stderr(self.stderr):
            return co.get_page(session, params, api_key)

    def test_returns_none_payload_and_marks_exhausted_on_402_without_retrying(self):
        session = FakeSession([FakeResponse(402)])
        payload, budget = self._get_page(session, {}, api_key=None)
        self.assertIsNone(payload)
        self.assertTrue(budget["exhausted"])
        self.assertEqual(len(session.calls), 1)  # 재시도하지 않는다

    def test_returns_none_payload_and_marks_exhausted_on_409_without_retrying(self):
        session = FakeSession([FakeResponse(409)])
        payload, budget = self._get_page(session, {}, api_key=None)
        self.assertIsNone(payload)
        self.assertTrue(budget["exhausted"])
        self.assertEqual(len(session.calls), 1)

    def test_marks_exhausted_when_a_successful_response_shows_zero_remaining(self):
        session = FakeSession(
            [
                FakeResponse(200, {"results": []}, headers={"x-ratelimit-remaining": "0"}),
            ]
        )
        payload, budget = co.get_page(session, {}, api_key=None)
        self.assertEqual(payload, {"results": []})
        self.assertTrue(budget["exhausted"])
        self.assertEqual(budget["remaining"], 0)

    def test_does_not_mark_exhausted_on_a_normal_low_but_nonzero_remaining(self):
        session = FakeSession(
            [
                FakeResponse(200, {"results": []}, headers={"x-ratelimit-remaining": "50"}),
            ]
        )
        _, budget = co.get_page(session, {}, api_key=None)
        self.assertFalse(budget["exhausted"])
        self.assertEqual(budget["remaining"], 50)

    def test_sends_the_api_key_only_when_one_is_set(self):
        session = FakeSession([FakeResponse(200, {"results": []})])
        co.get_page(session, {"per-page": 1}, api_key="secret")
        self.assertEqual(session.calls[0]["params"]["api_key"], "secret")

    def test_never_sends_a_mailto_request_parameter(self):
        # 2026-02-13부터 OpenAlex 가 mailto/polite pool 을 폐지했다.
        session = FakeSession([FakeResponse(200, {"results": []})])
        co.get_page(session, {"per-page": 1}, api_key=None)
        self.assertNotIn("mailto", session.calls[0]["params"])

    def test_still_retries_on_429_with_backoff(self):
        session = FakeSession(
            [
                FakeResponse(429, headers={"Retry-After": "5"}),
                FakeResponse(200, {"results": []}),
            ]
        )
        payload, budget = self._get_page(session, {}, api_key=None)
        self.assertEqual(payload, {"results": []})
        self.assertFalse(budget["exhausted"])
        self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
