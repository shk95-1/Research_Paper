"""evidence.verify 모듈: 제목 유사도와 배점.

배점은 스펙 7절에 못박혀 있다. 기본 5 + Crossref 30 + 제목일치 25 +
추가소스 15/개(최대 30) + 초록 10 = 100. 철회면 총점 0.

papers/tests/test_verify.py 의 모든 케이스를 새 시그니처(evidence dict)로
이식했다. crossref= 명명 파라미터 대신 evidence={"crossref": {...}} 를 쓴다.
"""

import unittest

from paper_radar.evidence import verify
from papers import verify as legacy_verify


def record(**overrides):
    base = {
        "title": "Retinol and the skin barrier",
        "doi": "10.1/a",
        "abstract": "Retinol improves the barrier.",
        "is_retracted": False,
    }
    base.update(overrides)
    return base


class NormalizeTitleTest(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(verify.normalize_title("Retinol"), "retinol")

    def test_collapses_whitespace(self):
        self.assertEqual(verify.normalize_title("a   b\n c"), "a b c")

    def test_drops_punctuation(self):
        self.assertEqual(verify.normalize_title("Retinol: a review!"), "retinol a review")

    def test_returns_empty_string_for_absent_input(self):
        self.assertEqual(verify.normalize_title(None), "")
        self.assertEqual(verify.normalize_title(""), "")


class TitleSimilarityTest(unittest.TestCase):
    def test_identical_titles_score_one(self):
        self.assertEqual(verify.title_similarity("Retinol", "Retinol"), 1.0)

    def test_ignores_case_punctuation_and_spacing(self):
        score = verify.title_similarity(
            "Retinol and the Skin Barrier", "retinol   and the skin barrier."
        )
        self.assertEqual(score, 1.0)

    def test_unrelated_titles_score_below_the_threshold(self):
        score = verify.title_similarity(
            "Retinol and the skin barrier", "Sunscreen photostability in humid climates"
        )
        self.assertLess(score, verify.TITLE_MATCH_THRESHOLD)

    def test_returns_zero_when_either_title_is_missing(self):
        self.assertEqual(verify.title_similarity(None, "Retinol"), 0.0)
        self.assertEqual(verify.title_similarity("Retinol", None), 0.0)


class BuildTest(unittest.TestCase):
    def test_the_spec_example_scores_eighty_five(self):
        # 스펙 7절의 예시 레코드: 2개 소스 + Crossref + 제목일치 + 초록
        result = verify.build(
            record(),
            found_in_sources=["openalex", "semantic_scholar"],
            evidence={"crossref": {"title": "Retinol and the skin barrier"}},
        )
        self.assertEqual(result["confidence_score"], 85)
        self.assertTrue(result["crossref_verified"])
        self.assertTrue(result["title_match"])
        self.assertEqual(result["found_in_sources"], ["openalex", "semantic_scholar"])
        self.assertFalse(result["is_retracted"])
        self.assertTrue(result["has_doi"])

    def test_openalex_only_with_an_abstract_scores_fifteen(self):
        result = verify.build(record(), found_in_sources=["openalex"], evidence=None)
        self.assertEqual(result["confidence_score"], 15)
        self.assertFalse(result["crossref_verified"])

    def test_openalex_only_without_an_abstract_scores_five(self):
        result = verify.build(record(abstract=None), found_in_sources=["openalex"], evidence=None)
        self.assertEqual(result["confidence_score"], 5)

    def test_all_three_sources_plus_crossref_scores_one_hundred(self):
        result = verify.build(
            record(),
            found_in_sources=["openalex", "semantic_scholar", "europepmc"],
            evidence={"crossref": {"title": "Retinol and the skin barrier"}},
        )
        self.assertEqual(result["confidence_score"], 100)

    def test_extra_source_credit_is_capped_at_thirty(self):
        result = verify.build(
            record(),
            found_in_sources=["openalex", "semantic_scholar", "europepmc", "extra"],
            evidence={"crossref": {"title": "Retinol and the skin barrier"}},
        )
        self.assertEqual(result["confidence_score"], 100)

    def test_a_mismatched_crossref_title_forfeits_the_title_match_points(self):
        result = verify.build(
            record(),
            found_in_sources=["openalex", "semantic_scholar"],
            evidence={"crossref": {"title": "Something else entirely about sunscreen"}},
        )
        self.assertTrue(result["crossref_verified"])
        self.assertFalse(result["title_match"])
        self.assertEqual(result["confidence_score"], 60)  # 85 - 25

    def test_retraction_zeroes_an_otherwise_perfect_score(self):
        result = verify.build(
            record(is_retracted=True),
            found_in_sources=["openalex", "semantic_scholar", "europepmc"],
            evidence={"crossref": {"title": "Retinol and the skin barrier"}},
        )
        self.assertEqual(result["confidence_score"], 0)
        self.assertTrue(result["is_retracted"])

    def test_a_doi_less_paper_is_marked_rather_than_dropped(self):
        result = verify.build(
            record(doi=None), found_in_sources=["openalex", "europepmc"], evidence=None
        )
        self.assertFalse(result["has_doi"])
        self.assertEqual(result["confidence_score"], 30)  # 5 + 15 + 10

    def test_defaults_to_openalex_when_no_source_list_is_given(self):
        result = verify.build(record(), found_in_sources=None, evidence=None)
        self.assertEqual(result["found_in_sources"], ["openalex"])

    def test_never_exceeds_one_hundred(self):
        result = verify.build(
            record(),
            found_in_sources=["openalex"] + [f"s{i}" for i in range(10)],
            evidence={"crossref": {"title": "Retinol and the skin barrier"}},
        )
        self.assertLessEqual(result["confidence_score"], 100)

    def test_crossref_without_a_title_still_counts_as_verified(self):
        result = verify.build(record(), found_in_sources=["openalex"], evidence={"crossref": {}})
        self.assertTrue(result["crossref_verified"])
        self.assertFalse(result["title_match"])
        self.assertEqual(result["confidence_score"], 45)  # 5 + 30 + 10

    def test_no_crossref_entry_means_unverified(self):
        """evidence 에 "crossref" 키 자체가 없으면(다른 소스만 있어도) 미검증이다
        — 값의 진위가 아니라 키의 존재로 판정한다."""
        result = verify.build(
            record(),
            found_in_sources=["openalex", "semantic_scholar"],
            evidence={"semantic_scholar": {"title": "Retinol and the skin barrier"}},
        )
        self.assertFalse(result["crossref_verified"])
        self.assertFalse(result["title_match"])

    def test_emits_the_spec_verification_keys_plus_evidence(self):
        """스펙 확장: 기존 6키(has_doi/crossref_verified/title_match/
        found_in_sources/is_retracted/confidence_score) 에 evidence 가
        추가된 7키다. T4 의 upsert() 가 verification["evidence"] 를 evidence
        컬럼에 저장하려면 verify.build() 가 이 키를 내보내야 한다 — 근거
        원문을 점수와 함께 보존해 사후 추적이 가능하게 하려는 스펙 확장이다."""
        result = verify.build(record(), found_in_sources=["openalex"], evidence=None)
        self.assertEqual(
            sorted(result),
            [
                "confidence_score",
                "crossref_verified",
                "evidence",
                "found_in_sources",
                "has_doi",
                "is_retracted",
                "title_match",
            ],
        )

    def test_evidence_defaults_to_an_empty_dict(self):
        result = verify.build(record(), found_in_sources=["openalex"], evidence=None)
        self.assertEqual(result["evidence"], {})

    def test_evidence_carries_every_successful_sources_raw_response(self):
        evidence = {
            "semantic_scholar": {"tldr": "helps"},
            "europepmc": {"abstract": "..."},
            "crossref": {"title": "Retinol and the skin barrier"},
        }
        result = verify.build(
            record(),
            found_in_sources=["openalex", "semantic_scholar", "europepmc"],
            evidence=evidence,
        )
        self.assertEqual(result["evidence"], evidence)

    def test_does_not_mutate_the_source_list_it_was_given(self):
        sources = ["openalex"]
        verify.build(record(), found_in_sources=sources, evidence=None)
        self.assertEqual(sources, ["openalex"])

    def test_does_not_mutate_the_evidence_dict_it_was_given(self):
        evidence = {"crossref": {"title": "Retinol and the skin barrier"}}
        result = verify.build(record(), found_in_sources=["openalex"], evidence=evidence)
        result["evidence"]["semantic_scholar"] = {"injected": True}
        self.assertEqual(evidence, {"crossref": {"title": "Retinol and the skin barrier"}})


class ScoreParityWithLegacyTest(unittest.TestCase):
    """레거시 papers.verify.build 와 새 verify.build 가 동일 입력에 같은
    confidence_score 를 내는지 직접 비교한다. papers/ 가 살아있는 동안(T7
    이전)만 가능한 검증이라 지금 고정해 둔다 — 두 구현이 슬쩍 갈라져도 이
    테스트가 즉시 잡는다."""

    CASES = [
        {
            "record": record(),
            "found_in_sources": ["openalex", "semantic_scholar"],
            "crossref": {"title": "Retinol and the skin barrier"},
        },
        {
            "record": record(),
            "found_in_sources": ["openalex"],
            "crossref": None,
        },
        {
            "record": record(abstract=None),
            "found_in_sources": ["openalex"],
            "crossref": None,
        },
        {
            "record": record(),
            "found_in_sources": ["openalex", "semantic_scholar", "europepmc"],
            "crossref": {"title": "Retinol and the skin barrier"},
        },
        {
            "record": record(),
            "found_in_sources": ["openalex", "semantic_scholar"],
            "crossref": {"title": "Something else entirely about sunscreen"},
        },
        {
            "record": record(is_retracted=True),
            "found_in_sources": ["openalex", "semantic_scholar", "europepmc"],
            "crossref": {"title": "Retinol and the skin barrier"},
        },
        {
            "record": record(doi=None),
            "found_in_sources": ["openalex", "europepmc"],
            "crossref": None,
        },
        {
            "record": record(),
            "found_in_sources": ["openalex"],
            "crossref": {},
        },
    ]

    def test_confidence_score_matches_the_legacy_implementation_for_every_case(self):
        for case in self.CASES:
            with self.subTest(case=case):
                legacy_result = legacy_verify.build(
                    case["record"],
                    crossref=case["crossref"],
                    found_in_sources=case["found_in_sources"],
                )
                evidence = {"crossref": case["crossref"]} if case["crossref"] is not None else None
                new_result = verify.build(
                    case["record"],
                    found_in_sources=case["found_in_sources"],
                    evidence=evidence,
                )
                self.assertEqual(
                    new_result["confidence_score"], legacy_result["confidence_score"]
                )
                self.assertEqual(
                    new_result["crossref_verified"], legacy_result["crossref_verified"]
                )
                self.assertEqual(new_result["title_match"], legacy_result["title_match"])
                self.assertEqual(new_result["is_retracted"], legacy_result["is_retracted"])
                self.assertEqual(new_result["has_doi"], legacy_result["has_doi"])


if __name__ == "__main__":
    unittest.main()
