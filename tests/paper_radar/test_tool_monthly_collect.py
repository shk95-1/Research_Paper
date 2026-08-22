"""tool/monthly_collect.py 의 순수 부분(창 끝 계산/단계 목록 구성/exit code
집계) 단위 테스트. 네트워크가 있는 run_step()/main() 은 이 태스크에서
테스트하지 않는다(브리핑 지시: "실제 수집 실행은 하지 않는다") — 계획만
검증한다.

tool/monthly_collect.py 는 pytest 가 discover 하지 않는 위치에 있다
(testpaths=tests/ 바깥) — tests/paper_radar/test_tool_fetch_cosing.py 와
같은 방식으로 importlib 로 파일 경로를 직접 불러온다.
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "monthly_collect", _REPO_ROOT / "tool" / "monthly_collect.py"
)
monthly_collect = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(monthly_collect)


class PreviousCompletedMonthEndTest(unittest.TestCase):
    """직전 완료 월의 말일 계산 — 순수 함수."""

    def test_mid_month_input_returns_the_previous_months_last_day(self):
        # 브리핑 예시 그대로: 2026-08-22 실행 -> 2026-07-31.
        self.assertEqual(
            monthly_collect.previous_completed_month_end(date(2026, 8, 22)), "2026-07-31"
        )

    def test_first_of_month_input_still_returns_the_previous_months_last_day(self):
        self.assertEqual(
            monthly_collect.previous_completed_month_end(date(2026, 3, 1)), "2026-02-28"
        )

    def test_last_day_of_month_input_returns_the_previous_months_last_day(self):
        self.assertEqual(
            monthly_collect.previous_completed_month_end(date(2026, 3, 31)), "2026-02-28"
        )

    def test_january_input_crosses_the_year_boundary(self):
        # 브리핑 예시: --today 2026-01-05 -> 2025-12-31.
        self.assertEqual(
            monthly_collect.previous_completed_month_end(date(2026, 1, 5)), "2025-12-31"
        )

    def test_leap_year_february_is_handled(self):
        self.assertEqual(
            monthly_collect.previous_completed_month_end(date(2024, 3, 5)), "2024-02-29"
        )


class ComputeStepsTest(unittest.TestCase):
    """단계 목록 구성 — 순서·개수만 검증한다(실행하지 않는다)."""

    def test_two_profiles_two_providers_yields_four_collect_steps_first(self):
        steps = monthly_collect.compute_steps(
            ["sunscreen", "cosmetics"], ["openalex", "pubmed"], "2026-07-31", skip_trials=True
        )
        collect_steps = [s for s in steps if s["kind"] == "trend_collect"]
        self.assertEqual(len(collect_steps), 4)
        # collect 단계가 전부 목록 앞쪽에 온다(브리핑: "프로파일 x 프로바이더마다
        # ... 실행. 이어서 ...").
        self.assertTrue(all(s["kind"] == "trend_collect" for s in steps[:4]))

    def test_every_collect_step_carries_the_computed_window_to(self):
        steps = monthly_collect.compute_steps(
            ["sunscreen"], ["openalex", "pubmed"], "2026-07-31", skip_trials=True
        )
        for step in steps:
            if step["kind"] == "trend_collect":
                self.assertIn("--window-to", step["argv"])
                self.assertIn("2026-07-31", step["argv"])

    def test_aggregate_and_overlap_follow_per_profile_in_order(self):
        steps = monthly_collect.compute_steps(
            ["sunscreen"], ["openalex", "pubmed"], "2026-07-31", skip_trials=True
        )
        kinds = [s["kind"] for s in steps]
        # collect(openalex) + collect(pubmed) + aggregate(openalex) +
        # aggregate(pubmed) + overlap = 5 단계.
        self.assertEqual(
            kinds,
            [
                "trend_collect",
                "trend_collect",
                "trend_aggregate",
                "trend_aggregate",
                "trend_overlap",
            ],
        )

    def test_pubmed_aggregate_step_is_skipped_when_pubmed_is_not_a_provider(self):
        steps = monthly_collect.compute_steps(
            ["sunscreen"], ["openalex"], "2026-07-31", skip_trials=True
        )
        aggregate_argvs = [s["argv"] for s in steps if s["kind"] == "trend_aggregate"]
        self.assertEqual(len(aggregate_argvs), 1)
        self.assertNotIn("pubmed", aggregate_argvs[0])

    def test_trials_collect_runs_once_per_profile_using_the_profile_name_as_the_query(self):
        steps = monthly_collect.compute_steps(
            ["sunscreen", "cosmetics"], ["openalex"], "2026-07-31", skip_trials=False
        )
        trials_steps = [s for s in steps if s["kind"] == "trials_collect"]
        self.assertEqual(len(trials_steps), 2)
        queries = {s["argv"][s["argv"].index("--query") + 1] for s in trials_steps}
        self.assertEqual(queries, {"sunscreen", "cosmetics"})
        # trials 단계는 collect/aggregate/overlap 뒤, 목록의 맨 끝에 온다.
        self.assertTrue(all(s["kind"] == "trials_collect" for s in steps[-2:]))

    def test_skip_trials_omits_the_trials_collect_step_entirely(self):
        steps = monthly_collect.compute_steps(
            ["sunscreen"], ["openalex"], "2026-07-31", skip_trials=True
        )
        self.assertFalse(any(s["kind"] == "trials_collect" for s in steps))


class OverallExitCodeTest(unittest.TestCase):
    """각 단계의 exit code 집계 — 순수 함수."""

    def test_all_zero_is_zero(self):
        results = [("a", 0), ("b", 0)]
        self.assertEqual(monthly_collect.overall_exit_code(results), 0)

    def test_one_nonzero_makes_the_whole_thing_one(self):
        results = [("a", 0), ("b", 1)]
        self.assertEqual(monthly_collect.overall_exit_code(results), 1)

    def test_none_from_a_dry_run_skip_does_not_count_as_a_failure(self):
        results = [("a", 0), ("b", None)]
        self.assertEqual(monthly_collect.overall_exit_code(results), 0)

    def test_empty_results_is_zero(self):
        self.assertEqual(monthly_collect.overall_exit_code([]), 0)


if __name__ == "__main__":
    unittest.main()
