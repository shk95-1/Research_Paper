"""실제 API 를 때리는 스모크 테스트 — 6개 소스(T10 의 pubmed 포함)의 실제 응답 형태 드리프트 감지용.

papers/tests/live_smoke.py(T7 에서 제거된 구 버전)를 paper_radar.sources /
paper_radar.transport 계약 기준으로 이식했다. 파일이 tool/ 아래 있고
파일명도 test*.py 가 아니므로 `uv run pytest`(testpaths=tests/)는 이 파일을
전혀 건드리지 않는다 — 기본 테스트 스위트는 네트워크를 쓰지 않는다는
계획의 제약을 지키기 위한 배치다. 이 파일을 직접 실행할 때만 실제
네트워크를 쓴다.

    uv run python tool/live_smoke.py

단위 테스트(tests/paper_radar/test_source_*.py)의 픽스처는 특정 시점에
고정해 둔 실제 응답이다. 업스트림 API 스키마가 조용히 바뀌어도 그 픽스처가
그대로 통과시켜 버리므로, 그 간극(픽스처와 실제 응답 사이)을 메우는 것이
이 스크립트의 역할이다 — 실패하면 API 응답 형태가 바뀐 것이니 픽스처를
갱신해야 한다.
"""

from __future__ import annotations

import sys
import unittest

from dotenv import load_dotenv

from paper_radar.sources import crossref, europepmc, openalex, pubmed, semantic_scholar, unpaywall
from paper_radar.transport.errors import NotFound
from paper_radar.transport.http import Transport

# 2016년 논문. 인용수가 많아 네 소스 모두에 색인되어 있다.
KNOWN_DOI = "10.1016/j.yrtph.2017.05.017"

# Wakefield 외(1998, Lancet) — MMR 백신-자폐 연관성을 주장한 논문으로, 2010년
# 공식 철회됐다(가장 널리 알려진 철회 사례 중 하나라 드리프트 감지용으로
# 안정적이다). T9(Crossref 철회 신호 파싱) 스모크용.
KNOWN_RETRACTED_DOI = "10.1016/S0140-6736(97)11096-0"


