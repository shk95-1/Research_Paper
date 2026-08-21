"""papers_trend 순수 함수 테스트. 네트워크를 쓰지 않는다.

    python -m unittest discover -s papers_trend/tests -t .

가장 위험한 것부터 덮는다. 명세가 지목한 조용한 오류 세 개다.
  1. 코호트 백분위의 계산 순서 (논문 단위가 먼저)
  2. 정규화 과잉 병합 (niacinamide != nicotinamide)
  3. 임계값 없는 growth_ratio (3편 -> 9편이 300%)
"""

import unittest

from papers_trend import aggregate, normalize, records, weight


class ParseDateTest(unittest.TestCase):
    def test_full_date_gives_day_precision(self):
        self.assertEqual(records.parse_date("2024-04-14"), ("2024-04", "day", 2024))

    def test_year_month_gives_month_precision(self):
        self.assertEqual(records.parse_date("2024-04"), ("2024-04", "month", 2024))

    def test_year_only_is_kept_without_a_month_bucket(self):
        # 버리지 않는다. 월별 집계에서만 빠진다.
        self.assertEqual(records.parse_date("2024"), (None, "year_only", 2024))

    def test_missing_date_falls_back_to_publication_year(self):
        self.assertEqual(records.parse_date(None, 2019), (None, "year_only", 2019))

    def test_missing_everything_is_unknown(self):
        self.assertEqual(records.parse_date(None), (None, "unknown", None))

    def test_malformed_date_falls_back_to_the_year_without_raising(self):
        # 13월 99일은 파싱되지 않는다. 연도만 살리고 월별 집계에서 빠진다.
        self.assertEqual(records.parse_date("2024-13-99"), (None, "year_only", 2024))


class BareDoiTest(unittest.TestCase):
    def test_strips_the_resolver_prefix(self):
        self.assertEqual(records.bare_doi("https://doi.org/10.1/A"), "10.1/a")

    def test_returns_none_for_empty(self):
        self.assertIsNone(records.bare_doi(None))


class NormalizeTermTest(unittest.TestCase):
    def test_lowercases_and_collapses_separators(self):
        self.assertEqual(normalize.normalize_term("Anti-Aging"), "anti aging")
        self.assertEqual(normalize.normalize_term("anti_aging"), "anti aging")
        self.assertEqual(normalize.normalize_term("anti   aging"), "anti aging")

    def test_nfkc_folds_chemical_subscripts(self):
        # 4단계 화학식 표기 통일은 NFKC 가 대신한다
        self.assertEqual(normalize.normalize_term("TiO₂"), "tio2")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(normalize.normalize_term("  Zinc Oxide  "), "zinc oxide")

    def test_empty_input_is_empty(self):
        self.assertEqual(normalize.normalize_term(None), "")


