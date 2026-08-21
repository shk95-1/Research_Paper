"""paper_radar.sources.europepmc: 생명과학 보강. DOI 우선, 없으면 제목으로 검색한다.

PAYLOAD 픽스처는 papers/tests/test_europepmc.py 의 2026-08-19 실제 응답
형태를 그대로 옮긴 것이다(T5a 에서 papers/tests 로부터 이식).

목킹 지점: 기존 테스트는 source.http.get_json 을 patch 했지만, 여기서는
FakeSession 을 실제 Transport 에 주입해 request 루프를 그대로 통과시킨다.
"""

import json
import unittest

from paper_radar.registry import SOURCES
from paper_radar.sources import europepmc
from paper_radar.transport.errors import NotFound
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

# 2026-08-19 실제 응답 형태
PAYLOAD = {
    "hitCount": 1,
    "resultList": {
        "result": [
            {
                "id": "28559157",
                "source": "MED",
                "title": "Integrating habits and practices data for soaps and cosmetics",
                "abstractText": "Aggregate exposure to fragrance ingredients was modelled.",
                "journalInfo": {
                    "journal": {"title": "Regulatory toxicology and pharmacology : RTP"}
                },
                "keywordList": {
                    "keyword": ["Database", "Cosmetics", "Personal Care", "Fragrance Ingredients"]
                },
            }
        ]
    },
}

RECORD_KEYS = {"title", "abstract", "journal", "keywords", "europepmc_id", "is_life_science"}


class FetchTest(unittest.TestCase):
    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def _fetch(self, payload, **kwargs):
        transport, session = self._transport([FakeResponse(200, body=json.dumps(payload).encode())])
        result = europepmc.fetch(transport, **kwargs)
        return result, session

    def test_returns_none_without_a_doi_or_title(self):
        transport, session = self._transport([])
        self.assertIsNone(europepmc.fetch(transport, doi=None, title=None))
        self.assertEqual(session.calls, [])

    def test_queries_by_doi_when_one_is_available(self):
        _, session = self._fetch(PAYLOAD, doi="10.1/a", title="Ignored title")
        self.assertEqual(session.calls[0]["params"]["query"], 'DOI:"10.1/a"')

    def test_falls_back_to_a_title_query_without_a_doi(self):
        _, session = self._fetch(PAYLOAD, doi=None, title="Retinol and the barrier")
        self.assertEqual(
            session.calls[0]["params"]["query"],
            'TITLE:"Retinol and the barrier"',
        )

    def test_strips_double_quotes_out_of_a_title_query(self):
        # 제목 안의 따옴표가 쿼리 문법을 깨뜨린다
        _, session = self._fetch(PAYLOAD, doi=None, title='A "quoted" title')
        self.assertEqual(session.calls[0]["params"]["query"], 'TITLE:"A quoted title"')

    def test_requests_the_core_result_type(self):
        # resultType=core 없이는 초록이 오지 않는다
        _, session = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(session.calls[0]["params"]["resultType"], "core")

    def test_reads_the_abstract(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(
            result["abstract"], "Aggregate exposure to fragrance ingredients was modelled."
        )

    def test_reads_the_journal_from_nested_journal_info(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["journal"], "Regulatory toxicology and pharmacology : RTP")

    def test_unwraps_the_keyword_list(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(
            result["keywords"],
            ["Database", "Cosmetics", "Personal Care", "Fragrance Ingredients"],
        )

    def test_carries_the_europepmc_id(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["europepmc_id"], "28559157")

    def test_a_hit_means_the_paper_is_indexed_as_life_science(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertTrue(result["is_life_science"])

    def test_returns_none_when_there_are_no_hits(self):
        result, _ = self._fetch({"hitCount": 0, "resultList": {"result": []}}, doi="10.1/a")
        self.assertIsNone(result)

    def test_returns_none_when_the_envelope_is_missing(self):
        result, _ = self._fetch({"hitCount": 1}, doi="10.1/a")
        self.assertIsNone(result)

    def test_survives_a_hit_stripped_of_every_optional_field(self):
        payload = {"resultList": {"result": [{"id": "1"}]}}
        result, _ = self._fetch(payload, doi="10.1/a")
        self.assertIsNone(result["abstract"])
        self.assertIsNone(result["journal"])
        self.assertEqual(result["keywords"], [])

    def test_drops_non_string_keywords(self):
        payload = {"resultList": {"result": [{"keywordList": {"keyword": ["ok", 5, None]}}]}}
        result, _ = self._fetch(payload, doi="10.1/a")
        self.assertEqual(result["keywords"], ["ok"])

    def test_propagates_not_found_instead_of_absorbing_it(self):
        """구 papers/http.py 는 실패를 전부 None 으로 흡수했지만, 새 계약에서는
        transport 의 NotFound 를 여기서 잡지 않고 그대로 전파해야 한다."""
        transport, session = self._transport([FakeResponse(404)])
        with self.assertRaises(NotFound):
            europepmc.fetch(transport, doi="10.1/a")
        self.assertEqual(len(session.calls), 1)

    def test_record_has_the_same_key_set_as_the_legacy_module(self):
        """T5b 의 동등성 전제 — 반환 dict 키가 papers/sources/europepmc.py 와 같아야 한다."""
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(set(result), RECORD_KEYS)


class RegistryTest(unittest.TestCase):
    def test_registers_europepmc_under_its_key(self):
        self.assertIs(SOURCES["europepmc"], europepmc.EuropePmc)


if __name__ == "__main__":
    unittest.main()
