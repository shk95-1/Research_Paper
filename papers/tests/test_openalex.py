"""openalex 소스: 역색인 초록 복원, DOI 정규화, 레코드 변환, 커서 페이지네이션.

WORK 픽스처는 2026-08-19 실제 응답에서 필드 경로를 그대로 옮긴 것이다.
"""

import unittest
from unittest import mock

from papers.sources import openalex

WORK = {
    "id": "https://openalex.org/W2618188783",
    "doi": "https://doi.org/10.1016/j.yrtph.2017.05.017",
    "title": "Integrating habits and practices data for soaps and cosmetics",
    "publication_year": 2017,
    "is_retracted": False,
    "cited_by_count": 1805,
    "type": "article",
    "language": "en",
    "abstract_inverted_index": {
        "Fragrance": [0],
        "is": [1, 4],
        "an": [2],
        "ingredient": [3],
        "everywhere": [5],
    },
    "open_access": {"is_oa": False, "oa_status": "closed", "oa_url": None},
    "primary_location": {
        "source": {
            "display_name": "Regulatory Toxicology and Pharmacology",
            "host_organization_name": "Elsevier BV",
        }
    },
    "topics": [
        {"display_name": "Contact Dermatitis and Allergies", "score": 0.99},
        {"display_name": "Toxicology", "score": 0.8},
    ],
    "keywords": [
        {"display_name": "Product (mathematics)", "score": 0.61},
        {"display_name": "Medicine", "score": 0.59},
    ],
    "authorships": [
        {"author": {"display_name": "D. Comiskey", "orcid": None}},
        {"author": {"display_name": "A. M. Api"}},
    ],
}


class RestoreAbstractTest(unittest.TestCase):
    def test_orders_words_by_position(self):
        self.assertEqual(
            openalex.restore_abstract(WORK["abstract_inverted_index"]),
            "Fragrance is an ingredient is everywhere",
        )

    def test_places_a_repeated_word_at_every_position(self):
        restored = openalex.restore_abstract({"a": [0, 2], "b": [1]})
        self.assertEqual(restored, "a b a")

    def test_joins_what_exists_when_positions_have_gaps(self):
        restored = openalex.restore_abstract({"first": [0], "third": [7]})
        self.assertEqual(restored, "first third")

    def test_returns_none_for_a_missing_index(self):
        self.assertIsNone(openalex.restore_abstract(None))

    def test_returns_none_for_an_empty_index(self):
        self.assertIsNone(openalex.restore_abstract({}))

    def test_returns_none_for_a_non_dict(self):
        self.assertIsNone(openalex.restore_abstract("not an index"))

    def test_ignores_entries_whose_positions_are_not_a_list(self):
        self.assertEqual(openalex.restore_abstract({"a": [0], "b": "nope"}), "a")

    def test_ignores_non_integer_positions(self):
        self.assertEqual(openalex.restore_abstract({"a": [0], "b": ["x"]}), "a")


class BareDoiTest(unittest.TestCase):
    def test_strips_the_https_doi_org_prefix(self):
        self.assertEqual(
            openalex.bare_doi("https://doi.org/10.1016/j.yrtph.2017.05.017"),
            "10.1016/j.yrtph.2017.05.017",
        )

    def test_strips_the_http_prefix(self):
        self.assertEqual(openalex.bare_doi("http://doi.org/10.1/a"), "10.1/a")

    def test_strips_a_doi_scheme_prefix(self):
        self.assertEqual(openalex.bare_doi("doi:10.1/a"), "10.1/a")

    def test_lowercases_and_strips_whitespace(self):
        self.assertEqual(openalex.bare_doi("  10.1/ABC "), "10.1/abc")

    def test_leaves_an_already_bare_doi_alone(self):
        self.assertEqual(openalex.bare_doi("10.1/a"), "10.1/a")

    def test_returns_none_for_absent_or_empty_input(self):
        self.assertIsNone(openalex.bare_doi(None))
        self.assertIsNone(openalex.bare_doi(""))
        self.assertIsNone(openalex.bare_doi("https://doi.org/"))


