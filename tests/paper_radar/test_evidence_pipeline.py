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
from paper_radar.sources import europepmc, semantic_scholar, unpaywall
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
WORK3 = dict(WORK, id="https://openalex.org/W3", doi="https://doi.org/10.1/c")

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

# T9 — WORK 의 doi("10.1/a")가 철회됐다고 Crossref 가 보고하는 응답. relation
# 경로(철회된 논문 자신의 응답)를 쓴다.
CROSSREF_PAYLOAD_RETRACTED = {
    "message": {
        "DOI": "10.1/a",
        "title": ["Retinol and the skin barrier"],
        "container-title": ["Journal of Cosmetic Science"],
        "publisher": "Elsevier BV",
        "type": "journal-article",
        "issued": {"date-parts": [[2024]]},
        "relation": {"is-retracted-by": [{"id": "10.1/notice", "id-type": "doi"}]},
    }
}

# T9(리뷰 Important 대응) — WORK 의 doi("10.1/a")가 철회 "공지" 문서 자신인
# 응답. update-to 경로(철회 공지 자신의 응답)를 쓴다 — role="notice" 로
# 파싱돼, 이 레코드(공지 자신)의 점수는 0 이 되면 안 되고 retraction 테이블
# 행의 doi/retraction_doi 는 뒤바뀌어(doi=원 논문, retraction_doi=공지 자신)
# 저장돼야 한다.
CROSSREF_PAYLOAD_NOTICE = {
    "message": {
        "DOI": "10.1/a",
        "title": ["Retinol and the skin barrier"],
        "container-title": ["Journal of Cosmetic Science"],
        "publisher": "Elsevier BV",
        "type": "journal-article",
        "issued": {"date-parts": [[2024]]},
        "update-to": [
            {
                "DOI": "10.1/original-paper",
                "type": "retraction",
                "updated": {"date-parts": [[2024, 3, 15]]},
            }
        ],
    }
}

# unpaywall — WORK 의 doi("10.1/a")에 대한 응답. collect() 는 papers 저장이
# 끝난 뒤 별도 단계로 이걸 조회한다(evidence/verify 에는 들어가지 않는다).
UNPAYWALL_PAYLOAD = {
    "doi": "10.1/a",
    "is_oa": True,
    "oa_status": "hybrid",
    "best_oa_location": {
        "url": "https://example.org/landing",
        "url_for_pdf": "https://example.org/article.pdf",
        "host_type": "publisher",
        "license": "cc-by",
    },
}

# pubmed(T10) — esearch 로 PMID 를 찾고 efetch(XML)로 본문을 가져오는 두 단계
# 조회다. abstract/journal 을 일부러 비워 뒀다(구성 예시 — 실검증은
# tool/live_smoke.py 가 한다) — 그래야 이 픽스처를 기존 테스트들의 응답
# 큐에 끼워 넣어도 "비었을 때만 채운다" 규칙 때문에 S2/EuropePMC 가 이미
# 채운 abstract/journal 값에 대한 기존 단언들이 그대로 유지된다. mesh_terms
# 만 이 소스의 진짜 관심사라 채워 둔다.
PUBMED_ESEARCH_PAYLOAD = {"esearchresult": {"idlist": ["999"], "count": "1"}}
PUBMED_ESEARCH_EMPTY = {"esearchresult": {"idlist": [], "count": "0"}}
PUBMED_EFETCH_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle>
<MedlineCitation>
<PMID>999</PMID>
<Article>
<ArticleTitle>Retinol and the skin barrier</ArticleTitle>
</Article>
<MeshHeadingList>
<MeshHeading><DescriptorName>Retinol</DescriptorName></MeshHeading>
<MeshHeading><DescriptorName>Skin Aging</DescriptorName></MeshHeading>
</MeshHeadingList>
</MedlineCitation>
<PubmedData>
<ArticleIdList>
<ArticleId IdType="doi">10.1/a</ArticleId>
</ArticleIdList>
</PubmedData>
</PubmedArticle>
</PubmedArticleSet>"""

# 위와 달리 abstract 도 함께 채워 둔 변형 — "abstract 는 비었을 때만 채운다"
# 규칙과 "mesh_terms 는 항상 기록한다" 규칙을 같은 응답으로 동시에 검증할 때 쓴다.
PUBMED_EFETCH_XML_WITH_ABSTRACT = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle>
<MedlineCitation>
<PMID>999</PMID>
<Article>
<ArticleTitle>Retinol and the skin barrier</ArticleTitle>
<Abstract><AbstractText>From PubMed.</AbstractText></Abstract>
</Article>
<MeshHeadingList>
<MeshHeading><DescriptorName>Retinol</DescriptorName></MeshHeading>
</MeshHeadingList>
</MedlineCitation>
</PubmedArticle>
</PubmedArticleSet>"""