class LexiconTest(unittest.TestCase):
    def setUp(self):
        self.entries, self.alias_map = normalize.load_lexicon()
        self.stopwords = normalize.load_stopwords()

    def _resolve(self, term):
        return normalize.resolve(term, self.alias_map, self.entries, self.stopwords)

    def test_chemical_formula_and_name_reach_the_same_key(self):
        self.assertEqual(self._resolve("TiO2")["keyword_key"], "TITANIUM_DIOXIDE")
        self.assertEqual(self._resolve("titanium dioxide")["keyword_key"], "TITANIUM_DIOXIDE")
        self.assertEqual(self._resolve("TiO₂")["keyword_key"], "TITANIUM_DIOXIDE")

    def test_abbreviation_and_expansion_reach_the_same_key(self):
        for term in ("SOD", "sods", "superoxide dismutase", "Cu/Zn-SOD"):
            self.assertEqual(self._resolve(term)["keyword_key"], "SUPEROXIDE_DISMUTASE", term)

    def test_singular_and_plural_reach_the_same_key(self):
        self.assertEqual(self._resolve("antioxidant")["keyword_key"], "ANTIOXIDANT")
        self.assertEqual(self._resolve("antioxidants")["keyword_key"], "ANTIOXIDANT")

    def test_british_and_american_spelling_reach_the_same_key(self):
        self.assertEqual(self._resolve("ageing")["keyword_key"], "ANTI_AGING")
        self.assertEqual(self._resolve("aging")["keyword_key"], "ANTI_AGING")

    def test_hyphen_variants_reach_the_same_key(self):
        for term in ("anti-aging", "anti aging", "antiaging"):
            self.assertEqual(self._resolve(term)["keyword_key"], "ANTI_AGING", term)

    def test_niacinamide_and_nicotinamide_stay_separate(self):
        # 정규화가 과하면 파서는 성공하고 테스트는 초록인데 값만 거짓이 된다
        self.assertEqual(self._resolve("niacinamide")["keyword_key"], "NIACINAMIDE")
        self.assertEqual(self._resolve("nicotinamide")["keyword_key"], "NICOTINAMIDE")

    def test_retinol_and_retinoid_stay_separate(self):
        self.assertEqual(self._resolve("retinol")["keyword_key"], "RETINOL")
        self.assertEqual(self._resolve("retinoids")["keyword_key"], "RETINOID")

    def test_field_labels_are_dropped(self):
        self.assertIsNone(self._resolve("Chemistry"))
        self.assertIsNone(self._resolve("materials science"))

    def test_unmatched_terms_pass_through_rather_than_disappear(self):
        resolved = self._resolve("some novel peptide complex")
        self.assertEqual(resolved["keyword_key"], "some novel peptide complex")
        self.assertFalse(resolved["is_in_lexicon"])

    def test_lexicon_wins_over_stopwords(self):
        # dermatology 는 분야명이지만 일부러 남긴 신호다
        self.assertIsNotNone(self._resolve("dermatology"))

    def test_a_paper_mentioning_two_aliases_counts_the_keyword_once(self):
        # 접지 않으면 paper_count 가 부풀어 prevalence 가 조용히 틀린다
        resolved = normalize.resolve_field(
            ["ZnO", "zinc oxide", "nano-ZnO"],
            self.alias_map,
            self.entries,
            self.stopwords,
        )
        self.assertEqual([item["keyword_key"] for item in resolved], ["ZINC_OXIDE"])

    def test_lexicon_has_no_duplicate_aliases(self):
        # load_lexicon 이 중복 별칭에 예외를 던진다. 여기까지 왔으면 통과다.
        self.assertGreater(len(self.alias_map), len(self.entries))

    def test_every_entry_has_the_korean_bridge_field(self):
        for key, entry in self.entries.items():
            self.assertIn("kr_colloquial", entry, key)
            self.assertIsInstance(entry["kr_colloquial"], list, key)


class CohortPercentileTest(unittest.TestCase):
    def test_midrank_handles_a_single_paper(self):
        self.assertEqual(weight.cohort_percentiles([5]), [0.5])

    def test_ranks_ascending_by_citation_count(self):
        result = weight.cohort_percentiles([0, 10, 100])
        self.assertEqual(result, [1 / 6, 0.5, 5 / 6])

    def test_ties_share_the_same_midrank(self):
        result = weight.cohort_percentiles([7, 7, 7, 7])
        self.assertEqual(result, [0.5, 0.5, 0.5, 0.5])

    def test_many_zeros_do_not_skew_the_survivor(self):
        result = weight.cohort_percentiles([0, 0, 0, 50])
        self.assertEqual(result[3], 7 / 8)
        self.assertEqual(result[0], 3 / 8)

    def test_empty_cohort_is_empty(self):
        self.assertEqual(weight.cohort_percentiles([]), [])


class PaperWeightsTest(unittest.TestCase):
    def _records(self):
        return [
            {
                "month_bucket": "2024-01",
                "citation_count": 100,
                "publication_date": "2024-01-15",
                "collected_at": "2026-08-20T00:00:00Z",
            },
            {
                "month_bucket": "2024-01",
                "citation_count": 0,
                "publication_date": "2024-01-20",
                "collected_at": "2026-08-20T00:00:00Z",
            },
            {
                "month_bucket": "2026-08",
                "citation_count": 3,
                "publication_date": "2026-08-01",
                "collected_at": "2026-08-20T00:00:00Z",
            },
        ]

    def test_percentiles_are_computed_within_each_month_not_across(self):
        # 이 순서를 뒤집으면 코호트가 깨진다. 2026-08 논문 3회가 자기 달에서는
        # 1등이므로, 2024-01 의 100회보다 낮은 백분위를 받아서는 안 된다.
        weighted = weight.assign_paper_weights(self._records())
        self.assertEqual(weighted[0]["citation_percentile_in_cohort"], 0.75)
        self.assertEqual(weighted[1]["citation_percentile_in_cohort"], 0.25)
        self.assertEqual(weighted[2]["citation_percentile_in_cohort"], 0.5)

    def test_raw_citation_count_is_never_overwritten(self):
        weighted = weight.assign_paper_weights(self._records())
        self.assertEqual([r["citation_count"] for r in weighted], [100, 0, 3])

    def test_does_not_mutate_the_input(self):
        source = self._records()
        weight.assign_paper_weights(source)
        self.assertNotIn("citation_percentile_in_cohort", source[0])

    def test_records_without_a_month_get_no_percentile(self):
        rows = [
            {
                "month_bucket": None,
                "citation_count": 9,
                "publication_date": "2024",
                "collected_at": "2026-08-20T00:00:00Z",
            }
        ]
        self.assertIsNone(weight.assign_paper_weights(rows)[0]["citation_percentile_in_cohort"])

    def test_citation_per_year_clips_the_age_denominator(self):
        # 갓 나온 논문의 분모가 0 에 가까워 값이 폭발하는 것을 막는다
        rate = weight.citation_per_year(10, "2026-08-19", "2026-08-20T00:00:00Z")
        self.assertEqual(rate, 20.0)

    def test_citation_per_year_is_none_without_dates(self):
        self.assertIsNone(weight.citation_per_year(10, None, None))


