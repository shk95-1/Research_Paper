"""evidence.pipeline: 소스 조립, 오류 타입별 캐시 동작, BudgetExhausted 중단, 자기기록.

papers/tests/test_pipeline.py 의 케이스를 FakeSession -> Transport 방식으로
이식했다(기존은 sources.*.fetch 를 mock.patch 로 대체했지만, 여기서는 실제
request 루프를 통과시켜 캐시·재시도·오류 타입까지 함께 검증한다). 추가로
이 태스크에서 신설된 것들을 검증한다: 실패 종류별 캐시 동작(NotFound 는
캐시됨 / TransientError 는 캐시 안 됨), BudgetExhausted 중단 + partial 저장
+ stopped_reason, run/run_source/fetch_log 행(같은 conn 으로 직접 조회).
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from paper_radar.contract import Fetch, SourcePolicy
from paper_radar.evidence import pipeline
from paper_radar.sources import europepmc, semantic_scholar
from paper_radar.storage import cache, repository
from paper_radar.transport.errors import BudgetExhausted
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

OPENALEX_RECORD = {
    "doi": "10.1/a",
    "openalex_id": "https://openalex.org/W1",
    "title": "Retinol and the skin barrier",
    "authors": ["Kim"],
    "year": 2024,
    "journal": None,
    "abstract": None,
    "tldr": None,
    "keywords": [],
    "topics": ["Dermatology"],
    "citation_count": 12,
    "is_open_access": True,
    "url": "https://doi.org/10.1/a",
    "is_retracted": False,
}

WORK = {
    "id": "https://openalex.org/W1",
    "doi": "https://doi.org/10.1/a",
    "title": "Retinol and the skin barrier",
    "publication_year": 2024,
    "is_retracted": False,
    "cited_by_count": 12,
    "open_access": {"is_oa": True},
    "primary_location": {"source": {"display_name": None}},
    "topics": [{"display_name": "Dermatology"}],
    "keywords": [],
    "authorships": [{"author": {"display_name": "Kim"}}],
}

WORK2 = dict(WORK, id="https://openalex.org/W2", doi="https://doi.org/10.1/b")

S2_PAYLOAD = {
    "title": "Retinol and the skin barrier",
    "abstract": "Retinol improves the barrier.",
    "tldr": {"text": "Retinol helps."},
    "citationCount": 9,
    "venue": "J Cosmet Sci",
    "year": 2024,
    "isOpenAccess": True,
}

EPMC_PAYLOAD = {
    "resultList": {
        "result": [
            {
                "title": "Retinol and the skin barrier",
                "abstractText": "From Europe PMC.",
                "journalInfo": {"journal": {"title": "Journal of Cosmetic Science"}},
                "keywordList": {"keyword": ["retinol", "skin barrier"]},
                "id": "123",
            }
        ]
    }
}

CROSSREF_PAYLOAD = {
    "message": {
        "title": ["Retinol and the skin barrier"],
        "container-title": ["Journal of Cosmetic Science"],
        "publisher": "Elsevier BV",
        "type": "journal-article",
        "issued": {"date-parts": [[2024]]},
    }
}


def _json_response(payload, status=200):
    return FakeResponse(status, body=json.dumps(payload).encode())


def _search_page(works, next_cursor=None):
    return _json_response({"results": works, "meta": {"next_cursor": next_cursor}})


def quiet():
    """의도된 경고를 삼킨다. 경고 문구 자체는 별도로 검증하지 않는다."""
    return mock.patch.object(pipeline, "warn")


class _DbTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = repository.connect(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session


class EnricherDataTest(unittest.TestCase):
    """ENRICHERS 는 순서·채움 규칙·캐시 키가 전부 데이터로 선언되어 있어야 한다."""

    def test_declares_three_enrichers_in_semantic_scholar_europepmc_crossref_order(self):
        self.assertEqual(
            [e.name for e in pipeline.ENRICHERS], ["semantic_scholar", "europepmc", "crossref"]
        )

    def test_crossref_fills_only_the_journal_field(self):
        """crossref 의 진짜 역할(제목 검증)은 evidence["crossref"] 로 verify 에
        전달되는 쪽에서 이뤄진다 — 레코드 필드로는 journal 만 채운다."""
        crossref_enricher = next(e for e in pipeline.ENRICHERS if e.name == "crossref")
        self.assertEqual(crossref_enricher.fills, (("journal", "journal"),))

    def test_semantic_scholar_and_crossref_cache_keys_are_doi_only(self):
        crossref_enricher = next(e for e in pipeline.ENRICHERS if e.name == "crossref")
        s2_enricher = next(e for e in pipeline.ENRICHERS if e.name == "semantic_scholar")
        record_without_doi = dict(OPENALEX_RECORD, doi=None)
        self.assertEqual(s2_enricher.cache_key(record_without_doi), "")
        self.assertEqual(crossref_enricher.cache_key(record_without_doi), "")

    def test_europepmc_cache_key_falls_back_to_a_truncated_lowercased_title(self):
        epmc_enricher = next(e for e in pipeline.ENRICHERS if e.name == "europepmc")
        record_without_doi = dict(OPENALEX_RECORD, doi=None, title="Retinol REVIEW " + "x" * 300)
        key = epmc_enricher.cache_key(record_without_doi)
        self.assertEqual(key, ("retinol review " + "x" * 300)[:200])
        self.assertLessEqual(len(key), 200)


class EnrichTest(_DbTestCase):
    """enrich() — 소스 하나하나가 캐시를 거쳐 record 를 채우고 verification·
    evidence 를 붙인다."""

    def _enrich(self, record=None, responses=None, error_counts=None):
        transport, session = self._transport(responses or [])
        result = pipeline.enrich(
            self.conn, transport, dict(record or OPENALEX_RECORD), error_counts
        )
        return result, session

    def test_fills_the_tldr_from_semantic_scholar(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["tldr"], "Retinol helps.")

    def test_fills_a_missing_abstract_from_semantic_scholar(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["abstract"], "Retinol improves the barrier.")

    def test_keeps_the_openalex_abstract_when_it_already_has_one(self):
        record = dict(OPENALEX_RECORD, abstract="From OpenAlex.")
        result, _ = self._enrich(
            record=record,
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ],
        )
        self.assertEqual(result["abstract"], "From OpenAlex.")

    def test_falls_back_to_europepmc_for_the_abstract_when_s2_has_none(self):
        s2_no_abstract = dict(S2_PAYLOAD, abstract=None)
        result, _ = self._enrich(
            responses=[
                _json_response(s2_no_abstract),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["abstract"], "From Europe PMC.")

    def test_prefers_the_openalex_citation_count(self):
        # 실측에서 두 소스의 인용수가 달랐다(1805 vs 1341류) — OpenAlex 를 믿는다.
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["citation_count"], 12)

    def test_fills_a_missing_citation_count_from_semantic_scholar(self):
        record = dict(OPENALEX_RECORD, citation_count=None)
        result, _ = self._enrich(
            record=record,
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ],
        )
        self.assertEqual(result["citation_count"], 9)

    def test_does_not_overwrite_a_genuine_zero_citation_count(self):
        """0 도 유효한 인용수다 — falsy 판정이 아니라 None 여부로 채움을
        판정해야 실제 0을 다른 소스 값으로 덮어쓰지 않는다."""
        record = dict(OPENALEX_RECORD, citation_count=0)
        result, _ = self._enrich(
            record=record,
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ],
        )
        self.assertEqual(result["citation_count"], 0)

    def test_fills_a_missing_journal_from_the_first_source_that_has_one(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["journal"], "J Cosmet Sci")

    def test_fills_empty_keywords_from_europepmc(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["keywords"], ["retinol", "skin barrier"])

    def test_records_every_source_that_found_the_paper_excluding_crossref(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(
            result["verification"]["found_in_sources"],
            ["openalex", "semantic_scholar", "europepmc"],
        )

    def test_omits_a_source_that_returned_not_found(self):
        with quiet():
            result, _ = self._enrich(
                responses=[
                    FakeResponse(404),
                    _json_response(EPMC_PAYLOAD),
                    _json_response(CROSSREF_PAYLOAD),
                ]
            )
        self.assertEqual(result["verification"]["found_in_sources"], ["openalex", "europepmc"])
        self.assertIsNone(result["tldr"])

    def test_scores_a_fully_enriched_paper_at_one_hundred(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["verification"]["confidence_score"], 100)

    def test_evidence_carries_every_successful_sources_raw_response(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        evidence = result["verification"]["evidence"]
        self.assertEqual(set(evidence), {"semantic_scholar", "europepmc", "crossref"})
        self.assertEqual(evidence["crossref"]["title"], "Retinol and the skin barrier")

    def test_marks_crossref_as_unverified_when_the_doi_is_unknown(self):
        with quiet():
            result, _ = self._enrich(
                responses=[
                    _json_response(S2_PAYLOAD),
                    _json_response(EPMC_PAYLOAD),
                    FakeResponse(404),
                ]
            )
        self.assertFalse(result["verification"]["crossref_verified"])
        self.assertNotIn("crossref", result["verification"]["evidence"])

    def test_stamps_a_utc_collection_timestamp(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertTrue(result["collected_at"].endswith("Z"))

    def test_skips_doi_only_sources_for_a_doi_less_paper(self):
        record = dict(OPENALEX_RECORD, doi=None)
        result, session = self._enrich(record=record, responses=[_json_response(EPMC_PAYLOAD)])
        self.assertEqual(len(session.calls), 1)  # semantic_scholar/crossref 는 호출조차 안 됨
        self.assertFalse(result["verification"]["has_doi"])
        self.assertEqual(result["verification"]["confidence_score"], 30)  # 5 + 15(europepmc) + 10

    def test_caches_enrichment_so_a_rerun_makes_no_further_calls(self):
        self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        result, session = self._enrich(responses=[])
        self.assertEqual(len(session.calls), 0)
        self.assertEqual(result["tldr"], "Retinol helps.")

    # -- 오류 타입별 캐시 동작 --------------------------------------------------

    def test_a_not_found_response_is_cached_as_a_confirmed_absence(self):
        """NotFound(404) 는 '부재 확정' — 캐시에 None 을 저장해 재실행 시 재호출을
        막는다(기존 papers/pipeline.py 의 동작 유지)."""
        with quiet():
            self._enrich(
                responses=[
                    FakeResponse(404),
                    _json_response(EPMC_PAYLOAD),
                    _json_response(CROSSREF_PAYLOAD),
                ]
            )
        cached = cache.get(self.conn, "semantic_scholar", OPENALEX_RECORD["doi"])
        self.assertIsNone(cached)  # MISS 가 아니라 저장된 "없음"

        # 재실행: 세션에 응답을 하나도 안 주지만 semantic_scholar 는 캐시를 그대로 쓴다.
        result, session = self._enrich(responses=[])
        self.assertEqual(len(session.calls), 0)
        self.assertIsNone(result["tldr"])

    def test_a_transient_error_is_not_cached_so_a_rerun_can_retry(self):
        """기존 papers/pipeline.py 는 실패도 None 으로 캐시해 영구 부재로 굳혔다.
        이 개선으로 TransientError 는 캐시하지 않아 다음 실행이 재시도할 수 있다."""
        max_attempts = semantic_scholar.SemanticScholar.policy.max_attempts
        with quiet():
            result, _ = self._enrich(
                responses=(
                    [FakeResponse(503) for _ in range(max_attempts)]
                    + [_json_response(EPMC_PAYLOAD), _json_response(CROSSREF_PAYLOAD)]
                )
            )
        self.assertIsNone(result["tldr"])
        cached = cache.get(self.conn, "semantic_scholar", OPENALEX_RECORD["doi"])
        self.assertIs(cached, cache.MISS)

        # 재실행: semantic_scholar 만 캐시가 없어 다시 불린다 — europepmc/crossref 는
        # 첫 실행에서 이미 성공해 캐시됐으므로 재호출되지 않는다.
        with quiet():
            _, session = self._enrich(responses=[_json_response(S2_PAYLOAD)])
        self.assertEqual(len(session.calls), 1)

    def test_a_transient_error_is_warned_and_counted(self):
        max_attempts = europepmc.EuropePmc.policy.max_attempts
        error_counts: dict[str, int] = {}
        with mock.patch.object(pipeline, "warn") as warn_mock:
            self._enrich(
                responses=(
                    [_json_response(S2_PAYLOAD)]
                    + [FakeResponse(503) for _ in range(max_attempts)]
                    + [_json_response(CROSSREF_PAYLOAD)]
                ),
                error_counts=error_counts,
            )
        self.assertEqual(error_counts["europepmc"], 1)
        warn_mock.assert_called_once()

    def test_budget_exhausted_propagates_with_the_offending_source_attached(self):
        """BudgetExhausted 는 삼키지 않고 그대로 올린다. collect() 가 보강 루프
        전체를 중단하려면 어느 소스에서 났는지 알아야 한다."""
        with self.assertRaises(BudgetExhausted) as ctx:
            self._enrich(responses=[FakeResponse(402)])
        self.assertEqual(ctx.exception.source, "semantic_scholar")

    def test_budget_exhausted_is_not_cached(self):
        with self.assertRaises(BudgetExhausted):
            self._enrich(responses=[FakeResponse(402)])
        cached = cache.get(self.conn, "semantic_scholar", OPENALEX_RECORD["doi"])
        self.assertIs(cached, cache.MISS)


class ErrorCachingMatrixTest(_DbTestCase):
    """오류 타입 5종(NotFound/TransientError/RateLimited/ParseError/
    PermanentError) + BudgetExhausted 각각에 대해 캐시 여부·오류 카운트·전파
    여부를 표로 확인한다. 전부 semantic_scholar(ENRICHERS 의 첫 항목)를
    상대로 재현한다 — 오류 판정은 transport 계층의 몫이라 어느 enricher 를
    쓰든 동일하게 동작해야 하지만, 첫 항목이 가장 적은 응답 큐로 재현된다."""

    def _s2_max_attempts(self):
        return semantic_scholar.SemanticScholar.policy.max_attempts

    def _enrich(self, s2_responses):
        # semantic_scholar 이후에도 europepmc/crossref 는 계속 불린다(doi 가
        # 있으므로 cache_key 가 비지 않는다) — 404 로 끝내 이 두 소스는
        # 매트릭스 판정에 끼어들지 않게 한다.
        responses = list(s2_responses) + [FakeResponse(404), FakeResponse(404)]
        transport, _ = self._transport(responses)
        error_counts: dict[str, int] = {}
        with quiet():
            result = pipeline.enrich(self.conn, transport, dict(OPENALEX_RECORD), error_counts)
        return result, error_counts

    def test_not_found_is_cached_as_none_and_not_counted_as_an_error(self):
        _, error_counts = self._enrich([FakeResponse(404)])
        self.assertIsNone(cache.get(self.conn, "semantic_scholar", OPENALEX_RECORD["doi"]))
        self.assertEqual(error_counts, {})

    def test_transient_error_is_not_cached_and_is_counted(self):
        responses = [FakeResponse(503) for _ in range(self._s2_max_attempts())]
        _, error_counts = self._enrich(responses)
        self.assertIs(cache.get(self.conn, "semantic_scholar", OPENALEX_RECORD["doi"]), cache.MISS)
        self.assertEqual(error_counts["semantic_scholar"], 1)

    def test_rate_limited_is_not_cached_and_is_counted(self):
        responses = [FakeResponse(429) for _ in range(self._s2_max_attempts())]
        _, error_counts = self._enrich(responses)
        self.assertIs(cache.get(self.conn, "semantic_scholar", OPENALEX_RECORD["doi"]), cache.MISS)
        self.assertEqual(error_counts["semantic_scholar"], 1)

    def test_parse_error_is_not_cached_and_is_counted(self):
        _, error_counts = self._enrich([FakeResponse(200, body=b"<html>not json</html>")])
        self.assertIs(cache.get(self.conn, "semantic_scholar", OPENALEX_RECORD["doi"]), cache.MISS)
        self.assertEqual(error_counts["semantic_scholar"], 1)

    def test_permanent_error_is_not_cached_and_is_counted(self):
        _, error_counts = self._enrich([FakeResponse(418)])
        self.assertIs(cache.get(self.conn, "semantic_scholar", OPENALEX_RECORD["doi"]), cache.MISS)
        self.assertEqual(error_counts["semantic_scholar"], 1)

    def test_budget_exhausted_is_not_cached_not_counted_and_propagates(self):
        transport, _ = self._transport([FakeResponse(402)])
        error_counts: dict[str, int] = {}
        with self.assertRaises(BudgetExhausted):
            pipeline.enrich(self.conn, transport, dict(OPENALEX_RECORD), error_counts)
        self.assertIs(cache.get(self.conn, "semantic_scholar", OPENALEX_RECORD["doi"]), cache.MISS)
        self.assertEqual(error_counts, {})


class CollectTest(_DbTestCase):
    """collect() — 검색부터 저장, RunLog 자기기록까지 전 과정."""

    def test_reusing_the_same_transport_after_collect_does_not_leak_into_the_finished_run(self):
        """collect() 가 설치한 observer 를 떼어내지 않으면, 같은 Transport 를
        재사용한 다음 요청이 끝난 run_id 로 계속 fetch_log 에 쌓인다(누수) —
        collect() 종료 후에는 관측 훅이 원래 상태(observer 없음)로 되돌아가
        있어야 한다."""
        responses = [
            _search_page([WORK]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            _json_response(CROSSREF_PAYLOAD),
            FakeResponse(200),  # collect() 종료 후 같은 transport 로 보내는 요청
        ]
        transport, _ = self._transport(responses)

        report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)
        fetch_count_after_collect = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (report.run_id,)
        ).fetchone()[0]
        self.assertGreater(fetch_count_after_collect, 0)  # collect() 진행 중에는 정상 기록됨

        transport.request(
            Fetch(url="https://api.example.org/x"),
            SourcePolicy(host="api.example.org", min_interval_s=0.0),
        )

        fetch_count_after_reuse = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (report.run_id,)
        ).fetchone()[0]
        self.assertEqual(fetch_count_after_reuse, fetch_count_after_collect)

    def test_a_full_run_stores_enriches_and_records_run_metadata(self):
        responses = [
            _search_page([WORK]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            _json_response(CROSSREF_PAYLOAD),
        ]
        transport, session = self._transport(responses)

        report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)

        self.assertEqual(report.status, "ok")
        self.assertEqual(len(report.records), 1)
        self.assertEqual(report.records[0]["verification"]["confidence_score"], 100)
        self.assertEqual(report.stopped_reason, {})
        self.assertEqual(len(session.calls), 4)

        stored_count = self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        self.assertEqual(stored_count, 1)

        run_row = self.conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (report.run_id,)
        ).fetchone()
        self.assertEqual(run_row["status"], "ok")
        self.assertIsNotNone(run_row["finished_at"])
        self.assertEqual(run_row["command"], "evidence collect")

        openalex_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'openalex'", (report.run_id,)
        ).fetchone()
        self.assertEqual(openalex_row["requests"], 1)
        self.assertEqual(openalex_row["records"], 1)
        self.assertEqual(openalex_row["errors"], 0)
        self.assertIsNone(openalex_row["stopped_reason"])

        for source in ("semantic_scholar", "europepmc", "crossref"):
            row = self.conn.execute(
                "SELECT * FROM run_source WHERE run_id = ? AND source = ?",
                (report.run_id, source),
            ).fetchone()
            self.assertEqual(row["requests"], 1, source)
            self.assertEqual(row["records"], 1, source)

        fetch_count = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (report.run_id,)
        ).fetchone()[0]
        self.assertEqual(fetch_count, 4)

    def test_returns_no_records_when_the_search_finds_nothing(self):
        transport, _ = self._transport([_search_page([])])
        report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)
        self.assertEqual(report.records, [])
        self.assertEqual(report.status, "ok")

    def test_a_second_run_updates_rather_than_duplicates(self):
        per_run = [
            _search_page([WORK]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            _json_response(CROSSREF_PAYLOAD),
        ]
        transport1, _ = self._transport(list(per_run))
        pipeline.collect(self.conn, transport1, "cosmetic", 2016, 2026, 10)
        transport2, _ = self._transport(list(per_run))
        pipeline.collect(self.conn, transport2, "cosmetic", 2016, 2026, 10)

        stored_count = self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        self.assertEqual(stored_count, 1)

    def test_records_without_an_identifier_are_skipped_but_the_batch_continues(self):
        broken = {"id": None}
        responses = [
            _search_page([WORK, broken]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            _json_response(CROSSREF_PAYLOAD),
        ]
        transport, _ = self._transport(responses)
        with quiet():
            report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)
        self.assertEqual(len(report.records), 1, "식별자 없는 레코드는 건너뛰고 나머지는 저장한다")

    def test_writes_the_json_backup_when_asked(self):
        target = self.path + ".json"
        self.addCleanup(lambda: os.path.exists(target) and os.unlink(target))
        responses = [
            _search_page([WORK]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            _json_response(CROSSREF_PAYLOAD),
        ]
        transport, _ = self._transport(responses)
        pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10, json_path=target)
        self.assertTrue(os.path.exists(target))

    def test_calls_on_progress_once_per_stored_record(self):
        responses = [
            _search_page([WORK]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            _json_response(CROSSREF_PAYLOAD),
        ]
        transport, _ = self._transport(responses)
        seen = []
        pipeline.collect(
            self.conn,
            transport,
            "cosmetic",
            2016,
            2026,
            10,
            on_progress=lambda index, total, record: seen.append((index, total)),
        )
        self.assertEqual(seen, [(1, 1)])

    def test_budget_exhausted_during_search_is_partial_and_keeps_already_yielded_records(self):
        """검색 단계 BudgetExhausted 도 보강 단계와 동일하게 status="partial" +
        openalex stopped_reason="budget_exhausted" — 이미 받은(1페이지) 레코드는
        그대로 보강·저장 단계로 넘어간다(iter_search 의 점진 소비)."""
        responses = [
            _search_page([WORK], next_cursor="c2"),
            FakeResponse(402),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            _json_response(CROSSREF_PAYLOAD),
        ]
        transport, _ = self._transport(responses)

        report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)

        self.assertEqual(report.status, "partial")
        self.assertEqual(report.stopped_reason, {"openalex": "budget_exhausted"})
        self.assertEqual(len(report.records), 1)

        run_row = self.conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (report.run_id,)
        ).fetchone()
        self.assertEqual(run_row["status"], "partial")

        openalex_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'openalex'", (report.run_id,)
        ).fetchone()
        self.assertEqual(openalex_row["stopped_reason"], "budget_exhausted")

    def test_budget_exhausted_during_enrichment_stops_the_whole_batch_but_keeps_prior_records(self):
        """2번째 레코드의 semantic_scholar 에서 예산이 소진되면, 이미 완전히
        처리·저장된 1번째 레코드는 남고 보강 루프 전체가 중단된다."""
        responses = [
            _search_page([WORK, WORK2]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            _json_response(CROSSREF_PAYLOAD),
            FakeResponse(402),  # WORK2 의 semantic_scholar
        ]
        transport, _ = self._transport(responses)

        report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)

        self.assertEqual(report.status, "partial")
        self.assertEqual(report.stopped_reason, {"semantic_scholar": "budget_exhausted"})
        self.assertEqual(len(report.records), 1)

        stored_count = self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        self.assertEqual(stored_count, 1)

        s2_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'semantic_scholar'",
            (report.run_id,),
        ).fetchone()
        self.assertEqual(s2_row["requests"], 2)
        self.assertEqual(s2_row["records"], 1)
        self.assertEqual(s2_row["stopped_reason"], "budget_exhausted")

    def test_a_transient_source_error_marks_the_run_partial_but_keeps_collecting(self):
        max_attempts = europepmc.EuropePmc.policy.max_attempts
        responses = (
            [_search_page([WORK]), _json_response(S2_PAYLOAD)]
            + [FakeResponse(503) for _ in range(max_attempts)]
            + [_json_response(CROSSREF_PAYLOAD)]
        )
        transport, _ = self._transport(responses)

        with quiet():
            report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)

        self.assertEqual(report.status, "partial")
        self.assertEqual(report.errors_by_source["europepmc"], 1)
        self.assertEqual(len(report.records), 1)

        run_row = self.conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (report.run_id,)
        ).fetchone()
        self.assertEqual(run_row["status"], "partial")

    def test_a_pipeline_bug_leaves_the_run_marked_failed_and_re_raises(self):
        """수집 로직 자체가 처리하지 못한 예외로 끝나면 run.status="failed" 로
        남아야 한다(finally 가 보장) — "running" 으로 영원히 남는 죽은 run 을
        막기 위함이다."""
        transport, _ = self._transport([_search_page([WORK])])
        with (
            mock.patch.object(pipeline, "enrich", side_effect=RuntimeError("버그")),
            self.assertRaises(RuntimeError),
        ):
            pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)

        run_row = self.conn.execute(
            "SELECT status FROM run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(run_row["status"], "failed")


if __name__ == "__main__":
    unittest.main()
