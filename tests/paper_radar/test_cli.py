"""cli: 인자 파싱과 출력. 기본 연도 범위가 '최근 10년'이라는 것이 핵심.

papers/tests/test_cli.py 의 collect/trend/cite 출력 케이스를 새 명령 구조
(`evidence collect|trend|cite`) 와 CollectReport 기반 exit code 규칙에 맞게
이식했다.
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from paper_radar import cli
from paper_radar.evidence.pipeline import CollectReport
from paper_radar.storage import repository


def record(**overrides):
    """스펙 8절 레코드 스키마를 따르는 최소 레코드(verification 미포함)."""
    base = {
        "doi": "10.1016/j.test.2024.01.001",
        "openalex_id": "https://openalex.org/W1",
        "title": "Retinol and the skin barrier",
        "authors": ["Kim", "Lee"],
        "year": 2024,
        "journal": "Journal of Cosmetic Science",
        "abstract": "Retinol improves the skin barrier function.",
        "tldr": "Retinol helps the barrier.",
        "keywords": ["retinol", "skin barrier"],
        "topics": ["Dermatology"],
        "citation_count": 12,
        "is_open_access": True,
        "url": "https://doi.org/10.1016/j.test.2024.01.001",
        "collected_at": "2026-08-19T10:00:00Z",
    }
    base.update(overrides)
    return base


def verification(**overrides):
    base = {
        "crossref_verified": True,
        "title_match": True,
        "found_in_sources": ["openalex", "semantic_scholar"],
        "is_retracted": False,
        "has_doi": True,
        "confidence_score": 85,
    }
    base.update(overrides)
    return base


def report(records=None, errors_by_source=None, stopped_reason=None, status="ok"):
    return CollectReport(
        run_id="test-run",
        records=records or [],
        errors_by_source=errors_by_source or {},
        stopped_reason=stopped_reason or {},
        status=status,
    )


class ParseArgsTest(unittest.TestCase):
    def test_collect_defaults_to_the_last_ten_years(self):
        args = cli.parse_args(["evidence", "collect", "--query", "cosmetic"])
        self.assertEqual(args.year_to - args.year_from, 9)

    def test_collect_reads_an_explicit_year_range(self):
        args = cli.parse_args(
            ["evidence", "collect", "--query", "cosmetic", "--from", "2016", "--to", "2026"]
        )
        self.assertEqual((args.year_from, args.year_to), (2016, 2026))

    def test_collect_defaults_the_limit_to_twenty_five(self):
        args = cli.parse_args(["evidence", "collect", "--query", "cosmetic"])
        self.assertEqual(args.limit, 25)

    def test_collect_reads_an_explicit_limit(self):
        args = cli.parse_args(["evidence", "collect", "--query", "cosmetic", "--limit", "100"])
        self.assertEqual(args.limit, 100)

    def test_cite_defaults_min_confidence_to_zero(self):
        args = cli.parse_args(["evidence", "cite", "--keyword", "skin barrier"])
        self.assertEqual(args.min_confidence, 0)

    def test_cite_reads_an_explicit_min_confidence(self):
        args = cli.parse_args(["evidence", "cite", "--keyword", "x", "--min-confidence", "70"])
        self.assertEqual(args.min_confidence, 70)

    def test_trend_takes_a_query(self):
        args = cli.parse_args(["evidence", "trend", "--query", "cosmetic retinol"])
        self.assertEqual(args.command, "trend")
        self.assertEqual(args.query, "cosmetic retinol")

    def test_collect_requires_a_query(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["evidence", "collect"])

    def test_cite_requires_a_keyword(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["evidence", "cite"])

    def test_no_group_exits_with_usage(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args([])

    def test_no_command_under_evidence_exits_with_usage(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["evidence"])

    def test_evidence_help_exits_cleanly(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
            cli.parse_args(["evidence", "--help"])
        self.assertEqual(ctx.exception.code, 0)


class TrendOutputTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(
            "os.environ",
            {"OPENALEX_API_KEY": "test-key", "OPENALEX_EMAIL": "a@b.com"},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prints_a_row_per_year_with_the_count(self):
        with mock.patch.object(cli.openalex, "trend", return_value=[(2016, 90), (2017, 120)]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(cli.parse_args(["evidence", "trend", "--query", "cosmetic"]))
        text = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("2016", text)
        self.assertIn("90", text)
        self.assertIn("2017", text)
        self.assertIn("120", text)

    def test_reports_when_nothing_was_found(self):
        with mock.patch.object(cli.openalex, "trend", return_value=[]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(cli.parse_args(["evidence", "trend", "--query", "nothing"]))
        self.assertEqual(exit_code, 0)
        self.assertIn("결과가 없습니다", output.getvalue())


class CiteOutputTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        conn = repository.connect(self.path)
        repository.upsert(conn, record(), verification())
        conn.close()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _cite(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.run(cli.parse_args(argv + ["--db", self.path]))
        return exit_code, output.getvalue()

    def test_prints_the_matching_paper_with_its_score(self):
        exit_code, text = self._cite(["evidence", "cite", "--keyword", "retinol"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Retinol and the skin barrier", text)
        self.assertIn("85", text)

    def test_prints_the_doi_so_the_citation_can_be_pasted(self):
        _, text = self._cite(["evidence", "cite", "--keyword", "retinol"])
        self.assertIn("10.1016/j.test.2024.01.001", text)

    def test_reports_when_the_keyword_matches_nothing(self):
        exit_code, text = self._cite(["evidence", "cite", "--keyword", "niacinamide"])
        self.assertEqual(exit_code, 0)
        self.assertIn("결과가 없습니다", text)

    def test_applies_the_min_confidence_floor(self):
        _, text = self._cite(
            ["evidence", "cite", "--keyword", "retinol", "--min-confidence", "90"]
        )
        self.assertIn("결과가 없습니다", text)


class CollectOutputTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for path in (self.path, self.path + ".json"):
            if os.path.exists(path):
                os.unlink(path)

    def _collect(self, argv, collect_return):
        self.enterContext(
            mock.patch.dict(
                "os.environ",
                {"OPENALEX_API_KEY": "test-key", "OPENALEX_EMAIL": "a@b.com"},
                clear=False,
            )
        )
        output = io.StringIO()
        with (
            mock.patch.object(cli.pipeline, "collect", return_value=collect_return) as collect,
            contextlib.redirect_stdout(output),
        ):
            exit_code = cli.run(cli.parse_args(argv + ["--db", self.path]))
        return exit_code, output.getvalue(), collect

    def test_reports_how_many_papers_were_stored(self):
        collected = [
            dict(record(), verification=verification()),
            dict(record(doi="10.1/b"), verification=verification()),
        ]
        exit_code, text, _ = self._collect(
            ["evidence", "collect", "--query", "cosmetic"], report(records=collected)
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("2", text)

    def test_passes_the_parsed_arguments_through_to_the_pipeline(self):
        _, _, collect = self._collect(
            [
                "evidence",
                "collect",
                "--query",
                "cosmetic retinol",
                "--from",
                "2016",
                "--to",
                "2026",
                "--limit",
                "7",
            ],
            report(),
        )
        _, _, query, year_from, year_to, limit = collect.call_args.args
        self.assertEqual((query, year_from, year_to, limit), ("cosmetic retinol", 2016, 2026, 7))

    def test_returns_zero_for_a_fully_ok_report(self):
        exit_code, _, _ = self._collect(
            ["evidence", "collect", "--query", "cosmetic"], report(status="ok")
        )
        self.assertEqual(exit_code, 0)

    def test_returns_one_when_the_report_has_a_stopped_reason(self):
        exit_code, _, _ = self._collect(
            ["evidence", "collect", "--query", "cosmetic"],
            report(stopped_reason={"openalex": "budget_exhausted"}, status="partial"),
        )
        self.assertEqual(exit_code, 1)

    def test_returns_one_when_a_source_had_errors(self):
        exit_code, _, _ = self._collect(
            ["evidence", "collect", "--query", "cosmetic"],
            report(errors_by_source={"europepmc": 2}, status="partial"),
        )
        self.assertEqual(exit_code, 1)

    def test_exit_code_follows_report_status_rather_than_recomputing_it(self):
        """partial/ok 판정은 pipeline.collect() 한 곳에서만 계산한다 — CLI 는
        report.status 를 그대로 옮길 뿐, stopped_reason/errors_by_source 를
        다시 훑어 재계산하지 않는다. status="partial" 인데 stopped_reason/
        errors_by_source 가 비어 있어도(재계산했다면 0 이 됐을 상황) 여전히
        exit 1 이어야 한다 — 판정의 단일 출처가 report.status 임을 증명한다."""
        exit_code, _, _ = self._collect(
            ["evidence", "collect", "--query", "cosmetic"],
            report(status="partial"),  # stopped_reason={}, errors_by_source={}
        )
        self.assertEqual(exit_code, 1)

    def test_warns_when_no_contact_email_is_configured(self):
        self.enterContext(
            mock.patch.dict(
                "os.environ", {"OPENALEX_API_KEY": "test-key", "OPENALEX_EMAIL": ""}, clear=False
            )
        )
        with mock.patch.object(cli.pipeline, "collect", return_value=report()):
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                cli.run(
                    cli.parse_args(
                        ["evidence", "collect", "--query", "cosmetic", "--db", self.path]
                    )
                )
        self.assertIn("OPENALEX_EMAIL", stderr.getvalue())

    def test_stays_quiet_about_email_when_one_is_configured(self):
        self.enterContext(
            mock.patch.dict(
                "os.environ",
                {"OPENALEX_API_KEY": "test-key", "OPENALEX_EMAIL": "a@b.com"},
                clear=False,
            )
        )
        with mock.patch.object(cli.pipeline, "collect", return_value=report()):
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                cli.run(
                    cli.parse_args(
                        ["evidence", "collect", "--query", "cosmetic", "--db", self.path]
                    )
                )
        self.assertNotIn("OPENALEX_EMAIL", stderr.getvalue())

    def test_warns_when_no_openalex_api_key_is_configured(self):
        self.enterContext(
            mock.patch.dict(
                "os.environ", {"OPENALEX_API_KEY": "", "OPENALEX_EMAIL": "a@b.com"}, clear=False
            )
        )
        with mock.patch.object(cli.pipeline, "collect", return_value=report()):
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                cli.run(
                    cli.parse_args(
                        ["evidence", "collect", "--query", "cosmetic", "--db", self.path]
                    )
                )
        self.assertIn("OPENALEX_API_KEY", stderr.getvalue())
        self.assertIn("1,000크레딧", stderr.getvalue())

    def test_stays_quiet_about_the_api_key_when_one_is_configured(self):
        self.enterContext(
            mock.patch.dict(
                "os.environ",
                {"OPENALEX_API_KEY": "secret", "OPENALEX_EMAIL": "a@b.com"},
                clear=False,
            )
        )
        with mock.patch.object(cli.pipeline, "collect", return_value=report()):
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                cli.run(
                    cli.parse_args(
                        ["evidence", "collect", "--query", "cosmetic", "--db", self.path]
                    )
                )
        self.assertNotIn("OPENALEX_API_KEY", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
