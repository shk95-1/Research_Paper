"""evidence.verify 모듈: 제목 유사도와 배점.

배점은 스펙 7절에 못박혀 있다. 기본 5 + Crossref 30 + 제목일치 25 +
추가소스 15/개(최대 30) + 초록 10 = 100. 철회면 총점 0.

papers/tests/test_verify.py 의 모든 케이스를 새 시그니처(evidence dict)로
이식했다. crossref= 명명 파라미터 대신 evidence={"crossref": {...}} 를 쓴다.
"""

import unittest

from paper_radar.evidence import verify


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


class CrossrefRetractionCrossCheckTest(unittest.TestCase):
    """T9: OpenAlex.is_retracted 와 Crossref.retractions(evidence["crossref"]
    안에 실려 온다) 중 어느 쪽이든 철회를 가리키면 철회로 판정해야 한다.
    4분면(둘 다 아님 / OpenAlex 만 / Crossref 만 / 둘 다) 전부를 확인한다."""

    _RETRACTION = (
        {
            "role": "retracted",
            "retraction_doi": "10.1/notice",
            "update_type": "retraction",
            "update_date": None,
        },
    )
    # role="notice" — 이 레코드 자신이 철회 공지 문서다(update-to 경로).
    # 공지는 철회'된' 논문이 아니라 철회를 알리는 문서이므로 점수를 0 으로
    # 만들면 안 된다(리뷰 Important 대응 — 수정 전에는 이것도 0점 처리했다).
    _NOTICE_ONLY = (
        {
            "role": "notice",
            "retraction_doi": "10.1/original-paper",
            "update_type": "retraction",
            "update_date": "2024-03-15",
        },
    )

    def _build(self, *, openalex_retracted, crossref_retractions):
        evidence = {
            "crossref": {
                "title": "Retinol and the skin barrier",
                "retractions": crossref_retractions,
            }
        }
        return verify.build(
            record(is_retracted=openalex_retracted),
            found_in_sources=["openalex", "semantic_scholar", "europepmc"],
            evidence=evidence,
        )

    def test_neither_source_flags_a_retraction(self):
        result = self._build(openalex_retracted=False, crossref_retractions=())
        self.assertFalse(result["is_retracted"])
        self.assertEqual(result["confidence_score"], 100)

    def test_openalex_alone_flags_a_retraction(self):
        result = self._build(openalex_retracted=True, crossref_retractions=())
        self.assertTrue(result["is_retracted"])
        self.assertEqual(result["confidence_score"], 0)

    def test_crossref_alone_flags_a_retraction(self):
        result = self._build(openalex_retracted=False, crossref_retractions=self._RETRACTION)
        self.assertTrue(result["is_retracted"])
        self.assertEqual(result["confidence_score"], 0)

    def test_both_sources_flag_a_retraction(self):
        result = self._build(openalex_retracted=True, crossref_retractions=self._RETRACTION)
        self.assertTrue(result["is_retracted"])
        self.assertEqual(result["confidence_score"], 0)

    def test_notice_only_retraction_does_not_zero_the_notices_own_score(self):
        """공지 문서 자신을 수집한 경우(role="notice" 뿐) — 이 문서는 철회된
        논문이 아니라 철회를 알리는 문서이므로 점수가 정상이어야 한다."""
        result = self._build(openalex_retracted=False, crossref_retractions=self._NOTICE_ONLY)
        self.assertFalse(result["is_retracted"])
        self.assertEqual(result["confidence_score"], 100)

    def test_a_retracted_role_item_still_zeroes_even_when_mixed_with_a_notice_role_item(self):
        result = self._build(
            openalex_retracted=False, crossref_retractions=self._NOTICE_ONLY + self._RETRACTION
        )
        self.assertTrue(result["is_retracted"])
        self.assertEqual(result["confidence_score"], 0)

    def test_a_retraction_item_without_a_role_key_is_treated_as_retracted_for_backward_compat(self):
        legacy_item = ({"retraction_doi": "10.1/notice", "update_type": "retraction",
                         "update_date": None},)
        result = self._build(openalex_retracted=False, crossref_retractions=legacy_item)
        self.assertTrue(result["is_retracted"])
        self.assertEqual(result["confidence_score"], 0)

    def test_does_not_add_a_new_verification_key(self):
        """검증 표면 불변 — retractions 는 이미 evidence["crossref"] 안에 있다."""
        result = self._build(openalex_retracted=False, crossref_retractions=self._RETRACTION)
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


