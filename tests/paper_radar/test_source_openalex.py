"""paper_radar.sources.openalex: 역색인 초록 복원, DOI 정규화, 레코드 변환, 커서 페이지네이션.

WORK 픽스처는 papers/tests/test_openalex.py 의 2026-08-19 실제 응답에서 필드
경로를 그대로 옮긴 것이다(T5a 에서 papers/tests 로부터 이식).

목킹 지점: 기존 테스트는 source.http.get_json 을 patch 했지만, 여기서는
FakeSession 을 실제 Transport 에 주입해 request 루프를 그대로 통과시킨다
(tests/paper_radar/test_transport.py 의 FakeSession/FakeResponse 재사용).
요청 파라미터 검증은 FakeSession 이 기록한 호출에서 확인한다.
"""

import json
import unittest

from paper_radar.registry import SOURCES
from paper_radar.sources import openalex
from paper_radar.transport.errors import BudgetExhausted, TransientError
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

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

RECORD_KEYS = {
    "doi",
    "openalex_id",
    "title",
    "authors",
    "year",
    "journal",
    "abstract",
    "tldr",
    "keywords",
    "topics",
    "citation_count",
    "is_open_access",
    "url",
    "is_retracted",
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
        self.assertEqual(self.record["topics"], ["Contact Dermatitis and Allergies", "Toxicology"])
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

    def test_record_has_the_same_key_set_as_the_legacy_module(self):
        """T5b 의 동등성 전제 — 레코드 dict 키 14개가 papers/sources/openalex.py 와 같아야 한다."""
        self.assertEqual(set(self.record), RECORD_KEYS)
        self.assertEqual(len(RECORD_KEYS), 14)


def _json_response(payload, status=200):
    return FakeResponse(status, body=json.dumps(payload).encode())


class DriverTest(unittest.TestCase):
    """search()/trend() — Transport 를 받는 얇은 드라이버. FakeSession 을 통해
    실제 request 루프(페이스/재시도/인증)를 통과시킨다."""

    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def test_builds_a_title_and_abstract_filter_with_the_year_range(self):
        transport, session = self._transport([_json_response({"results": [WORK]})])
        openalex.search(transport, "cosmetic retinol", 2016, 2026, limit=1)
        params = session.calls[0]["params"]
        self.assertIn("title_and_abstract.search:cosmetic retinol", params["filter"])
        self.assertIn("from_publication_date:2016-01-01", params["filter"])
        self.assertIn("to_publication_date:2026-12-31", params["filter"])

    def test_requests_only_the_fields_it_uses(self):
        transport, session = self._transport([_json_response({"results": [WORK]})])
        openalex.search(transport, "cosmetic", 2016, 2026, limit=1)
        self.assertIn("abstract_inverted_index", session.calls[0]["params"]["select"])

    def test_follows_the_cursor_until_the_limit_is_reached(self):
        pages = [
            _json_response({"results": [WORK, WORK], "meta": {"next_cursor": "c2"}}),
            _json_response({"results": [WORK], "meta": {"next_cursor": None}}),
        ]
        transport, session = self._transport(pages)
        records = openalex.search(transport, "cosmetic", 2016, 2026, limit=3)
        self.assertEqual(len(records), 3)
        self.assertEqual(session.calls[1]["params"]["cursor"], "c2")

    def test_never_returns_more_than_the_limit(self):
        page = _json_response({"results": [WORK] * 50, "meta": {"next_cursor": "c2"}})
        transport, _ = self._transport([page])
        self.assertEqual(len(openalex.search(transport, "cosmetic", 2016, 2026, limit=2)), 2)

    def test_stops_when_a_page_comes_back_empty(self):
        transport, session = self._transport(
            [_json_response({"results": [], "meta": {"next_cursor": "c2"}})]
        )
        self.assertEqual(openalex.search(transport, "cosmetic", 2016, 2026, limit=10), [])
        self.assertEqual(len(session.calls), 1)

    def test_propagates_a_transient_error_instead_of_absorbing_it(self):
        """구 papers/sources/openalex.py 는 실패 시 그때까지 모은 결과만 돌려줬지만,
        새 계약에서는 그 판단(무엇을 실패로 볼지)이 파이프라인 몫이라 소스는 흡수하지
        않고 그대로 전파해야 한다. search() 는 iter_search() 의 list() 래퍼로
        바뀐 뒤에도 이 동작(부분 결과를 반환하지 않고 예외만 던짐)이 그대로다 —
        부분 결과가 필요하면 호출자가 iter_search() 를 직접 써야 한다."""
        policy_attempts = openalex.OpenAlex.policy.max_attempts
        transport, session = self._transport([FakeResponse(503) for _ in range(policy_attempts)])
        with self.assertRaises(TransientError):
            openalex.search(transport, "cosmetic", 2016, 2026, limit=10)
        self.assertEqual(len(session.calls), policy_attempts)

    def test_groups_by_publication_year_and_sorts_ascending(self):
        payload = {
            "group_by": [
                {"key": "2025", "count": 18737},
                {"key": "2016", "count": 9000},
            ]
        }
        transport, session = self._transport([_json_response(payload)])
        result = openalex.trend(transport, "cosmetic", 2016, 2026)
        self.assertEqual(result, [(2016, 9000), (2025, 18737)])
        self.assertEqual(session.calls[0]["params"]["group_by"], "publication_year")

    def test_asks_for_enough_groups_to_cover_every_year(self):
        # per-page 가 그룹 수를 자른다. 실측에서 per-page=1 이면 1개 연도만 왔다.
        transport, session = self._transport([_json_response({"group_by": []})])
        openalex.trend(transport, "cosmetic", 2016, 2026)
        self.assertGreaterEqual(int(session.calls[0]["params"]["per-page"]), 200)

    def test_skips_groups_whose_key_is_not_a_year(self):
        payload = {"group_by": [{"key": "unknown", "count": 5}, {"key": "2020", "count": 1}]}
        transport, _ = self._transport([_json_response(payload)])
        self.assertEqual(openalex.trend(transport, "cosmetic", 2016, 2026), [(2020, 1)])


class IterSearchTest(unittest.TestCase):
    """iter_search() — 페이지를 받는 즉시 그 페이지의 레코드를 yield 한다.
    search()가 list()로 통째로 모으는 것과 달리, 중간 실패가 나도 이미 넘어온
    레코드는 호출자 손에 남는다는 것이 이 리뷰 대응의 핵심이다."""

    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def test_yields_records_page_by_page_without_prefetching_further_pages(self):
        """한 페이지 안의 레코드를 다 꺼내기 전까지는 다음 페이지를 요청하지
        않아야 한다 — yield 가 실제로 페이지 단위로 일어난다는 증거다."""
        pages = [
            _json_response({"results": [WORK, WORK], "meta": {"next_cursor": "c2"}}),
            _json_response({"results": [WORK], "meta": {"next_cursor": None}}),
        ]
        transport, session = self._transport(pages)
        records = openalex.iter_search(transport, "cosmetic", 2016, 2026, limit=3)

        first = next(records)
        self.assertEqual(len(session.calls), 1)  # 첫 페이지만 요청된 상태
        second = next(records)
        self.assertEqual(len(session.calls), 1)  # 같은 페이지 안의 두 번째 레코드 — 추가 요청 없음
        third = next(records)
        self.assertEqual(len(session.calls), 2)  # limit 을 채우려 두 번째 페이지를 요청했다

        self.assertEqual([first, second, third], [openalex.to_record(WORK)] * 3)
        with self.assertRaises(StopIteration):
            next(records)

    def test_preserves_already_yielded_records_when_a_later_page_raises(self):
        """2페이지째에서 BudgetExhausted 가 나도 1페이지째에서 이미 yield 된
        레코드는 호출자 손에 남아 있어야 한다 — OpenAlex 는 요청당 과금이므로
        이미 지불한 페이지를 버리면 실제 손실이다. list() 로 감싸지 않고
        for 문으로 직접 소비해서 확인한다(list() 로 감싸면 예외가 나는 순간
        이미 모은 것까지 통째로 사라지므로 이 불변식을 증명하지 못한다)."""
        responses = [
            _json_response({"results": [WORK, WORK], "meta": {"next_cursor": "c2"}}),
            FakeResponse(402),  # BudgetExhausted — 재시도 없이 즉시 던져진다
        ]
        transport, session = self._transport(responses)
        records = openalex.iter_search(transport, "cosmetic", 2016, 2026, limit=10)

        collected = []
        with self.assertRaises(BudgetExhausted):
            for record in records:
                collected.append(record)

        self.assertEqual(collected, [openalex.to_record(WORK), openalex.to_record(WORK)])
        self.assertEqual(len(session.calls), 2)

    def test_never_yields_more_than_the_limit(self):
        page = _json_response({"results": [WORK] * 50, "meta": {"next_cursor": "c2"}})
        transport, _ = self._transport([page])
        records = list(openalex.iter_search(transport, "cosmetic", 2016, 2026, limit=2))
        self.assertEqual(len(records), 2)


class RegistryTest(unittest.TestCase):
    def test_registers_openalex_under_its_key(self):
        self.assertIs(SOURCES["openalex"], openalex.OpenAlex)


if __name__ == "__main__":
    unittest.main()
