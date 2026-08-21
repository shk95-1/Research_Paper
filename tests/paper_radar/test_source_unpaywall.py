"""paper_radar.sources.unpaywall: DOI -> 합법 OA 링크. is_oa 참/거짓 분기가 핵심.

PAYLOAD_OPEN/PAYLOAD_CLOSED 픽스처는 태스크 브리핑(task-8-brief.md)의 API
사실(2026-08 조사) 절에 실린 응답 형태를 구성한 예시다 — 실제 응답 형태
검증은 이 픽스처가 아니라 tool/live_smoke.py 가 한다.

목킹 지점: FakeSession 을 실제 Transport 에 주입해 request 루프를 그대로
통과시킨다(다른 소스 테스트와 동일한 방식).
"""

import json
import unittest
from unittest import mock

from paper_radar.registry import SOURCES
from paper_radar.sources import unpaywall
from paper_radar.transport.errors import NotFound
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

# 구성 예시(브리핑의 API 사실 절 기반) — 실응답 검증은 tool/live_smoke.py 로.
PAYLOAD_OPEN = {
    "doi": "10.1016/j.jaad.2023.01.001",
    "is_oa": True,
    "oa_status": "hybrid",
    "best_oa_location": {
        "url": "https://example.org/landing",
        "url_for_pdf": "https://example.org/article.pdf",
        "host_type": "publisher",
        "license": "cc-by",
        "version": "publishedVersion",
    },
    "journal_is_oa": False,
    "title": "Example title",
}

PAYLOAD_CLOSED = {
    "doi": "10.1016/j.jaad.2023.01.002",
    "is_oa": False,
    "oa_status": "closed",
    "best_oa_location": None,
    "journal_is_oa": False,
    "title": "Closed example",
}


def _response(payload, status=200):
    return FakeResponse(status, body=json.dumps(payload).encode())


class FetchTest(unittest.TestCase):
    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def _fetch(self, payload, doi):
        transport, session = self._transport([_response(payload)])
        result = unpaywall.fetch(transport, doi)
        return result, session

    def test_returns_none_without_a_doi(self):
        transport, session = self._transport([])
        self.assertIsNone(unpaywall.fetch(transport, doi=None))
        self.assertIsNone(unpaywall.fetch(transport, doi=""))
        self.assertEqual(session.calls, [])

    def test_parses_an_open_access_response(self):
        result, _ = self._fetch(PAYLOAD_OPEN, doi="10.1016/j.jaad.2023.01.001")
        self.assertTrue(result.is_oa)
        self.assertEqual(result.oa_status, "hybrid")
        self.assertEqual(result.pdf_url, "https://example.org/article.pdf")
        self.assertEqual(result.landing_url, "https://example.org/landing")
        self.assertEqual(result.host_type, "publisher")
        self.assertEqual(result.license, "cc-by")

    def test_parses_a_closed_response_with_every_location_field_none(self):
        result, _ = self._fetch(PAYLOAD_CLOSED, doi="10.1016/j.jaad.2023.01.002")
        self.assertFalse(result.is_oa)
        self.assertEqual(result.oa_status, "closed")
        self.assertIsNone(result.pdf_url)
        self.assertIsNone(result.landing_url)
        self.assertIsNone(result.host_type)
        self.assertIsNone(result.license)

    def test_lowercases_the_doi_on_the_record(self):
        payload = dict(PAYLOAD_OPEN, doi="10.1016/J.JAAD.2023.01.001")
        result, _ = self._fetch(payload, doi="10.1016/J.JAAD.2023.01.001")
        self.assertEqual(result.doi, "10.1016/j.jaad.2023.01.001")

    def test_puts_the_doi_in_the_request_path(self):
        _, session = self._fetch(PAYLOAD_OPEN, doi="10.1016/j.jaad.2023.01.001")
        self.assertTrue(session.calls[0]["url"].endswith("10.1016/j.jaad.2023.01.001"))

    def test_sends_the_email_param_when_configured(self):
        """email 은 Unpaywall API 의 필수 파라미터다(브리핑 API 사실 절) —
        SourcePolicy 선언만으로 Transport 가 자동 주입하는지 확인한다."""
        with mock.patch.dict("os.environ", {"OPENALEX_EMAIL": "researcher@example.com"}):
            _, session = self._fetch(PAYLOAD_OPEN, doi="10.1016/j.jaad.2023.01.001")
        self.assertEqual(session.calls[0]["params"]["email"], "researcher@example.com")

    def test_sends_no_email_param_when_unconfigured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            _, session = self._fetch(PAYLOAD_OPEN, doi="10.1016/j.jaad.2023.01.001")
        self.assertNotIn("email", session.calls[0]["params"])

    def test_propagates_not_found_instead_of_absorbing_it(self):
        """404 = '그 DOI 를 모른다'(브리핑 API 사실 절) — is_oa: false(정상 200)와는
        다른 신호이니 여기서 흡수하지 않고 그대로 전파해야 한다."""
        transport, session = self._transport([FakeResponse(404)])
        with self.assertRaises(NotFound):
            unpaywall.fetch(transport, doi="10.9999/absent")
        self.assertEqual(len(session.calls), 1)


class ToRecordTest(unittest.TestCase):
    """순수 파싱 함수 — 네트워크 없이 dict -> OaLocationRecord 를 직접 검사한다."""

    def test_open_access_record_carries_the_best_location_fields(self):
        record = unpaywall.to_record(PAYLOAD_OPEN, checked_at="2026-08-21T00:00:00Z")
        self.assertTrue(record.is_oa)
        self.assertEqual(record.pdf_url, "https://example.org/article.pdf")
        self.assertEqual(record.checked_at, "2026-08-21T00:00:00Z")

    def test_closed_record_has_no_location_fields_but_keeps_oa_status(self):
        record = unpaywall.to_record(PAYLOAD_CLOSED, checked_at="2026-08-21T00:00:00Z")
        self.assertFalse(record.is_oa)
        self.assertEqual(record.oa_status, "closed")
        self.assertIsNone(record.pdf_url)
        self.assertIsNone(record.landing_url)
        self.assertIsNone(record.host_type)
        self.assertIsNone(record.license)

    def test_ignores_best_oa_location_when_is_oa_is_false_even_if_present(self):
        """방어적 케이스: is_oa=false 인데 best_oa_location 이 비정상적으로
        채워져 있어도 무시한다 — is_oa 가 판정의 유일한 기준이다."""
        malformed = dict(
            PAYLOAD_CLOSED,
            is_oa=False,
            best_oa_location={"url": "https://should-be-ignored.example.org"},
        )
        record = unpaywall.to_record(malformed, checked_at="2026-08-21T00:00:00Z")
        self.assertIsNone(record.landing_url)

    def test_falls_back_to_closed_when_oa_status_is_missing(self):
        stripped = dict(PAYLOAD_CLOSED)
        del stripped["oa_status"]
        record = unpaywall.to_record(stripped, checked_at="2026-08-21T00:00:00Z")
        self.assertEqual(record.oa_status, "closed")


class RegistryTest(unittest.TestCase):
    def test_registers_unpaywall_under_its_key(self):
        self.assertIs(SOURCES["unpaywall"], unpaywall.Unpaywall)


if __name__ == "__main__":
    unittest.main()