class LiveSmokeTest(unittest.TestCase):
    """실패하면 API 응답 형태가 바뀐 것이다. 픽스처를 갱신해야 한다."""

    def setUp(self):
        self.transport = Transport()

    def test_openalex_still_returns_the_fields_we_read(self):
        records = openalex.search(self.transport, "cosmetic", 2016, 2026, limit=5)
        self.assertTrue(records, "OpenAlex 검색이 비었습니다")
        self.assertTrue(any(r["title"] for r in records), "title 이 전부 비었습니다")
        self.assertTrue(any(r["doi"] for r in records), "doi 가 전부 비었습니다")
        self.assertTrue(any(r["year"] for r in records), "publication_year 가 전부 비었습니다")

    def test_openalex_still_serves_inverted_abstracts(self):
        records = openalex.search(self.transport, "cosmetic retinol", 2016, 2026, limit=20)
        with_abstract = [r for r in records if r["abstract"]]
        self.assertTrue(
            with_abstract,
            "abstract_inverted_index 가 전부 비었습니다. 복원 로직이나 select 를 확인하세요",
        )

    def test_openalex_trend_still_groups_by_year(self):
        counts = openalex.trend(self.transport, "cosmetic", 2016, 2026)
        self.assertGreater(len(counts), 1, "연도 그룹이 1개 이하입니다. per-page 를 확인하세요")
        self.assertTrue(all(2016 <= year <= 2026 for year, _ in counts))

    def test_crossref_still_wraps_titles_in_a_list(self):
        result = crossref.fetch(self.transport, doi=KNOWN_DOI)
        self.assertIsNotNone(result, "Crossref 조회가 실패했습니다")
        self.assertTrue(result["title"], "title 을 꺼내지 못했습니다")
        self.assertTrue(result["journal"], "container-title 을 꺼내지 못했습니다")

    def test_crossref_still_404s_on_an_unknown_doi(self):
        # 새 계약에서는 404 가 None 이 아니라 NotFound 로 전파된다 — 구
        # papers/http.py 는 이를 None 으로 흡수했지만(T5a 에서 "오류는
        # 타입이다"로 바뀐 결정), 그 값이 실제로도 여전히 404 인지는
        # 이 스모크가 계속 확인한다.
        with self.assertRaises(NotFound):
            crossref.fetch(self.transport, doi="10.9999/definitely-not-a-real-doi")

    def test_crossref_still_reports_a_known_retraction(self):
        # 이 DOI 는 실제로 철회됐다 — relation["is-retracted-by"] 경로가
        # 여전히 이 형태로 오는지(파싱 로직의 드리프트 감지) 확인한다.
        result = crossref.fetch(self.transport, doi=KNOWN_RETRACTED_DOI)
        self.assertIsNotNone(result, "Crossref 조회가 실패했습니다")
        self.assertTrue(result["retractions"], "알려진 철회 DOI인데 retractions 가 비었습니다")
        self.assertEqual(result["retractions"][0]["update_type"], "retraction")

    def test_semantic_scholar_still_nests_tldr_text(self):
        try:
            result = semantic_scholar.fetch(self.transport, doi=KNOWN_DOI)
        except NotFound:
            self.skipTest("Semantic Scholar 가 이 DOI 를 404 로 답했습니다")
        if result is None:
            self.skipTest("Semantic Scholar 가 응답하지 않았습니다 (키 없이 429 잦음)")
        self.assertTrue(result["title"], "title 을 꺼내지 못했습니다")
        self.assertIsNotNone(result["citation_count"], "citationCount 를 꺼내지 못했습니다")

    def test_europepmc_still_serves_abstracts_under_core(self):
        result = europepmc.fetch(self.transport, doi=KNOWN_DOI)
        self.assertIsNotNone(result, "Europe PMC 조회가 실패했습니다")
        self.assertTrue(
            result["abstract"], "abstractText 가 비었습니다. resultType=core 를 확인하세요"
        )

    def test_europepmc_still_finds_a_paper_by_title_alone(self):
        # DOI 없는 논문이 점수를 받을 수 있는 유일한 경로다
        result = europepmc.fetch(
            self.transport,
            doi=None,
            title="Integrating habits and practices data for soaps, cosmetics and air"
            " care products into an existing aggregate exposure model",
        )
        self.assertIsNotNone(result, "제목 검색이 실패했습니다")

    def test_pubmed_still_resolves_a_doi_to_a_pmid_with_mesh_terms(self):
        # esearch [DOI] -> efetch 두 요청이 이 한 번의 호출 안에서 순차로
        # 일어난다(pubmed.py 모듈 docstring 참고).
        try:
            result = pubmed.fetch(self.transport, doi=KNOWN_DOI)
        except NotFound:
            self.skipTest("PubMed 가 이 DOI 조회를 404 로 답했습니다")
        if result is None:
            self.skipTest("PubMed 가 이 DOI 를 색인하지 않았습니다(esearch 0건)")
        self.assertTrue(result["title"], "title 을 꺼내지 못했습니다")
        self.assertIsInstance(
            result["mesh_terms"], tuple, "mesh_terms 는 항상 tuple 이어야 합니다"
        )

    def test_pubmed_still_reports_no_pmids_for_an_unknown_doi(self):
        # 0건은 오류가 아니라 정상적인 부재다 — esearch 가 빈 idlist 를 준다.
        pmids = pubmed.search_pmids(self.transport, "10.9999/definitely-not-a-real-doi[DOI]")
        self.assertEqual(pmids, [])

    def test_unpaywall_still_returns_a_record_shaped_response(self):
        # is_oa 가 true 든 false 든(엠바고·정책은 시간에 따라 바뀐다) 최소한
        # 응답 형태(필드 존재)는 안정적이어야 한다 — is_oa 값 자체는 확언하지
        # 않는다.
        record = unpaywall.fetch(self.transport, doi=KNOWN_DOI)
        self.assertIsNotNone(record, "Unpaywall 조회가 실패했습니다")
        self.assertEqual(record.doi, KNOWN_DOI.lower())
        self.assertIsInstance(record.is_oa, bool)
        self.assertTrue(record.oa_status, "oa_status 가 비었습니다")
        if record.is_oa:
            self.assertIsNotNone(record.landing_url, "is_oa=true 인데 landing_url 이 없습니다")

    def test_unpaywall_still_404s_on_an_unknown_doi(self):
        # 새 계약에서는 "그 DOI 를 모른다"가 404(NotFound)로 전파된다 —
        # is_oa: false(정상 200, 논문은 알지만 OA 가 아님)와는 다른 신호다.
        with self.assertRaises(NotFound):
            unpaywall.fetch(self.transport, doi="10.9999/definitely-not-a-real-doi")


if __name__ == "__main__":
    load_dotenv()
    print("실제 API 를 호출합니다. Semantic Scholar 때문에 몇십 초 걸릴 수 있습니다.\n")
    result = unittest.main(argv=[sys.argv[0], "-v"], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
