"""trend.weight: 코호트 백분위, 인용 가중치.

papers_trend/tests/test_papers_trend.py 의 CohortPercentileTest/
PaperWeightsTest/WeightedPrevalenceTest 를 이식했다(로직 변경 없음 — 이
모듈은 경로 의존이 없어 파일 자체가 papers_trend/weight.py 와 동일하다).
"""

from __future__ import annotations

import unittest

from paper_radar.trend import weight


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


if __name__ == "__main__":
    unittest.main()
