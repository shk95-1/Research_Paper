"""semantic_scholar 소스: tldr 요약. tldr 이 객체라는 점이 핵심."""

import unittest
from unittest import mock

from papers.sources import semantic_scholar as s2

# 2026-08-19 실제 응답 형태. abstract 가 null 인데 tldr 은 있는 실제 사례다.
PAYLOAD = {
    "paperId": "abc123",
    "title": "Integrating habits and practices data for soaps and cosmetics",
    "abstract": None,
    "tldr": {"model": "tldr@v2.0.0", "text": "The Phase 2 Creme RIFM model is described."},
    "citationCount": 1341,
    "year": 2017,
    "venue": "Regulatory toxicology and pharmacology : RTP",
    "isOpenAccess": False,
}


class FetchTest(unittest.TestCase):
    def _fetch(self, payload, **kwargs):
        with mock.patch.object(s2.http, "get_json", return_value=payload) as get_json:
            result = s2.fetch(**kwargs)
        return result, get_json

    def test_returns_none_without_a_doi(self):
        with mock.patch.object(s2.http, "get_json") as get_json:
            self.assertIsNone(s2.fetch(doi=None, title="Some title"))
        get_json.assert_not_called()

    def test_unwraps_the_tldr_text_out_of_its_object(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["tldr"], "The Phase 2 Creme RIFM model is described.")

    def test_reports_a_null_abstract_even_when_a_tldr_exists(self):
        # 초록과 tldr 은 서로 독립이다. 하나가 있어도 다른 하나가 없을 수 있다.
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertIsNone(result["abstract"])

    def test_maps_venue_to_journal(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["journal"], "Regulatory toxicology and pharmacology : RTP")

    def test_carries_the_citation_count(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["citation_count"], 1341)

    def test_prefixes_the_doi_for_the_lookup_path(self):
        _, get_json = self._fetch(PAYLOAD, doi="10.1016/j.yrtph.2017.05.017")
        self.assertIn("DOI:10.1016/j.yrtph.2017.05.017", get_json.call_args.args[0])

    def test_sends_the_api_key_header_when_one_is_set(self):
        with mock.patch.dict("os.environ", {"SEMANTIC_SCHOLAR_API_KEY": "secret"}, clear=False):
            _, get_json = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(get_json.call_args.kwargs["headers"]["x-api-key"], "secret")

    def test_sends_no_api_key_header_when_none_is_set(self):
        with mock.patch.dict("os.environ", {"SEMANTIC_SCHOLAR_API_KEY": ""}, clear=False):
            _, get_json = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(get_json.call_args.kwargs["headers"], {})

    def test_returns_none_when_the_paper_is_unknown(self):
        result, _ = self._fetch(None, doi="10.9999/absent")
        self.assertIsNone(result)

    def test_returns_none_for_an_empty_payload(self):
        result, _ = self._fetch({}, doi="10.1/a")
        self.assertIsNone(result)

    def test_survives_a_tldr_that_is_a_plain_string(self):
        result, _ = self._fetch(dict(PAYLOAD, tldr="already a string"), doi="10.1/a")
        self.assertEqual(result["tldr"], "already a string")

    def test_survives_a_missing_tldr(self):
        result, _ = self._fetch(dict(PAYLOAD, tldr=None), doi="10.1/a")
        self.assertIsNone(result["tldr"])

    def test_treats_an_empty_venue_as_absent(self):
        result, _ = self._fetch(dict(PAYLOAD, venue=""), doi="10.1/a")
        self.assertIsNone(result["journal"])


if __name__ == "__main__":
    unittest.main()
