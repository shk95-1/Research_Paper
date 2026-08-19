"""crossref 소스: DOI 검증. title 과 container-title 이 리스트라는 점이 핵심."""

import unittest
from unittest import mock

from papers.sources import crossref

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


class FetchTest(unittest.TestCase):
    def _fetch(self, payload, **kwargs):
        with mock.patch.object(crossref.http, "get_json", return_value=payload) as get_json:
            result = crossref.fetch(**kwargs)
        return result, get_json

    def test_returns_none_without_a_doi(self):
        with mock.patch.object(crossref.http, "get_json") as get_json:
            self.assertIsNone(crossref.fetch(doi=None, title="Some title"))
        get_json.assert_not_called()

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
        _, get_json = self._fetch(PAYLOAD, doi="10.1016/j.yrtph.2017.05.017")
        self.assertTrue(
            get_json.call_args.args[0].endswith("10.1016/j.yrtph.2017.05.017")
        )

    def test_returns_none_when_the_doi_is_unknown(self):
        # http.get_json 은 404 를 None 으로 흡수한다. 그것이 미검증의 근거다.
        result, _ = self._fetch(None, doi="10.9999/absent")
        self.assertIsNone(result)

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


if __name__ == "__main__":
    unittest.main()
