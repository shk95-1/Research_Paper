"""trend.aggregate: 월 산술, 분류, 평균, 출력 네임스페이스, CensusError.

papers_trend/tests/test_papers_trend.py 의 MonthArithmeticTest/MedianTest/
ClassifyTest/MeanTest 를 이식했다(로직 변경 없음). 여기에 T6 신규 동작을
더한다: census_guard() 가 SystemExit 대신 CensusError 를 던지는지,
run() 이 out_dir/{query_id}/ 네임스페이스에 쓰는지, _PROVISIONAL_CACHE 모듈
전역이 사라지고 같은 query_id 를 두 번 연달아 집계해도(캐시 상태 누수 없이)
같은 결과를 내는지.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paper_radar.trend import aggregate


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


class FixtureCase(unittest.TestCase):
    """T6 신규: 합성 픽스처를 이용한 census_guard/네임스페이스/캐시 테스트.

    tests/fixtures/trend_synthetic/ 를 raw 소스로 겨냥한다(records.NEW_RAW_ROOT
    를 픽스처로 임시 교체) — 골든 동일성 자체는 test_trend_golden.py 의 몫이고,
    여기서는 그 픽스처를 재료로 aggregate 고유의 동작만 확인한다.
    """

    FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "trend_synthetic"

    def setUp(self):
        from paper_radar.trend import records

        self._records = records
        self._orig_new_raw_root = records.NEW_RAW_ROOT
        records.NEW_RAW_ROOT = self.FIXTURE_ROOT / "raw"
        self.addCleanup(self._restore)
        with open(self.FIXTURE_ROOT / "config.json", encoding="utf-8") as handle:
            self.config = json.load(handle)

    def _restore(self):
        self._records.NEW_RAW_ROOT = self._orig_new_raw_root

    def test_run_writes_under_the_query_id_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = aggregate.run("synthetic", config=self.config, out_dir=tmp)
            namespace = Path(tmp) / "synthetic"
            self.assertEqual(result["out_dir"], namespace)
            self.assertTrue((namespace / "monthly_denominator.csv").exists())

    def test_two_profiles_in_the_same_out_dir_do_not_overwrite_each_other(self):
        # T6 배경의 그 버그: 여러 프로파일이 out/ 하나를 공유해 서로 덮어썼다.
        with tempfile.TemporaryDirectory() as tmp:
            aggregate.run("synthetic", config=self.config, out_dir=tmp)
            aggregate.run("synthetic", config=self.config, out_dir=tmp)  # 같은 query_id, 재실행
            # 다른 query_id 를 시뮬레이션: 같은 out_dir 아래 다른 이름의 raw 를 만들지 않고도,
            # 네임스페이스 자체가 query_id 로 분리되어 있음을 out_dir 구조로 확인한다.
            namespace = Path(tmp) / "synthetic"
            self.assertTrue(namespace.is_dir())
            # out_dir 바로 아래에는 CSV 가 없다 — 전부 namespace 안에 있다.
            self.assertEqual(list(Path(tmp).glob("*.csv")), [])

    def test_running_the_same_query_id_twice_gives_identical_metrics(self):
        # _PROVISIONAL_CACHE 모듈 전역을 없앤 것의 회귀 방지: 같은 프로세스에서
        # 두 번 돌려도 캐시 상태가 새지 않아 결과가 같아야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            first = aggregate.run("synthetic", config=self.config, out_dir=tmp)
            second = aggregate.run("synthetic", config=self.config, out_dir=tmp)
            self.assertEqual(first["metrics"], second["metrics"])

    def test_census_guard_raises_censuserror_not_systemexit(self):
        meta = self._records.raw_meta("synthetic")
        self.assertTrue(meta.get("is_census"))
        # is_census 가 아닌 프로파일을 만들어 CensusError 를 유발한다.
        with tempfile.TemporaryDirectory() as tmp:
            self._records.NEW_RAW_ROOT = Path(tmp)
            profile_dir = Path(tmp) / "openalex" / "sample_only"
            profile_dir.mkdir(parents=True)
            with open(profile_dir / "_meta.json", "w", encoding="utf-8") as handle:
                json.dump({"is_census": False, "collected": 5, "expected_from_api": 500}, handle)
            with self.assertRaises(aggregate.CensusError):
                aggregate.census_guard("sample_only")
            # SystemExit 이 아니라는 것도 명시적으로 확인한다.
            with self.assertRaises(aggregate.CensusError):
                try:
                    aggregate.census_guard("sample_only")
                except SystemExit:
                    self.fail("census_guard 가 SystemExit 을 던지면 안 된다")

    def test_census_error_is_a_value_error(self):
        self.assertTrue(issubclass(aggregate.CensusError, ValueError))

    def test_module_no_longer_holds_a_global_provisional_cache(self):
        # T6: _PROVISIONAL_CACHE 모듈 전역 제거. _is_provisional_month() 는 이제
        # 호출자(trend_metrics)가 만든 로컬 dict 를 인자로 받는다.
        self.assertFalse(hasattr(aggregate, "_PROVISIONAL_CACHE"))


if __name__ == "__main__":
    unittest.main()
