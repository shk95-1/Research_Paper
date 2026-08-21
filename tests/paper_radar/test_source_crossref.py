"""paper_radar.sources.crossref: DOI 검증. title 과 container-title 이 리스트라는 점이 핵심.

PAYLOAD 픽스처는 papers/tests/test_crossref.py 의 2026-08-19 실제 응답
형태를 그대로 옮긴 것이다(T5a 에서 papers/tests 로부터 이식).

목킹 지점: 기존 테스트는 source.http.get_json 을 patch 했지만, 여기서는
FakeSession 을 실제 Transport 에 주입해 request 루프를 그대로 통과시킨다.
"""

import json
import unittest

from paper_radar.registry import SOURCES
from paper_radar.sources import crossref
from paper_radar.transport.errors import NotFound
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

# 2026-08-19 실제 응답 형태
PAYLOAD = {
    "message": {
        "title": ["Integrating habits and practices data for soaps and cosmetics"],
        "container-title": ["Regulatory Toxicology and Pharmacology"],
        "publisher": "Elsevier BV",
        "type": "journal-article",
        "issued": {"date-parts": [[2017, 8]]},
    }
}

RECORD_KEYS = {"title", "journal", "publisher", "type", "year"}


class FetchTest(unittest.TestCase):
    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def _fetch(self, payload, **kwargs):
        transport, session = self._transport([FakeResponse(200, body=json.dumps(payload).encode())])
        result = crossref.fetch(transport, **kwargs)
        return result, session

    def test_returns_none_without_a_doi(self):
        transport, session = self._transport([])
        self.assertIsNone(crossref.fetch(transport, doi=None, title="Some title"))
        self.assertEqual(session.calls, [])

    def test_unwraps_the_title_list(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1016/j.yrtph.2017.05.017")
        self.assertEqual(
            result["title"],
            "Integrating habits and practices data for soaps and cosmetics",
        )

    def test_unwraps_the_container_title_list_as_the_journal(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["journal"], "Regulatory Toxicology and Pharmacology")

    def test_reads_publisher_and_type(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["publisher"], "Elsevier BV")
        self.assertEqual(result["type"], "journal-article")

    def test_reads_the_year_out_of_nested_date_parts(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["year"], 2017)

    def test_puts_the_doi_in_the_request_path(self):
        _, session = self._fetch(PAYLOAD, doi="10.1016/j.yrtph.2017.05.017")
        self.assertTrue(session.calls[0]["url"].endswith("10.1016/j.yrtph.2017.05.017"))

    def test_propagates_not_found_instead_of_absorbing_it(self):
        """구 papers/http.py 는 404 를 None 으로 흡수했지만, 새 계약에서는 transport
        의 NotFound 를 여기서 잡지 않고 그대로 전파해야 한다 — 그 판단은 파이프라인 몫."""
        transport, session = self._transport([FakeResponse(404)])
        with self.assertRaises(NotFound):
            crossref.fetch(transport, doi="10.9999/absent")
        self.assertEqual(len(session.calls), 1)

    def test_returns_none_when_the_message_envelope_is_missing(self):
        result, _ = self._fetch({"status": "ok"}, doi="10.1/a")
        self.assertIsNone(result)

    def test_returns_none_when_the_message_is_not_an_object(self):
        result, _ = self._fetch({"message": "unexpected"}, doi="10.1/a")
        self.assertIsNone(result)

    def test_survives_a_message_stripped_of_every_optional_field(self):
        result, _ = self._fetch({"message": {}}, doi="10.1/a")
        self.assertIsNone(result["title"])
        self.assertIsNone(result["journal"])
        self.assertIsNone(result["year"])

    def test_survives_an_empty_title_list(self):
        result, _ = self._fetch({"message": {"title": []}}, doi="10.1/a")
        self.assertIsNone(result["title"])

    def test_accepts_a_bare_string_title(self):
        result, _ = self._fetch({"message": {"title": "Plain"}}, doi="10.1/a")
        self.assertEqual(result["title"], "Plain")

    def test_survives_malformed_date_parts(self):
        result, _ = self._fetch({"message": {"issued": {"date-parts": [[]]}}}, doi="10.1/a")
        self.assertIsNone(result["year"])

    def test_record_has_the_same_key_set_as_the_legacy_module(self):
        """T5b 의 동등성 전제 — 반환 dict 키가 papers/sources/crossref.py 와 같아야 한다."""
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(set(result), RECORD_KEYS)


class RegistryTest(unittest.TestCase):
    def test_registers_crossref_under_its_key(self):
        self.assertIs(SOURCES["crossref"], crossref.Crossref)


if __name__ == "__main__":
    unittest.main()
