"""cli 의 `trend` 그룹: papers_trend/*.py 의 개별 `python -m` 진입점들을
`paper-radar trend collect|records|normalize|aggregate|unmatched` 로 통합한
것(T6). 최상위 "evidence trend"(연도별 논문 수, tests/paper_radar/test_cli.py
의 TrendOutputTest)와 이름이 겹치므로 클래스 이름에 Trend 접두를 명시해
구분한다.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_radar import cli
from paper_radar.trend import records as trend_records

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "trend_synthetic"


class TrendParseArgsTest(unittest.TestCase):
    def test_collect_defaults_the_profile_to_all(self):
        args = cli.parse_args(["trend", "collect"])
        self.assertEqual(args.profile, "all")

    def test_collect_reads_max_pages(self):
        args = cli.parse_args(["trend", "collect", "--max-pages", "3"])
        self.assertEqual(args.max_pages, 3)

    def test_records_requires_a_profile(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["trend", "records"])

    def test_aggregate_reads_allow_sample(self):
        args = cli.parse_args(["trend", "aggregate", "--profile", "x", "--allow-sample"])
        self.assertTrue(args.allow_sample)

    def test_unmatched_defaults_field_to_keywords_norm(self):
        args = cli.parse_args(["trend", "unmatched", "--profile", "x"])
        self.assertEqual(args.field, "keywords_norm")

    def test_unmatched_rejects_an_unknown_field(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["trend", "unmatched", "--profile", "x", "--field", "bogus"])

    def test_no_command_under_trend_exits_with_usage(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["trend"])


class _FixtureCase(unittest.TestCase):
    def setUp(self):
        self._orig_new_raw_root = trend_records.NEW_RAW_ROOT
        trend_records.NEW_RAW_ROOT = FIXTURE_ROOT / "raw"
        self.addCleanup(self._restore)

    def _restore(self):
        trend_records.NEW_RAW_ROOT = self._orig_new_raw_root


class TrendRecordsOutputTest(_FixtureCase):
    def test_prints_a_summary_for_the_synthetic_profile(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.run(cli.parse_args(["trend", "records", "--profile", "synthetic"]))
        text = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("레코드 38건", text)
        self.assertIn("DOI 보유: 37건", text)


class TrendNormalizeOutputTest(_FixtureCase):
    def test_prints_lexicon_hit_counts(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.run(
                cli.parse_args(["trend", "normalize", "--profile", "synthetic", "--top", "3"])
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("사전 적중", output.getvalue())


class TrendAggregateOutputTest(_FixtureCase):
    def test_writes_csvs_under_the_profile_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(
                    cli.parse_args(["trend", "aggregate", "--profile", "synthetic", "--out", tmp])
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue((Path(tmp) / "synthetic" / "monthly_denominator.csv").exists())
            self.assertIn("레코드 38건", output.getvalue())

    def test_census_error_becomes_exit_code_one_with_the_original_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            trend_records.NEW_RAW_ROOT = Path(tmp)
            profile_dir = Path(tmp) / "openalex" / "sample_only"
            profile_dir.mkdir(parents=True)
            with open(profile_dir / "_meta.json", "w", encoding="utf-8") as handle:
                json.dump({"is_census": False, "collected": 1, "expected_from_api": 100}, handle)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = cli.run(
                    cli.parse_args(
                        ["trend", "aggregate", "--profile", "sample_only", "--out", tmp]
                    )
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("[중단]", stderr.getvalue())
        self.assertIn("--allow-sample", stderr.getvalue())


class TrendUnmatchedOutputTest(_FixtureCase):
    def test_lists_unmatched_terms_by_frequency(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(
                    cli.parse_args(
                        [
                            "trend",
                            "unmatched",
                            "--profile",
                            "synthetic",
                            "--out",
                            str(Path(tmp) / "u.csv"),
                        ]
                    )
                )
        self.assertEqual(exit_code, 0)
        self.assertIn("cactus extract complex", output.getvalue())


class TrendCollectOutputTest(unittest.TestCase):
    """실제 네트워크는 쓰지 않는다 — trend.collect.run() 을 mock 으로 대체하고
    CLI 의 인자 해석·출력·exit code 만 검증한다(evidence 쪽 CollectOutputTest
    와 같은 방식)."""

    def test_dry_run_prints_the_estimated_request_count(self):
        config = {"profiles": {"demo": {"query": "demo"}}, "window": {}, "per_page": 200}
        with (
            mock.patch.object(cli.trend_collect, "load_config", return_value=config),
            mock.patch.object(
                cli.trend_collect,
                "run",
                return_value={"demo": {"dry_run": True, "count": 500, "estimated_requests": 3}},
            ) as run_mock,
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(
                    cli.parse_args(["trend", "collect", "--profile", "demo", "--dry-run"])
                )
        self.assertEqual(exit_code, 0)
        self.assertIn("500", output.getvalue())
        self.assertIn("예상 요청 3회", output.getvalue())
        self.assertTrue(run_mock.call_args.kwargs["dry_run"])

    def test_unknown_profile_exits_with_code_one(self):
        config = {"profiles": {"demo": {"query": "demo"}}, "window": {}, "per_page": 200}
        with mock.patch.object(cli.trend_collect, "load_config", return_value=config):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = cli.run(
                    cli.parse_args(["trend", "collect", "--profile", "nope"])
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("nope", stderr.getvalue())

    def test_exit_code_is_one_when_a_profile_stopped_on_budget(self):
        config = {"profiles": {"demo": {"query": "demo"}}, "window": {}, "per_page": 200}
        with (
            mock.patch.object(cli.trend_collect, "load_config", return_value=config),
            mock.patch.object(
                cli.trend_collect,
                "run",
                return_value={"demo": {"stopped_reason": "budget_exhausted"}},
            ),
        ):
            exit_code = cli.run(cli.parse_args(["trend", "collect", "--profile", "demo"]))
        self.assertEqual(exit_code, 1)

    def test_exit_code_is_zero_on_a_clean_run(self):
        config = {"profiles": {"demo": {"query": "demo"}}, "window": {}, "per_page": 200}
        with (
            mock.patch.object(cli.trend_collect, "load_config", return_value=config),
            mock.patch.object(
                cli.trend_collect, "run", return_value={"demo": {"stopped_reason": None}}
            ),
        ):
            exit_code = cli.run(cli.parse_args(["trend", "collect", "--profile", "demo"]))
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
