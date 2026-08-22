"""cli 의 `trend` 그룹: papers_trend/*.py 의 개별 `python -m` 진입점들을
`paper-radar trend collect|records|normalize|aggregate|unmatched` 로 통합한
것(T6). 최상위 "evidence trend"(연도별 논문 수, tests/paper_radar/test_cli.py
의 TrendOutputTest)와 이름이 겹치므로 클래스 이름에 Trend 접두를 명시해
구분한다.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_radar import cli
from paper_radar.models import IngredientRecord
from paper_radar.storage import repository
from paper_radar.trend import records as trend_records
from paper_radar.trend import unmatched as trend_unmatched

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

    def test_suggest_requires_a_profile(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["trend", "suggest"])

    def test_collect_defaults_the_provider_to_openalex(self):
        # T15: --provider 기본값은 openalex — 기존 동작 불변(브리핑 지시).
        args = cli.parse_args(["trend", "collect"])
        self.assertEqual(args.provider, "openalex")

    def test_collect_reads_provider_pubmed_and_max_months(self):
        args = cli.parse_args(
            ["trend", "collect", "--provider", "pubmed", "--max-months", "2"]
        )
        self.assertEqual(args.provider, "pubmed")
        self.assertEqual(args.max_months, 2)

    def test_collect_rejects_an_unknown_provider(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["trend", "collect", "--provider", "bogus"])

    def test_aggregate_defaults_the_provider_to_openalex(self):
        args = cli.parse_args(["trend", "aggregate", "--profile", "x"])
        self.assertEqual(args.provider, "openalex")

    def test_overlap_requires_a_profile(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["trend", "overlap"])


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


def _unmatched_row(**overrides):
    base = {
        "rank": "1",
        "term": "niacinamide",
        "paper_count": "5",
        "first_seen_month": "2024-01",
        "last_seen_month": "2024-03",
        "months_present": "2",
        "example_title_1": "",
        "example_title_2": "",
        "example_title_3": "",
        "verdict": "",
    }
    base.update(overrides)
    return base


class TrendSuggestOutputTest(unittest.TestCase):
    """`trend suggest` — T14, 로컬 전용(네트워크 없음). 실제 SQLite DB + 실제
    unmatched CSV 픽스처로 end-to-end(모킹 없음, ingredient show/import-cosing
    CLI 테스트와 같은 방식)."""

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.db_path)
        self.addCleanup(lambda: os.path.exists(self.db_path) and os.unlink(self.db_path))
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)

    def _write_unmatched_csv(self, profile, rows):
        path = trend_unmatched.default_path(profile, "keywords_norm", out_dir=self.tmp_dir.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=trend_unmatched.FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _suggest(self, profile):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.run(
                cli.parse_args(
                    [
                        "trend",
                        "suggest",
                        "--profile",
                        profile,
                        "--db",
                        self.db_path,
                        "--out",
                        self.tmp_dir.name,
                    ]
                )
            )
        return exit_code, output.getvalue()

    def test_prints_the_review_and_suggestion_counts_and_writes_the_csv(self):
        self._write_unmatched_csv("sunscreen", [_unmatched_row(term="niacinamide")])
        conn = repository.connect(self.db_path)
        repository.upsert_records(
            conn,
            [
                IngredientRecord(
                    name_key="niacinamide",
                    inci_name="Niacinamide",
                    cid=936,
                    cas="98-92-0",
                    synonyms=(),
                    sources=("pubchem",),
                    fetched_at="2026-08-21T00:00:00Z",
                )
            ],
        )
        conn.close()

        exit_code, text = self._suggest("sunscreen")
        self.assertEqual(exit_code, 0)
        self.assertIn("검토", text)
        self.assertIn("제안 1건", text)
        target = Path(self.tmp_dir.name) / "sunscreen" / "lexicon_suggestions.csv"
        self.assertTrue(target.exists())

    def test_reports_an_empty_ingredient_table_without_writing_a_file(self):
        self._write_unmatched_csv("sunscreen", [_unmatched_row(term="niacinamide")])
        repository.connect(self.db_path).close()  # DB 는 있지만 ingredient 행이 없다.

        exit_code, text = self._suggest("sunscreen")
        self.assertEqual(exit_code, 0)
        self.assertIn("ingredient 테이블이 비어 있습니다", text)
        target = Path(self.tmp_dir.name) / "sunscreen" / "lexicon_suggestions.csv"
        self.assertFalse(target.exists())


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


class TrendCollectWindowToFlagTest(unittest.TestCase):
    """항목2: --window-to 인자 파싱·검증·수집기 호출 전달. 네트워크는 mock 으로 대체한다."""

    def _config(self):
        return {
            "profiles": {"demo": {"query": "demo"}},
            "window": {"from": "2023-01-01", "to": "2026-08-31"},
            "per_page": 200,
        }

    def test_parse_args_reads_window_to(self):
        args = cli.parse_args(["trend", "collect", "--window-to", "2026-07-31"])
        self.assertEqual(args.window_to, "2026-07-31")

    def test_defaults_to_none_when_omitted(self):
        args = cli.parse_args(["trend", "collect"])
        self.assertIsNone(args.window_to)

    def test_is_passed_through_to_collect_run(self):
        with (
            mock.patch.object(cli.trend_collect, "load_config", return_value=self._config()),
            mock.patch.object(
                cli.trend_collect, "run", return_value={"demo": {"stopped_reason": None}}
            ) as run_mock,
        ):
            exit_code = cli.run(
                cli.parse_args(
                    ["trend", "collect", "--profile", "demo", "--window-to", "2026-07-31"]
                )
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(run_mock.call_args.kwargs["window_to"], "2026-07-31")

    def test_omitted_flag_passes_none_through_unchanged_behavior(self):
        with (
            mock.patch.object(cli.trend_collect, "load_config", return_value=self._config()),
            mock.patch.object(
                cli.trend_collect, "run", return_value={"demo": {"stopped_reason": None}}
            ) as run_mock,
        ):
            cli.run(cli.parse_args(["trend", "collect", "--profile", "demo"]))
        self.assertIsNone(run_mock.call_args.kwargs["window_to"])

    def test_invalid_format_exits_with_code_one(self):
        with mock.patch.object(cli.trend_collect, "load_config", return_value=self._config()):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = cli.run(
                    cli.parse_args(
                        ["trend", "collect", "--profile", "demo", "--window-to", "2026/07/31"]
                    )
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("YYYY-MM-DD", stderr.getvalue())

    def test_earlier_than_window_from_exits_with_code_one(self):
        with mock.patch.object(cli.trend_collect, "load_config", return_value=self._config()):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = cli.run(
                    cli.parse_args(
                        ["trend", "collect", "--profile", "demo", "--window-to", "2020-01-01"]
                    )
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("window.from", stderr.getvalue())

    def test_is_also_passed_through_for_the_pubmed_provider(self):
        config = {
            "profiles": {"sunscreen": {"query": "q", "pubmed_query": "pq"}},
            "window": {"from": "2024-01-01", "to": "2024-01-31"},
        }
        with (
            mock.patch.object(cli.trend_collect_pubmed, "load_config", return_value=config),
            mock.patch.object(
                cli.trend_collect_pubmed,
                "run",
                return_value={"sunscreen": {"stopped_reason": None}},
            ) as run_mock,
        ):
            exit_code = cli.run(
                cli.parse_args(
                    [
                        "trend",
                        "collect",
                        "--profile",
                        "sunscreen",
                        "--provider",
                        "pubmed",
                        "--window-to",
                        "2024-02-29",
                    ]
                )
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(run_mock.call_args.kwargs["window_to"], "2024-02-29")


class TrendCollectPubmedProviderRoutingTest(unittest.TestCase):
    """T15: --provider pubmed 는 trend_collect(openalex)가 아니라
    trend_collect_pubmed 를 부른다. 네트워크는 mock 으로 대체한다."""

    def test_dry_run_routes_to_collect_pubmed_and_prints_the_month_count(self):
        config = {
            "profiles": {"sunscreen": {"query": "q", "pubmed_query": "pq"}},
            "window": {"from": "2024-01-01", "to": "2024-01-31"},
        }
        with (
            mock.patch.object(cli.trend_collect_pubmed, "load_config", return_value=config),
            mock.patch.object(
                cli.trend_collect_pubmed,
                "run",
                return_value={"sunscreen": {"dry_run": True, "count": 42, "months": 1}},
            ) as run_mock,
            mock.patch.object(cli.trend_collect, "run") as openalex_run_mock,
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(
                    cli.parse_args(
                        [
                            "trend",
                            "collect",
                            "--profile",
                            "sunscreen",
                            "--provider",
                            "pubmed",
                            "--dry-run",
                        ]
                    )
                )
        self.assertEqual(exit_code, 0)
        self.assertIn("42", output.getvalue())
        self.assertTrue(run_mock.call_args.kwargs["dry_run"])
        # openalex 쪽 run() 은 전혀 호출되지 않았어야 한다(라우팅이 실제로 갈렸는지 확인).
        openalex_run_mock.assert_not_called()

    def test_named_profile_without_pubmed_query_exits_with_code_one(self):
        config = {
            "profiles": {"cosmetics": {"query": "q"}},  # pubmed_query 없음
            "window": {"from": "2024-01-01", "to": "2024-01-31"},
        }
        with mock.patch.object(cli.trend_collect_pubmed, "load_config", return_value=config):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = cli.run(
                    cli.parse_args(
                        ["trend", "collect", "--profile", "cosmetics", "--provider", "pubmed"]
                    )
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("pubmed_query", stderr.getvalue())

    def test_profile_all_silently_skips_profiles_without_pubmed_query(self):
        config = {
            "profiles": {
                "cosmetics": {"query": "q"},  # pubmed_query 없음 -> 걸러진다
                "sunscreen": {"query": "q", "pubmed_query": "pq"},
            },
            "window": {"from": "2024-01-01", "to": "2024-01-31"},
        }
        with (
            mock.patch.object(cli.trend_collect_pubmed, "load_config", return_value=config),
            mock.patch.object(
                cli.trend_collect_pubmed,
                "run",
                return_value={"sunscreen": {"stopped_reason": None}},
            ) as run_mock,
        ):
            exit_code = cli.run(
                cli.parse_args(["trend", "collect", "--profile", "all", "--provider", "pubmed"])
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(run_mock.call_args.args[0], ["sunscreen"])

    def test_max_months_is_passed_through_to_collect_pubmed_run(self):
        config = {
            "profiles": {"sunscreen": {"query": "q", "pubmed_query": "pq"}},
            "window": {"from": "2024-01-01", "to": "2024-01-31"},
        }
        with (
            mock.patch.object(cli.trend_collect_pubmed, "load_config", return_value=config),
            mock.patch.object(
                cli.trend_collect_pubmed,
                "run",
                return_value={"sunscreen": {"stopped_reason": None}},
            ) as run_mock,
        ):
            cli.run(
                cli.parse_args(
                    [
                        "trend",
                        "collect",
                        "--profile",
                        "sunscreen",
                        "--provider",
                        "pubmed",
                        "--max-months",
                        "3",
                    ]
                )
            )
        self.assertEqual(run_mock.call_args.kwargs["max_months"], 3)


class TrendAggregatePubmedProviderRoutingTest(unittest.TestCase):
    def test_provider_pubmed_routes_to_mesh_aggregate_and_prints_mesh_monthly(self):
        with mock.patch.object(
            cli.trend_mesh_aggregate,
            "run",
            return_value={
                "records": 5,
                "months": 2,
                "written": {"mesh_monthly.csv": 3},
                "provisional": [],
                "low_sample": [],
            },
        ) as run_mock:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(
                    cli.parse_args(
                        ["trend", "aggregate", "--profile", "sunscreen", "--provider", "pubmed"]
                    )
                )
        self.assertEqual(exit_code, 0)
        self.assertIn("mesh_monthly.csv", output.getvalue())
        self.assertTrue(run_mock.called)

    def test_provider_pubmed_census_error_becomes_exit_code_one(self):
        with mock.patch.object(
            cli.trend_mesh_aggregate,
            "run",
            side_effect=cli.trend_mesh_aggregate.CensusError("[중단] 전수가 아닙니다"),
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = cli.run(
                    cli.parse_args(
                        ["trend", "aggregate", "--profile", "sunscreen", "--provider", "pubmed"]
                    )
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("[중단]", stderr.getvalue())


class TrendOverlapOutputTest(unittest.TestCase):
    def test_prints_the_target_and_summed_columns(self):
        rows = [
            {
                "month_bucket": "2024-01",
                "openalex_papers": 3,
                "pubmed_papers": 3,
                "both_by_doi": 1,
                "openalex_only": 1,
                "pubmed_only": 1,
                "pubmed_doi_missing": 1,
            }
        ]
        overlap_result = {
            "target": Path("/tmp/out/sunscreen/provider_overlap.csv"),
            "rows": rows,
            "written": 1,
        }
        with mock.patch.object(cli.trend_overlap, "run", return_value=overlap_result):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(
                    cli.parse_args(["trend", "overlap", "--profile", "sunscreen"])
                )
        self.assertEqual(exit_code, 0)
        self.assertIn("provider_overlap.csv", output.getvalue())
        self.assertIn("both_by_doi 합 1", output.getvalue())

    def test_missing_raw_becomes_exit_code_zero_with_a_message(self):
        with mock.patch.object(
            cli.trend_overlap,
            "run",
            side_effect=cli.trend_overlap.MissingRawError("sunscreen: pubmed raw 가 없습니다."),
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(
                    cli.parse_args(["trend", "overlap", "--profile", "sunscreen"])
                )
        self.assertEqual(exit_code, 0)
        self.assertIn("pubmed raw 가 없습니다", output.getvalue())


if __name__ == "__main__":
    unittest.main()