class WeightedPrevalenceTest(unittest.TestCase):
    def test_averages_the_paper_percentiles(self):
        self.assertAlmostEqual(weight.weighted_prevalence([0.2, 0.4, 0.6]), 0.4)

    def test_skips_papers_without_a_percentile(self):
        self.assertAlmostEqual(weight.weighted_prevalence([0.2, None, 0.6]), 0.4)

    def test_all_missing_gives_none(self):
        self.assertIsNone(weight.weighted_prevalence([None, None]))


class MonthArithmeticTest(unittest.TestCase):
    def test_steps_back_across_a_year_boundary(self):
        self.assertEqual(aggregate.month_add("2026-01", -1), "2025-12")

    def test_steps_forward_across_a_year_boundary(self):
        self.assertEqual(aggregate.month_add("2025-12", 1), "2026-01")

    def test_steps_back_a_full_year(self):
        self.assertEqual(aggregate.month_add("2026-08", -12), "2025-08")

    def test_provisional_window_covers_the_most_recent_months(self):
        window = aggregate.provisional_months("2026-08-20T00:00:00Z", 3)
        self.assertEqual(window, {"2026-08", "2026-07", "2026-06"})


class MedianTest(unittest.TestCase):
    def test_odd_length(self):
        self.assertEqual(aggregate.median([3, 1, 2]), 2.0)

    def test_even_length_averages_the_middle_pair(self):
        self.assertEqual(aggregate.median([1, 2, 3, 4]), 2.5)

    def test_empty_is_none(self):
        self.assertIsNone(aggregate.median([]))


class ClassifyTest(unittest.TestCase):
    def test_a_tiny_keyword_is_never_classified(self):
        # 3편 -> 9편도 300% 다. 하한을 못 넘으면 분류하지 않는다.
        self.assertEqual(
            aggregate.classify(9, 4, 36, growth_ratio=3.0, is_new_entrant=False),
            "unrated",
        )

    def test_growth_with_broad_presence_is_emerging(self):
        self.assertEqual(
            aggregate.classify(200, 30, 36, growth_ratio=2.0, is_new_entrant=False),
            "emerging",
        )

    def test_growth_with_thin_presence_is_sporadic(self):
        # 특정 연구실이 한 번에 몇 편 낸 것일 가능성이 크다
        self.assertEqual(
            aggregate.classify(200, 5, 36, growth_ratio=2.0, is_new_entrant=False),
            "sporadic",
        )

    def test_flat_prevalence_with_broad_presence_is_steady(self):
        self.assertEqual(
            aggregate.classify(200, 30, 36, growth_ratio=1.0, is_new_entrant=False),
            "steady",
        )

    def test_shrinking_prevalence_is_declining(self):
        self.assertEqual(
            aggregate.classify(200, 30, 36, growth_ratio=0.4, is_new_entrant=False),
            "declining",
        )

    def test_a_new_entrant_with_presence_is_emerging(self):
        self.assertEqual(
            aggregate.classify(40, 10, 36, growth_ratio=None, is_new_entrant=True),
            "emerging",
        )

    def test_missing_growth_ratio_is_unrated(self):
        self.assertEqual(
            aggregate.classify(200, 30, 36, growth_ratio=None, is_new_entrant=False),
            "unrated",
        )


class MeanTest(unittest.TestCase):
    def test_absent_months_count_as_zero(self):
        # 등장한 달만 평균하면 희소한 키워드가 과대평가된다
        self.assertAlmostEqual(aggregate._mean([0.6], denominator=12), 0.05)

    def test_zero_denominator_is_none(self):
        self.assertIsNone(aggregate._mean([0.6], denominator=0))


if __name__ == "__main__":
    unittest.main()
