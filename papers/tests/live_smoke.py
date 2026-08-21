"""실제 API 를 때리는 스모크 테스트.

파일명이 test*.py 가 아니므로 `unittest discover` 의 기본 패턴에 걸리지 않는다.
기본 테스트 실행은 네트워크를 쓰지 않는다. 이 파일만 직접 실행할 때 나간다.

    python -m papers.tests.live_smoke

API 스키마가 조용히 바뀌었는지 확인하는 용도다. 단위 테스트의 픽스처는
2026-08-19 응답을 고정해 둔 것이므로, 실제 응답이 달라져도 단위 테스트는
계속 통과한다. 그 간극을 메우는 것이 이 파일의 역할이다.
"""

import sys
import unittest

from dotenv import load_dotenv

from papers.sources import crossref, europepmc, openalex, semantic_scholar

# 2016년 논문. 인용수가 많아 네 소스 모두에 색인되어 있다.
KNOWN_DOI = "10.1016/j.yrtph.2017.05.017"


class LiveSmokeTest(unittest.TestCase):
    """실패하면 API 응답 형태가 바뀐 것이다. 픽스처를 갱신해야 한다."""

    def test_openalex_still_returns_the_fields_we_read(self):
        records = openalex.search("cosmetic", 2016, 2026, limit=5)
        self.assertTrue(records, "OpenAlex 검색이 비었습니다")
        self.assertTrue(any(r["title"] for r in records), "title 이 전부 비었습니다")
        self.assertTrue(any(r["doi"] for r in records), "doi 가 전부 비었습니다")
        self.assertTrue(any(r["year"] for r in records), "publication_year 가 전부 비었습니다")

    def test_openalex_still_serves_inverted_abstracts(self):
        records = openalex.search("cosmetic retinol", 2016, 2026, limit=20)
        with_abstract = [r for r in records if r["abstract"]]
        self.assertTrue(
            with_abstract,
            "abstract_inverted_index 가 전부 비었습니다. 복원 로직이나 select 를 확인하세요",
        )

    def test_openalex_trend_still_groups_by_year(self):
        counts = openalex.trend("cosmetic", 2016, 2026)
        self.assertGreater(len(counts), 1, "연도 그룹이 1개 이하입니다. per-page 를 확인하세요")
        self.assertTrue(all(2016 <= year <= 2026 for year, _ in counts))

    def test_crossref_still_wraps_titles_in_a_list(self):
        result = crossref.fetch(doi=KNOWN_DOI)
        self.assertIsNotNone(result, "Crossref 조회가 실패했습니다")
        self.assertTrue(result["title"], "title 을 꺼내지 못했습니다")
        self.assertTrue(result["journal"], "container-title 을 꺼내지 못했습니다")

    def test_crossref_still_404s_on_an_unknown_doi(self):
        self.assertIsNone(crossref.fetch(doi="10.9999/definitely-not-a-real-doi"))

    def test_semantic_scholar_still_nests_tldr_text(self):
        result = semantic_scholar.fetch(doi=KNOWN_DOI)
        if result is None:
            self.skipTest("Semantic Scholar 가 응답하지 않았습니다 (키 없이 429 잦음)")
        self.assertTrue(result["title"], "title 을 꺼내지 못했습니다")
        self.assertIsNotNone(result["citation_count"], "citationCount 를 꺼내지 못했습니다")

    def test_europepmc_still_serves_abstracts_under_core(self):
        result = europepmc.fetch(doi=KNOWN_DOI)
        self.assertIsNotNone(result, "Europe PMC 조회가 실패했습니다")
        self.assertTrue(
            result["abstract"], "abstractText 가 비었습니다. resultType=core 를 확인하세요"
        )

    def test_europepmc_still_finds_a_paper_by_title_alone(self):
        # DOI 없는 논문이 점수를 받을 수 있는 유일한 경로다
        result = europepmc.fetch(
            doi=None,
            title="Integrating habits and practices data for soaps, cosmetics and air"
            " care products into an existing aggregate exposure model",
        )
        self.assertIsNotNone(result, "제목 검색이 실패했습니다")


if __name__ == "__main__":
    load_dotenv()
    print("실제 API 를 호출합니다. Semantic Scholar 때문에 몇십 초 걸릴 수 있습니다.\n")
    result = unittest.main(argv=[sys.argv[0], "-v"], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