# MeshHeadingList 자체가 없는 응답 — "PubMed 는 응답했지만 그 논문에 MeSH 가
# 없었다"를 흉내낸다. mesh_terms 는 빈 튜플이어야 하고, 그래도 record 에
# 기록은 돼야 한다(m0006/repository 가 NULL 로 접는 건 그다음 단계).
PUBMED_EFETCH_XML_NO_MESH = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle>
<MedlineCitation>
<PMID>999</PMID>
<Article>
<ArticleTitle>Retinol and the skin barrier</ArticleTitle>
</Article>
</MedlineCitation>
</PubmedArticle>
</PubmedArticleSet>"""


def _json_response(payload, status=200):
    return FakeResponse(status, body=json.dumps(payload).encode())


def _pubmed_responses():
    """pubmed.fetch() 가 doi 가 있을 때 순차로 만드는 두 응답(esearch, efetch).

    ENRICHERS 순서(semantic_scholar -> europepmc -> pubmed -> crossref)상
    europepmc 응답과 crossref 응답 사이에 끼워 넣는다.
    """
    return [
        _json_response(PUBMED_ESEARCH_PAYLOAD),
        FakeResponse(200, body=PUBMED_EFETCH_XML.encode()),
    ]


def _pubmed_not_found_response():
    """esearch 가 0건을 답한 경우 — efetch 는 아예 호출되지 않는다."""
    return [_json_response(PUBMED_ESEARCH_EMPTY)]


def _pubmed_responses_with_abstract():
    return [
        _json_response(PUBMED_ESEARCH_PAYLOAD),
        FakeResponse(200, body=PUBMED_EFETCH_XML_WITH_ABSTRACT.encode()),
    ]


def _pubmed_responses_no_mesh():
    return [
        _json_response(PUBMED_ESEARCH_PAYLOAD),
        FakeResponse(200, body=PUBMED_EFETCH_XML_NO_MESH.encode()),
    ]


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

    def test_declares_four_enrichers_in_semantic_scholar_europepmc_pubmed_crossref_order(self):
        # T10 — pubmed 가 europepmc 와 crossref 사이에 추가됐다(브리핑 지시
        # 순서). 예전 이름(test_declares_three_enrichers_...)이 가리키던
        # "세 소스"는 더 이상 사실이 아니라 이름 자체를 갱신한다.
        self.assertEqual(
            [e.name for e in pipeline.ENRICHERS],
            ["semantic_scholar", "europepmc", "pubmed", "crossref"],
        )

    def test_crossref_fills_only_the_journal_field(self):
        """crossref 의 진짜 역할(제목 검증)은 evidence["crossref"] 로 verify 에
        전달되는 쪽에서 이뤄진다 — 레코드 필드로는 journal 만 채운다."""
        crossref_enricher = next(e for e in pipeline.ENRICHERS if e.name == "crossref")
        self.assertEqual(crossref_enricher.fills, (("journal", "journal"),))

    def test_semantic_scholar_and_crossref_and_pubmed_cache_keys_are_doi_only(self):
        crossref_enricher = next(e for e in pipeline.ENRICHERS if e.name == "crossref")
        s2_enricher = next(e for e in pipeline.ENRICHERS if e.name == "semantic_scholar")
        pubmed_enricher = next(e for e in pipeline.ENRICHERS if e.name == "pubmed")
        record_without_doi = dict(OPENALEX_RECORD, doi=None)
        self.assertEqual(s2_enricher.cache_key(record_without_doi), "")
        self.assertEqual(crossref_enricher.cache_key(record_without_doi), "")
        self.assertEqual(pubmed_enricher.cache_key(record_without_doi), "")

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
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["tldr"], "Retinol helps.")

    def test_fills_a_missing_abstract_from_semantic_scholar(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
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
                *_pubmed_responses(),
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
                *_pubmed_responses(),
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
                *_pubmed_responses(),
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
                *_pubmed_responses(),
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
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD),
            ],
        )
        self.assertEqual(result["citation_count"], 0)

    def test_fills_a_missing_journal_from_the_first_source_that_has_one(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["journal"], "J Cosmet Sci")

    def test_fills_empty_keywords_from_europepmc(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["keywords"], ["retinol", "skin barrier"])

    def test_records_every_source_that_found_the_paper_excluding_crossref(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(
            result["verification"]["found_in_sources"],
            ["openalex", "semantic_scholar", "europepmc", "pubmed"],
        )

    def test_omits_a_source_that_returned_not_found(self):
        with quiet():
            result, _ = self._enrich(
                responses=[
                    FakeResponse(404),
                    _json_response(EPMC_PAYLOAD),
                    *_pubmed_responses(),
                    _json_response(CROSSREF_PAYLOAD),
                ]
            )
        self.assertEqual(
            result["verification"]["found_in_sources"], ["openalex", "europepmc", "pubmed"]
        )
        self.assertIsNone(result["tldr"])

    def test_scores_a_fully_enriched_paper_at_one_hundred(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertEqual(result["verification"]["confidence_score"], 100)

    def test_evidence_carries_every_successful_sources_raw_response(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        evidence = result["verification"]["evidence"]
        self.assertEqual(set(evidence), {"semantic_scholar", "europepmc", "pubmed", "crossref"})
        self.assertEqual(evidence["crossref"]["title"], "Retinol and the skin barrier")

    def test_stores_a_retraction_row_and_marks_the_record_retracted_when_crossref_flags_one(self):
        """T9: crossref 의 retractions 신호가 (a) retraction 테이블에 저장되고
        (b) verify.build() 의 교차 검증을 거쳐 is_retracted/점수 0 에 반영돼야
        한다 — 두 표면 모두 확인한다."""
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD_RETRACTED),
            ]
        )
        self.assertTrue(result["verification"]["is_retracted"])
        self.assertEqual(result["verification"]["confidence_score"], 0)

        row = self.conn.execute("SELECT * FROM retraction WHERE doi = '10.1/a'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["retraction_doi"], "10.1/notice")
        self.assertEqual(row["update_type"], "retraction")
        self.assertIsNone(row["update_date"])
        self.assertEqual(row["source"], "crossref")

    def test_a_retraction_notice_record_swaps_doi_roles_and_keeps_its_own_score(self):
        """리뷰 Important 대응: 수집 대상 자신이 철회 공지 문서일 때
        (update-to 경로, role="notice") — (a) retraction 테이블 행의
        doi/retraction_doi 가 스키마 의미(doi=철회된 논문, retraction_doi=
        공지)대로 뒤바뀌어 저장되고, (b) 이 레코드(공지 자신)는 철회된
        논문이 아니므로 점수가 0 이 되면 안 된다."""
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD_NOTICE),
            ]
        )
        self.assertFalse(result["verification"]["is_retracted"])
        self.assertEqual(result["verification"]["confidence_score"], 100)

        row = self.conn.execute(
            "SELECT * FROM retraction WHERE doi = '10.1/original-paper'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["retraction_doi"], "10.1/a")  # 공지 자신의 doi
        self.assertEqual(row["update_type"], "retraction")
        self.assertEqual(row["update_date"], "2024-03-15")
        self.assertEqual(row["source"], "crossref")

        # role="retracted" 경로(WORK 자신이 철회된 논문)로 오인해 doi="10.1/a"
        # 로 저장되는 행은 없어야 한다 — role 뒤집힘 버그의 회귀 감지.
        wrong_row = self.conn.execute("SELECT * FROM retraction WHERE doi = '10.1/a'").fetchone()
        self.assertIsNone(wrong_row)

    def test_does_not_write_a_retraction_row_when_crossref_reports_none(self):
        result, _ = self._enrich(
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
                _json_response(CROSSREF_PAYLOAD),
            ]
        )
        self.assertFalse(result["verification"]["is_retracted"])
        count = self.conn.execute("SELECT COUNT(*) FROM retraction").fetchone()[0]
        self.assertEqual(count, 0)

    def test_marks_crossref_as_unverified_when_the_doi_is_unknown(self):
        with quiet():
            result, _ = self._enrich(
                responses=[
                    _json_response(S2_PAYLOAD),
                    _json_response(EPMC_PAYLOAD),
                    *_pubmed_responses(),
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
                *_pubmed_responses(),
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
                *_pubmed_responses(),
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
                    *_pubmed_responses(),
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
                    + [_json_response(EPMC_PAYLOAD)]
                    + _pubmed_responses()
                    + [_json_response(CROSSREF_PAYLOAD)]
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
                    + [*_pubmed_responses(), _json_response(CROSSREF_PAYLOAD)]
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


class PubMedMeshTermsTest(_DbTestCase):
    """T10 전용: mesh_terms 는 fills 규칙(비었을 때만 채운다)과 무관하게
    항상 기록되고, pubmed 는 다른 소스와 마찬가지로 found_in_sources 에
    들어가 추가소스 점수(15/개, 상한 30)를 받는다. score-parity 고정 CASES
    (test_verify.py)는 pubmed 없는 입력이므로 이 클래스와 무관하게 그대로
    남아 있다 — 여기서는 pubmed 가 실제로 참여하는 새 케이스만 다룬다."""

    def _enrich(self, record=None, responses=None, error_counts=None):
        transport, session = self._transport(responses or [])
        result = pipeline.enrich(
            self.conn, transport, dict(record or OPENALEX_RECORD), error_counts
        )
        return result, session

    def test_mesh_terms_is_recorded_even_when_the_paper_already_has_an_abstract(self):
        # abstract 는 fills 규칙(비었을 때만 채운다) 때문에 그대로 남지만,
        # mesh_terms 는 그 규칙과 무관하게 기록돼야 한다.
        record = dict(OPENALEX_RECORD, abstract="Existing abstract.")
        result, _ = self._enrich(
            record=record,
            responses=[
                FakeResponse(404),  # semantic_scholar
                FakeResponse(404),  # europepmc
                *_pubmed_responses_with_abstract(),
                FakeResponse(404),  # crossref
            ],
        )
        self.assertEqual(result["abstract"], "Existing abstract.")
        self.assertEqual(result["mesh_terms"], ("Retinol",))

    def test_pubmed_still_fills_a_missing_abstract(self):
        # 다른 소스가 채우지 못한 abstract 라면 pubmed 도 다른 enricher 와
        # 동일하게 "비었을 때만 채운다" 규칙을 따른다.
        result, _ = self._enrich(
            responses=[
                FakeResponse(404),
                FakeResponse(404),
                *_pubmed_responses_with_abstract(),
                FakeResponse(404),
            ],
        )
        self.assertEqual(result["abstract"], "From PubMed.")

    def test_records_an_empty_mesh_terms_tuple_when_pubmed_reports_none(self):
        # "PubMed 가 응답했지만 이 논문엔 MeSH 가 없었다"도 기록 대상이다 —
        # NULL 로 접는 건 repository 의 몫이지, pipeline 이 조용히 생략하면
        # 안 된다(빈 튜플과 "아직 조회 안 함"을 구분하려면 항상 써야 한다).
        result, _ = self._enrich(
            responses=[
                FakeResponse(404),
                FakeResponse(404),
                *_pubmed_responses_no_mesh(),
                FakeResponse(404),
            ],
        )
        self.assertIn("mesh_terms", result)
        self.assertEqual(result["mesh_terms"], ())

    def test_a_doi_less_paper_never_gets_mesh_terms(self):
        # pubmed 도 semantic_scholar/crossref 와 같은 이유로 DOI 없는
        # 논문은 아예 조회하지 않는다(cache_key 가 "") — mesh_terms 키
        # 자체가 record 에 생기지 않는다.
        record = dict(OPENALEX_RECORD, doi=None)
        result, session = self._enrich(record=record, responses=[FakeResponse(404)])  # europepmc
        self.assertNotIn("mesh_terms", result)
        self.assertEqual(len(session.calls), 1)

    def test_no_mesh_terms_key_when_pubmed_finds_no_matching_pmid(self):
        # esearch 0건은 pubmed.fetch() 가 None 을 돌려주는 정상 부재다 —
        # 다른 소스의 404 와 마찬가지로 evidence/found_in_sources 어디에도
        # "pubmed" 가 나타나지 않고, mesh_terms 키도 record 에 생기지 않는다.
        result, session = self._enrich(
            responses=[
                FakeResponse(404),  # semantic_scholar
                FakeResponse(404),  # europepmc
                *_pubmed_not_found_response(),
                FakeResponse(404),  # crossref
            ],
        )
        self.assertNotIn("mesh_terms", result)
        self.assertNotIn("pubmed", result["verification"]["found_in_sources"])
        self.assertNotIn("pubmed", result["verification"]["evidence"])
        self.assertEqual(len(session.calls), 4)  # efetch 는 아예 호출되지 않는다

    def test_pubmed_participation_counts_toward_found_in_sources(self):
        result, _ = self._enrich(
            responses=[
                FakeResponse(404),
                FakeResponse(404),
                *_pubmed_responses(),
                FakeResponse(404),
            ],
        )
        self.assertEqual(result["verification"]["found_in_sources"], ["openalex", "pubmed"])

    def test_three_extra_sources_including_pubmed_still_cap_the_score_at_thirty(self):
        """openalex + semantic_scholar + europepmc + pubmed(3 개의 추가 소스)는
        규칙대로면 15*3=45 점이지만 SCORE_EXTRA_SOURCE_CAP=30 에서 막힌다.
        crossref 는 일부러 404 로 빼서(검증 30/제목일치 25 를 관여시키지
        않고) 상한 계산만 순수하게 드러낸다: 5(기본) + 30(상한) + 10(초록)
        = 45 — cap 이 없었다면 5 + 45 + 10 = 60 이 됐을 것이다."""
        record = dict(OPENALEX_RECORD, abstract="Existing abstract.")
        result, _ = self._enrich(
            record=record,
            responses=[
                _json_response(S2_PAYLOAD),
                _json_response(EPMC_PAYLOAD),
                *_pubmed_responses(),
                FakeResponse(404),  # crossref
            ],
        )
        self.assertEqual(
            result["verification"]["found_in_sources"],
            ["openalex", "semantic_scholar", "europepmc", "pubmed"],
        )
        self.assertEqual(result["verification"]["confidence_score"], 45)  # 5 + 30(cap) + 10


class ErrorCachingMatrixTest(_DbTestCase):
    """오류 타입 5종(NotFound/TransientError/RateLimited/ParseError/
    PermanentError) + BudgetExhausted 각각에 대해 캐시 여부·오류 카운트·전파
    여부를 표로 확인한다. 전부 semantic_scholar(ENRICHERS 의 첫 항목)를
    상대로 재현한다 — 오류 판정은 transport 계층의 몫이라 어느 enricher 를
    쓰든 동일하게 동작해야 하지만, 첫 항목이 가장 적은 응답 큐로 재현된다."""

    def _s2_max_attempts(self):
        return semantic_scholar.SemanticScholar.policy.max_attempts

    def _enrich(self, s2_responses):
        # semantic_scholar 이후에도 europepmc/pubmed/crossref 는 계속
        # 불린다(doi 가 있으므로 cache_key 가 비지 않는다) — 전부 404 로
        # 끝내 이 세 소스는 매트릭스 판정에 끼어들지 않게 한다. pubmed 는
        # esearch 한 번만으로 404(NotFound)가 나면 efetch 를 아예 부르지
        # 않으므로(fetch() 참고) 다른 두 소스와 마찬가지로 404 하나면 된다.
        responses = list(s2_responses) + [FakeResponse(404), FakeResponse(404), FakeResponse(404)]
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
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            _json_response(UNPAYWALL_PAYLOAD),
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
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            _json_response(UNPAYWALL_PAYLOAD),
        ]
        transport, session = self._transport(responses)

        report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)

        self.assertEqual(report.status, "ok")
        self.assertEqual(len(report.records), 1)
        self.assertEqual(report.records[0]["verification"]["confidence_score"], 100)
        self.assertEqual(report.stopped_reason, {})
        self.assertEqual(len(session.calls), 7)  # T10: pubmed 가 esearch+efetch 2건을 더한다

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

        for source in ("semantic_scholar", "europepmc", "crossref", "unpaywall"):
            row = self.conn.execute(
                "SELECT * FROM run_source WHERE run_id = ? AND source = ?",
                (report.run_id, source),
            ).fetchone()
            self.assertEqual(row["requests"], 1, source)
            self.assertEqual(row["records"], 1, source)

        # pubmed 는 esearch+efetch 두 요청이라 requests=2 다(다른 소스는 1).
        pubmed_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'pubmed'", (report.run_id,)
        ).fetchone()
        self.assertEqual(pubmed_row["requests"], 2)
        self.assertEqual(pubmed_row["records"], 1)

        fetch_count = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (report.run_id,)
        ).fetchone()[0]
        self.assertEqual(fetch_count, 7)

        oa_row = self.conn.execute(
            "SELECT * FROM oa_location WHERE doi = '10.1/a'"
        ).fetchone()
        self.assertEqual(oa_row["pdf_url"], "https://example.org/article.pdf")

        # unpaywall 은 evidence/found_in_sources 어디에도 나타나지 않는다 —
        # 서지 소스가 아니라 부가 정보다(점수 불변 회귀 테스트는 아래
        # ScoreInvarianceTest 참고).
        evidence = report.records[0]["verification"]["evidence"]
        self.assertNotIn("unpaywall", evidence)
        self.assertNotIn("unpaywall", report.records[0]["verification"]["found_in_sources"])

    def test_a_crossref_retraction_is_reflected_in_the_retraction_table_and_papers_is_retracted(
        self,
    ):
        """T9: collect() 전체 흐름에서 crossref 철회 신호가 (a) retraction
        테이블 행과 (b) papers.is_retracted(및 raw 안의 verification) 양쪽에
        반영돼야 한다."""
        responses = [
            _search_page([WORK]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD_RETRACTED),
            _json_response(UNPAYWALL_PAYLOAD),
        ]
        transport, _ = self._transport(responses)

        report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)

        self.assertTrue(report.records[0]["is_retracted"])
        self.assertEqual(report.records[0]["verification"]["confidence_score"], 0)

        papers_row = self.conn.execute(
            "SELECT is_retracted FROM papers WHERE doi = '10.1/a'"
        ).fetchone()
        self.assertEqual(papers_row["is_retracted"], 1)

        retraction_row = self.conn.execute(
            "SELECT * FROM retraction WHERE doi = '10.1/a'"
        ).fetchone()
        self.assertIsNotNone(retraction_row)
        self.assertEqual(retraction_row["retraction_doi"], "10.1/notice")
        self.assertEqual(retraction_row["source"], "crossref")

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
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            _json_response(UNPAYWALL_PAYLOAD),
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
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            _json_response(UNPAYWALL_PAYLOAD),
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
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            _json_response(UNPAYWALL_PAYLOAD),
        ]
        transport, _ = self._transport(responses)
        pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10, json_path=target)
        self.assertTrue(os.path.exists(target))

    def test_calls_on_progress_once_per_stored_record(self):
        responses = [
            _search_page([WORK]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            _json_response(UNPAYWALL_PAYLOAD),
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
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            _json_response(UNPAYWALL_PAYLOAD),
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
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            _json_response(UNPAYWALL_PAYLOAD),  # WORK 의 unpaywall(enrich 완료 후)
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
            + _pubmed_responses()
            + [_json_response(CROSSREF_PAYLOAD), _json_response(UNPAYWALL_PAYLOAD)]
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

    def test_skips_oa_resolution_for_a_record_without_a_doi(self):
        """DOI 없는 논문은 semantic_scholar/crossref 와 마찬가지로 unpaywall
        도 아예 조회하지 않는다 — oa_location 에 빈 doi 로 행이 생기지 않는다."""
        work_without_doi = dict(WORK, id="https://openalex.org/W3", doi=None)
        responses = [_search_page([work_without_doi]), _json_response(EPMC_PAYLOAD)]
        transport, session = self._transport(responses)

        report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)

        self.assertEqual(len(report.records), 1)
        # openalex 검색 1회 + europepmc(제목 기반) 1회만 — semantic_scholar/
        # crossref/unpaywall 은 doi 가 없어 전혀 호출되지 않는다.
        self.assertEqual(len(session.calls), 2)
        oa_count = self.conn.execute("SELECT COUNT(*) FROM oa_location").fetchone()[0]
        self.assertEqual(oa_count, 0)

    def test_budget_exhausted_during_oa_resolution_keeps_the_already_upserted_paper_and_stops(
        self,
    ):
        """OA 단계의 BudgetExhausted 는 enrich 단계와 의도적으로 다르게 동작한다:
        그 레코드의 papers 행은 이미(OA 조회 전에) 저장이 끝난 뒤이므로, OA 조회가
        실패해도 stored 에서 빼지 않는다 — 저장은 되돌리지 않고, 이후 레코드
        시도만 중단한다. 3건 중 2번째의 unpaywall 에서 402 를 받는 시나리오로
        (a) report.records 에 1·2번째가 남는지, (b) 2번째의 papers 행이 실제로
        DB 에 저장돼 있는지, (c) 3번째는 시도조차 안 되는지, (d) stopped_reason
        이 {"unpaywall": "budget_exhausted"}인지, (e) run.status 가 "partial"인지
        를 전부 확인한다."""
        responses = [
            _search_page([WORK, WORK2, WORK3]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            _json_response(UNPAYWALL_PAYLOAD),  # WORK 의 unpaywall — 성공
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            FakeResponse(402),  # WORK2 의 unpaywall — 예산 소진
        ]
        transport, session = self._transport(responses)

        report = pipeline.collect(self.conn, transport, "cosmetic", 2016, 2026, 10)

        self.assertEqual(report.status, "partial")  # (e)
        self.assertEqual(report.stopped_reason, {"unpaywall": "budget_exhausted"})  # (d)

        # (a) 1·2번째 레코드 둘 다 report.records 에 남는다 — 각각 enrich+upsert
        # 까지는 완전히 끝난 뒤에 OA 단계에서 중단됐을 뿐이다.
        self.assertEqual(len(report.records), 2)
        self.assertEqual({r["doi"] for r in report.records}, {"10.1/a", "10.1/b"})

        # (b) 2번째(WORK2)의 papers 행도 실제로 저장돼 있다 — OA 단계 전에
        # 이미 repository.upsert() 가 끝났기 때문이다.
        stored_count = self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        self.assertEqual(stored_count, 2)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM papers WHERE doi = '10.1/b'").fetchone()
        )

        # (c) 3번째(WORK3)는 아예 시도되지 않는다 — 응답 큐를 정확히 다 썼는데
        # 그 이상 소비했다면 FakeSession 이 AssertionError 를 던졌을 것이다.
        # (T10: pubmed 의 esearch+efetch 2건 x 레코드 2개 = 4건이 더해져 13이다.)
        self.assertEqual(len(session.calls), 13)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM papers WHERE doi = '10.1/c'").fetchone()
        )

        # 1번째(WORK)는 unpaywall 도 성공했으니 oa_location 에 딱 1행만 있다.
        oa_count = self.conn.execute("SELECT COUNT(*) FROM oa_location").fetchone()[0]
        self.assertEqual(oa_count, 1)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM oa_location WHERE doi = '10.1/a'").fetchone()
        )

        unpaywall_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'unpaywall'",
            (report.run_id,),
        ).fetchone()
        self.assertEqual(unpaywall_row["requests"], 2)
        self.assertEqual(unpaywall_row["records"], 1)
        self.assertEqual(unpaywall_row["stopped_reason"], "budget_exhausted")

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


class ResolveOaLocationTest(_DbTestCase):
    """resolve_oa_location() — enrich() 와 동일한 오류×캐시 매트릭스를 unpaywall
    에 대해 직접 검증한다(collect() 를 거치지 않고 함수 하나만)."""

    def test_returns_none_without_a_doi(self):
        transport, session = self._transport([])
        self.assertIsNone(pipeline.resolve_oa_location(self.conn, transport, None))
        self.assertIsNone(pipeline.resolve_oa_location(self.conn, transport, ""))
        self.assertEqual(session.calls, [])

    def test_returns_the_record_and_caches_it_on_success(self):
        transport, _ = self._transport([_json_response(UNPAYWALL_PAYLOAD)])
        record = pipeline.resolve_oa_location(self.conn, transport, "10.1/a")
        self.assertTrue(record.is_oa)
        self.assertEqual(record.pdf_url, "https://example.org/article.pdf")

        # 재실행: 세션에 응답을 하나도 안 주지만 캐시로 처리된다.
        transport2, session2 = self._transport([])
        record2 = pipeline.resolve_oa_location(self.conn, transport2, "10.1/a")
        self.assertEqual(session2.calls, [])
        self.assertEqual(record2, record)

    def test_a_not_found_response_is_cached_as_a_confirmed_absence(self):
        transport, _ = self._transport([FakeResponse(404)])
        with quiet():
            result = pipeline.resolve_oa_location(self.conn, transport, "10.1/absent")
        self.assertIsNone(result)
        cached = cache.get(self.conn, "unpaywall", "10.1/absent")
        self.assertIsNone(cached)  # MISS 가 아니라 저장된 "없음"

        transport2, session2 = self._transport([])
        result2 = pipeline.resolve_oa_location(self.conn, transport2, "10.1/absent")
        self.assertEqual(session2.calls, [])
        self.assertIsNone(result2)

    def test_a_transient_error_is_not_cached_and_is_counted(self):
        max_attempts = unpaywall.Unpaywall.policy.max_attempts
        error_counts: dict[str, int] = {}
        transport, _ = self._transport([FakeResponse(503) for _ in range(max_attempts)])
        with quiet():
            result = pipeline.resolve_oa_location(self.conn, transport, "10.1/x", error_counts)
        self.assertIsNone(result)
        self.assertEqual(error_counts["unpaywall"], 1)
        self.assertIs(cache.get(self.conn, "unpaywall", "10.1/x"), cache.MISS)

    def test_budget_exhausted_propagates_with_the_offending_source_attached(self):
        transport, _ = self._transport([FakeResponse(402)])
        with self.assertRaises(BudgetExhausted) as ctx:
            pipeline.resolve_oa_location(self.conn, transport, "10.1/x")
        self.assertEqual(ctx.exception.source, "unpaywall")
        self.assertIs(cache.get(self.conn, "unpaywall", "10.1/x"), cache.MISS)


class ScoreInvarianceTest(_DbTestCase):
    """핵심 회귀 방지: confidence_score(및 evidence/found_in_sources)는
    unpaywall 조회가 성공하든, 그 논문의 DOI 를 unpaywall 이 모른다고
    답하든(404) 완전히 동일해야 한다 — unpaywall 은 서지 소스가 아니라
    부가 정보이므로 점수 산출에 관여해서는 안 된다."""

    def _collect_verification(self, conn, unpaywall_response):
        responses = [
            _search_page([WORK]),
            _json_response(S2_PAYLOAD),
            _json_response(EPMC_PAYLOAD),
            *_pubmed_responses(),
            _json_response(CROSSREF_PAYLOAD),
            unpaywall_response,
        ]
        transport, _ = self._transport(responses)
        with quiet():
            report = pipeline.collect(conn, transport, "cosmetic", 2016, 2026, 10)
        return report.records[0]["verification"]

    def _second_connection(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(path)
        conn = repository.connect(path)
        self.addCleanup(conn.close)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return conn

    def test_confidence_score_is_identical_whether_unpaywall_succeeds_or_404s(self):
        with_success = self._collect_verification(self.conn, _json_response(UNPAYWALL_PAYLOAD))

        not_found_conn = self._second_connection()
        with_not_found = self._collect_verification(not_found_conn, FakeResponse(404))

        self.assertEqual(with_success["confidence_score"], with_not_found["confidence_score"])
        self.assertEqual(with_success["found_in_sources"], with_not_found["found_in_sources"])
        self.assertEqual(with_success["evidence"], with_not_found["evidence"])
        # evidence/found_in_sources 어디에도 unpaywall 이 없다는 것 자체가
        # 이 무관성의 구조적 이유다.
        self.assertNotIn("unpaywall", with_success["evidence"])
        self.assertNotIn("unpaywall", with_success["found_in_sources"])


if __name__ == "__main__":
    unittest.main()
