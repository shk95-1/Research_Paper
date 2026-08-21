"""europepmc 소스: 생명과학 보강. DOI 우선, 없으면 제목으로 검색한다."""

import unittest
from unittest import mock

from papers.sources import europepmc

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


class FetchTest(unittest.TestCase):
    def _fetch(self, payload, **kwargs):
        with mock.patch.object(europepmc.http, "get_json", return_value=payload) as get_json:
            result = europepmc.fetch(**kwargs)
        return result, get_json

    def test_returns_none_without_a_doi_or_title(self):
        with mock.patch.object(europepmc.http, "get_json") as get_json:
            self.assertIsNone(europepmc.fetch(doi=None, title=None))
        get_json.assert_not_called()

    def test_queries_by_doi_when_one_is_available(self):
        _, get_json = self._fetch(PAYLOAD, doi="10.1/a", title="Ignored title")
        self.assertEqual(get_json.call_args.kwargs["params"]["query"], 'DOI:"10.1/a"')

    def test_falls_back_to_a_title_query_without_a_doi(self):
        _, get_json = self._fetch(PAYLOAD, doi=None, title="Retinol and the barrier")
        self.assertEqual(
            get_json.call_args.kwargs["params"]["query"],
            'TITLE:"Retinol and the barrier"',
        )

    def test_strips_double_quotes_out_of_a_title_query(self):
        # 제목 안의 따옴표가 쿼리 문법을 깨뜨린다
        _, get_json = self._fetch(PAYLOAD, doi=None, title='A "quoted" title')
        self.assertEqual(get_json.call_args.kwargs["params"]["query"], 'TITLE:"A quoted title"')

    def test_requests_the_core_result_type(self):
        # resultType=core 없이는 초록이 오지 않는다
        _, get_json = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(get_json.call_args.kwargs["params"]["resultType"], "core")

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

    def test_returns_none_when_the_request_fails(self):
        result, _ = self._fetch(None, doi="10.1/a")
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


if __name__ == "__main__":
    unittest.main()