class ToRecordTest(unittest.TestCase):
    def setUp(self):
        self.record = openalex.to_record(WORK)

    def test_normalizes_the_doi_to_a_bare_form(self):
        self.assertEqual(self.record["doi"], "10.1016/j.yrtph.2017.05.017")

    def test_keeps_the_openalex_id(self):
        self.assertEqual(self.record["openalex_id"], "https://openalex.org/W2618188783")

    def test_reads_the_journal_from_primary_location_source(self):
        self.assertEqual(self.record["journal"], "Regulatory Toxicology and Pharmacology")

    def test_flattens_authors_to_display_names(self):
        self.assertEqual(self.record["authors"], ["D. Comiskey", "A. M. Api"])

    def test_flattens_topics_and_keywords_to_display_names(self):
        self.assertEqual(
            self.record["topics"], ["Contact Dermatitis and Allergies", "Toxicology"]
        )
        self.assertEqual(self.record["keywords"], ["Product (mathematics)", "Medicine"])

    def test_reads_open_access_from_the_nested_flag(self):
        self.assertFalse(self.record["is_open_access"])

    def test_restores_the_abstract(self):
        self.assertEqual(self.record["abstract"], "Fragrance is an ingredient is everywhere")

    def test_leaves_tldr_empty_for_semantic_scholar_to_fill(self):
        self.assertIsNone(self.record["tldr"])

    def test_uses_the_doi_url_as_the_canonical_url(self):
        self.assertEqual(self.record["url"], "https://doi.org/10.1016/j.yrtph.2017.05.017")

    def test_falls_back_to_the_openalex_url_when_there_is_no_doi(self):
        record = openalex.to_record(dict(WORK, doi=None))
        self.assertEqual(record["url"], "https://openalex.org/W2618188783")

    def test_carries_citation_count_and_retraction_flag(self):
        self.assertEqual(self.record["citation_count"], 1805)
        self.assertFalse(self.record["is_retracted"])

    def test_survives_a_response_stripped_of_every_optional_field(self):
        record = openalex.to_record({"id": "https://openalex.org/W9"})
        self.assertIsNone(record["doi"])
        self.assertIsNone(record["journal"])
        self.assertIsNone(record["abstract"])
        self.assertEqual(record["authors"], [])
        self.assertEqual(record["topics"], [])
        self.assertFalse(record["is_open_access"])