class ScoreParityWithLegacyTest(unittest.TestCase):
    """새 verify.build 가 레거시 papers.verify.build 와 동일한 판정을 내리는지
    고정 기대값으로 검증한다.

    레거시 papers/ 가 아직 살아 있던 T5b~T6 기간에는 이 CASES 를 두 구현
    모두에 직접 돌려 confidence_score/crossref_verified/title_match/
    is_retracted/has_doi 가 문자 그대로 같음을 확인했다(commit 54cf083
    시점). T7 이 레거시 papers/ 를 제거하면서 그 살아있는 비교를 더는 할 수
    없어, 그때 확인된 값을 리터럴로 고정한다 — 두 구현을 다시 나란히 놓고
    비교할 수는 없지만, 새 구현이 그 시점 이후로 슬쩍 갈라지면 이 테스트가
    잡는다."""

    CASES = [
        {
            "record": record(),
            "found_in_sources": ["openalex", "semantic_scholar"],
            "crossref": {"title": "Retinol and the skin barrier"},
            "expected": {
                "confidence_score": 85,
                "crossref_verified": True,
                "title_match": True,
                "is_retracted": False,
                "has_doi": True,
            },
        },
        {
            "record": record(),
            "found_in_sources": ["openalex"],
            "crossref": None,
            "expected": {
                "confidence_score": 15,
                "crossref_verified": False,
                "title_match": False,
                "is_retracted": False,
                "has_doi": True,
            },
        },
        {
            "record": record(abstract=None),
            "found_in_sources": ["openalex"],
            "crossref": None,
            "expected": {
                "confidence_score": 5,
                "crossref_verified": False,
                "title_match": False,
                "is_retracted": False,
                "has_doi": True,
            },
        },
        {
            "record": record(),
            "found_in_sources": ["openalex", "semantic_scholar", "europepmc"],
            "crossref": {"title": "Retinol and the skin barrier"},
            "expected": {
                "confidence_score": 100,
                "crossref_verified": True,
                "title_match": True,
                "is_retracted": False,
                "has_doi": True,
            },
        },
        {
            "record": record(),
            "found_in_sources": ["openalex", "semantic_scholar"],
            "crossref": {"title": "Something else entirely about sunscreen"},
            "expected": {
                "confidence_score": 60,  # 85 - 25 (제목 불일치로 title_match 점수 상실)
                "crossref_verified": True,
                "title_match": False,
                "is_retracted": False,
                "has_doi": True,
            },
        },
        {
            "record": record(is_retracted=True),
            "found_in_sources": ["openalex", "semantic_scholar", "europepmc"],
            "crossref": {"title": "Retinol and the skin barrier"},
            "expected": {
                "confidence_score": 0,
                "crossref_verified": True,
                "title_match": True,
                "is_retracted": True,
                "has_doi": True,
            },
        },
        {
            "record": record(doi=None),
            "found_in_sources": ["openalex", "europepmc"],
            "crossref": None,
            "expected": {
                "confidence_score": 30,  # 5 + 15 + 10
                "crossref_verified": False,
                "title_match": False,
                "is_retracted": False,
                "has_doi": False,
            },
        },
        {
            "record": record(),
            "found_in_sources": ["openalex"],
            "crossref": {},
            "expected": {
                "confidence_score": 45,  # 5 + 30 + 10
                "crossref_verified": True,
                "title_match": False,
                "is_retracted": False,
                "has_doi": True,
            },
        },
        {
            # T10 — pubmed 참여 케이스(브리핑 지시): 위 케이스들은 전부
            # pubmed 없는 입력이라 불변으로 남겨두고, 이 케이스 하나만 새로
            # 추가해 pubmed 도 "추가 소스"로서 상한(30) 계산에 들어가는지
            # 확인한다. openalex + 3개 소스(semantic_scholar/europepmc/
            # pubmed) = 45점 자격이지만 SCORE_EXTRA_SOURCE_CAP=30 에 막혀
            # 100 을 넘지 않는다(5 + 30 + 25 + 30 + 10 = 100, 애초에 100
            # 상한과 겹쳐 100 이 나온다 — cap 이 실제로 개입했는지는 아래
            # ScoreInvarianceTest 류가 아니라 evidence.pipeline 쪽 전용
            # 테스트가 별도로 확인한다).
            "record": record(),
            "found_in_sources": ["openalex", "semantic_scholar", "europepmc", "pubmed"],
            "crossref": {"title": "Retinol and the skin barrier"},
            "expected": {
                "confidence_score": 100,
                "crossref_verified": True,
                "title_match": True,
                "is_retracted": False,
                "has_doi": True,
            },
        },
    ]

    def test_confidence_score_matches_the_fixed_legacy_baseline_for_every_case(self):
        for case in self.CASES:
            with self.subTest(case=case):
                evidence = {"crossref": case["crossref"]} if case["crossref"] is not None else None
                new_result = verify.build(
                    case["record"],
                    found_in_sources=case["found_in_sources"],
                    evidence=evidence,
                )
                for key, expected_value in case["expected"].items():
                    self.assertEqual(new_result[key], expected_value, key)


if __name__ == "__main__":
    unittest.main()
