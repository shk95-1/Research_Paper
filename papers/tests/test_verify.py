"""verify 모듈: 제목 유사도와 배점.

배점은 스펙 7절에 못박혀 있다. 기본 5 + Crossref 30 + 제목일치 25 +
추가소스 15/개(최대 30) + 초록 10 = 100. 철회면 총점 0.
"""

import unittest

from papers import verify


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
            crossref={"title": "Retinol and the skin barrier"},
            found_in_sources=["openalex", "semantic_scholar"],
        )
        self.assertEqual(result["confidence_score"], 85)
        self.assertTrue(result["crossref_verified"])
        self.assertTrue(result["title_match"])
        self.assertEqual(result["found_in_sources"], ["openalex", "semantic_scholar"])
        self.assertFalse(result["is_retracted"])
        self.assertTrue(result["has_doi"])

    def test_openalex_only_with_an_abstract_scores_fifteen(self):
        result = verify.build(record(), crossref=None, found_in_sources=["openalex"])
        self.assertEqual(result["confidence_score"], 15)
        self.assertFalse(result["crossref_verified"])

    def test_openalex_only_without_an_abstract_scores_five(self):
        result = verify.build(record(abstract=None), crossref=None, found_in_sources=["openalex"])
        self.assertEqual(result["confidence_score"], 5)

    def test_all_three_sources_plus_crossref_scores_one_hundred(self):
        result = verify.build(
            record(),
            crossref={"title": "Retinol and the skin barrier"},
            found_in_sources=["openalex", "semantic_scholar", "europepmc"],
        )
        self.assertEqual(result["confidence_score"], 100)

    def test_extra_source_credit_is_capped_at_thirty(self):
        result = verify.build(
            record(),
            crossref={"title": "Retinol and the skin barrier"},
            found_in_sources=["openalex", "semantic_scholar", "europepmc", "extra"],
        )
        self.assertEqual(result["confidence_score"], 100)

    def test_a_mismatched_crossref_title_forfeits_the_title_match_points(self):
        result = verify.build(
            record(),
            crossref={"title": "Something else entirely about sunscreen"},
            found_in_sources=["openalex", "semantic_scholar"],
        )
        self.assertTrue(result["crossref_verified"])
        self.assertFalse(result["title_match"])
        self.assertEqual(result["confidence_score"], 60)  # 85 - 25

    def test_retraction_zeroes_an_otherwise_perfect_score(self):
        result = verify.build(
            record(is_retracted=True),
            crossref={"title": "Retinol and the skin barrier"},
            found_in_sources=["openalex", "semantic_scholar", "europepmc"],
        )
        self.assertEqual(result["confidence_score"], 0)
        self.assertTrue(result["is_retracted"])

    def test_a_doi_less_paper_is_marked_rather_than_dropped(self):
        result = verify.build(
            record(doi=None), crossref=None, found_in_sources=["openalex", "europepmc"]
        )
        self.assertFalse(result["has_doi"])
        self.assertEqual(result["confidence_score"], 30)  # 5 + 15 + 10

    def test_defaults_to_openalex_when_no_source_list_is_given(self):
        result = verify.build(record(), crossref=None, found_in_sources=None)
        self.assertEqual(result["found_in_sources"], ["openalex"])

    def test_never_exceeds_one_hundred(self):
        result = verify.build(
            record(),
            crossref={"title": "Retinol and the skin barrier"},
            found_in_sources=["openalex"] + [f"s{i}" for i in range(10)],
        )
        self.assertLessEqual(result["confidence_score"], 100)

    def test_crossref_without_a_title_still_counts_as_verified(self):
        result = verify.build(record(), crossref={}, found_in_sources=["openalex"])
        self.assertTrue(result["crossref_verified"])
        self.assertFalse(result["title_match"])
        self.assertEqual(result["confidence_score"], 45)  # 5 + 30 + 10

    def test_emits_exactly_the_spec_verification_keys(self):
        result = verify.build(record(), crossref=None, found_in_sources=["openalex"])
        self.assertEqual(
            sorted(result),
            [
                "confidence_score",
                "crossref_verified",
                "found_in_sources",
                "has_doi",
                "is_retracted",
                "title_match",
            ],
        )

    def test_does_not_mutate_the_source_list_it_was_given(self):
        sources = ["openalex"]
        verify.build(record(), crossref=None, found_in_sources=sources)
        self.assertEqual(sources, ["openalex"])


if __name__ == "__main__":
    unittest.main()