class SearchTest(unittest.TestCase):
    def test_builds_a_title_and_abstract_filter_with_the_year_range(self):
        with mock.patch.object(openalex.http, "get_json", return_value={"results": [WORK]}) as get_json:
            openalex.search("cosmetic retinol", 2016, 2026, limit=1)
        params = get_json.call_args.kwargs["params"]
        self.assertIn("title_and_abstract.search:cosmetic retinol", params["filter"])
        self.assertIn("from_publication_date:2016-01-01", params["filter"])
        self.assertIn("to_publication_date:2026-12-31", params["filter"])

    def test_requests_only_the_fields_it_uses(self):
        with mock.patch.object(openalex.http, "get_json", return_value={"results": [WORK]}) as get_json:
            openalex.search("cosmetic", 2016, 2026, limit=1)
        self.assertIn("abstract_inverted_index", get_json.call_args.kwargs["params"]["select"])

    def test_includes_mailto_when_the_contact_email_is_configured(self):
        with mock.patch.object(openalex.http, "contact_email", return_value="a@b.com"):
            with mock.patch.object(openalex.http, "get_json", return_value={"results": [WORK]}) as get_json:
                openalex.search("cosmetic", 2016, 2026, limit=1)
        self.assertEqual(get_json.call_args.kwargs["params"]["mailto"], "a@b.com")

    def test_omits_mailto_when_no_email_is_configured(self):
        with mock.patch.object(openalex.http, "contact_email", return_value=""):
            with mock.patch.object(openalex.http, "get_json", return_value={"results": [WORK]}) as get_json:
                openalex.search("cosmetic", 2016, 2026, limit=1)
        self.assertNotIn("mailto", get_json.call_args.kwargs["params"])

    def test_sends_the_api_key_only_when_one_is_set(self):
        with mock.patch.dict("os.environ", {"OPENALEX_API_KEY": "secret"}, clear=False):
            with mock.patch.object(openalex.http, "get_json", return_value={"results": [WORK]}) as get_json:
                openalex.search("cosmetic", 2016, 2026, limit=1)
        self.assertEqual(get_json.call_args.kwargs["params"]["api_key"], "secret")

    def test_follows_the_cursor_until_the_limit_is_reached(self):
        pages = [
            {"results": [WORK, WORK], "meta": {"next_cursor": "c2"}},
            {"results": [WORK], "meta": {"next_cursor": None}},
        ]
        with mock.patch.object(openalex.http, "get_json", side_effect=pages) as get_json:
            records = openalex.search("cosmetic", 2016, 2026, limit=3)
        self.assertEqual(len(records), 3)
        self.assertEqual(get_json.call_args_list[1].kwargs["params"]["cursor"], "c2")

    def test_never_returns_more_than_the_limit(self):
        page = {"results": [WORK] * 50, "meta": {"next_cursor": "c2"}}
        with mock.patch.object(openalex.http, "get_json", return_value=page):
            self.assertEqual(len(openalex.search("cosmetic", 2016, 2026, limit=2)), 2)

    def test_stops_when_a_page_comes_back_empty(self):
        pages = [{"results": [], "meta": {"next_cursor": "c2"}}]
        with mock.patch.object(openalex.http, "get_json", side_effect=pages) as get_json:
            self.assertEqual(openalex.search("cosmetic", 2016, 2026, limit=10), [])
        self.assertEqual(get_json.call_count, 1)

    def test_returns_what_it_has_when_a_request_fails(self):
        pages = [{"results": [WORK], "meta": {"next_cursor": "c2"}}, None]
        with mock.patch.object(openalex.http, "get_json", side_effect=pages):
            self.assertEqual(len(openalex.search("cosmetic", 2016, 2026, limit=10)), 1)


class TrendTest(unittest.TestCase):
    def test_groups_by_publication_year_and_sorts_ascending(self):
        payload = {"group_by": [
            {"key": "2025", "count": 18737},
            {"key": "2016", "count": 9000},
        ]}
        with mock.patch.object(openalex.http, "get_json", return_value=payload) as get_json:
            result = openalex.trend("cosmetic", 2016, 2026)
        self.assertEqual(result, [(2016, 9000), (2025, 18737)])
        self.assertEqual(get_json.call_args.kwargs["params"]["group_by"], "publication_year")

    def test_asks_for_enough_groups_to_cover_every_year(self):
        # per-page 가 그룹 수를 자른다. 실측에서 per-page=1 이면 1개 연도만 왔다.
        with mock.patch.object(openalex.http, "get_json", return_value={"group_by": []}) as get_json:
            openalex.trend("cosmetic", 2016, 2026)
        self.assertGreaterEqual(get_json.call_args.kwargs["params"]["per-page"], 200)

    def test_skips_groups_whose_key_is_not_a_year(self):
        payload = {"group_by": [{"key": "unknown", "count": 5}, {"key": "2020", "count": 1}]}
        with mock.patch.object(openalex.http, "get_json", return_value=payload):
            self.assertEqual(openalex.trend("cosmetic", 2016, 2026), [(2020, 1)])

    def test_returns_an_empty_list_when_the_request_fails(self):
        with mock.patch.object(openalex.http, "get_json", return_value=None):
            self.assertEqual(openalex.trend("cosmetic", 2016, 2026), [])


if __name__ == "__main__":
    unittest.main()
