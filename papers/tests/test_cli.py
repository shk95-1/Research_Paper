"""cli: 인자 파싱과 출력. 기본 연도 범위가 '최근 10년'이라는 것이 핵심."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from papers import cli, store
from papers.tests.test_store import record


class ParseArgsTest(unittest.TestCase):
    def test_collect_defaults_to_the_last_ten_years(self):
        args = cli.parse_args(["collect", "--query", "cosmetic"])
        self.assertEqual(args.year_to - args.year_from, 9)

    def test_collect_reads_an_explicit_year_range(self):
        args = cli.parse_args(["collect", "--query", "cosmetic", "--from", "2016", "--to", "2026"])
        self.assertEqual((args.year_from, args.year_to), (2016, 2026))

    def test_collect_defaults_the_limit_to_twenty_five(self):
        args = cli.parse_args(["collect", "--query", "cosmetic"])
        self.assertEqual(args.limit, 25)

    def test_collect_reads_an_explicit_limit(self):
        args = cli.parse_args(["collect", "--query", "cosmetic", "--limit", "100"])
        self.assertEqual(args.limit, 100)

    def test_cite_defaults_min_confidence_to_zero(self):
        args = cli.parse_args(["cite", "--keyword", "skin barrier"])
        self.assertEqual(args.min_confidence, 0)

    def test_cite_reads_an_explicit_min_confidence(self):
        args = cli.parse_args(["cite", "--keyword", "x", "--min-confidence", "70"])
        self.assertEqual(args.min_confidence, 70)

    def test_trend_takes_a_query(self):
        args = cli.parse_args(["trend", "--query", "cosmetic retinol"])
        self.assertEqual(args.command, "trend")
        self.assertEqual(args.query, "cosmetic retinol")

    def test_collect_requires_a_query(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.parse_args(["collect"])

    def test_cite_requires_a_keyword(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.parse_args(["cite"])

    def test_no_command_exits_with_usage(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.parse_args([])


class TrendOutputTest(unittest.TestCase):
    def setUp(self):
        # 테스트는 .env 를 읽지 않으므로 자격 증명 경고가 항상 뜬다.
        # 경고 자체는 CollectOutputTest 에서 검증한다.
        patcher = mock.patch.object(cli.http, "contact_email", return_value="a@b.com")
        patcher.start()
        self.addCleanup(patcher.stop)
        env_patcher = mock.patch.dict("os.environ", {"OPENALEX_API_KEY": "test-key"}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def test_prints_a_row_per_year_with_the_count(self):
        with mock.patch.object(cli.openalex, "trend", return_value=[(2016, 90), (2017, 120)]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(cli.parse_args(["trend", "--query", "cosmetic"]))
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
                exit_code = cli.run(cli.parse_args(["trend", "--query", "nothing"]))
        self.assertEqual(exit_code, 0)
        self.assertIn("결과가 없습니다", output.getvalue())


class CiteOutputTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        conn = store.connect(self.path)
        store.upsert(conn, record())
        conn.close()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _cite(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.run(cli.parse_args(argv + ["--db", self.path]))
        return exit_code, output.getvalue()

    def test_prints_the_matching_paper_with_its_score(self):
        exit_code, text = self._cite(["cite", "--keyword", "retinol"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Retinol and the skin barrier", text)
        self.assertIn("85", text)

    def test_prints_the_doi_so_the_citation_can_be_pasted(self):
        _, text = self._cite(["cite", "--keyword", "retinol"])
        self.assertIn("10.1016/j.test.2024.01.001", text)

    def test_reports_when_the_keyword_matches_nothing(self):
        exit_code, text = self._cite(["cite", "--keyword", "niacinamide"])
        self.assertEqual(exit_code, 0)
        self.assertIn("결과가 없습니다", text)

    def test_applies_the_min_confidence_floor(self):
        _, text = self._cite(["cite", "--keyword", "retinol", "--min-confidence", "90"])
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

    def test_reports_how_many_papers_were_stored(self):
        self.enterContext(mock.patch.object(cli.http, "contact_email", return_value="a@b.com"))
        self.enterContext(
            mock.patch.dict("os.environ", {"OPENALEX_API_KEY": "test-key"}, clear=False)
        )
        collected = [record(), record(doi="10.1/b")]
        with mock.patch.object(cli.pipeline, "collect", return_value=collected):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = cli.run(
                    cli.parse_args(["collect", "--query", "cosmetic", "--db", self.path])
                )
        self.assertEqual(exit_code, 0)
        self.assertIn("2", output.getvalue())

    def test_passes_the_parsed_arguments_through_to_the_pipeline(self):
        self.enterContext(mock.patch.object(cli.http, "contact_email", return_value="a@b.com"))
        self.enterContext(
            mock.patch.dict("os.environ", {"OPENALEX_API_KEY": "test-key"}, clear=False)
        )
        with mock.patch.object(cli.pipeline, "collect", return_value=[]) as collect:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.run(
                    cli.parse_args(
                        [
                            "collect",
                            "--query",
                            "cosmetic retinol",
                            "--from",
                            "2016",
                            "--to",
                            "2026",
                            "--limit",
                            "7",
                            "--db",
                            self.path,
                        ]
                    )
                )
        _, query, year_from, year_to, limit = collect.call_args.args
        self.assertEqual((query, year_from, year_to, limit), ("cosmetic retinol", 2016, 2026, 7))

    def test_warns_when_no_contact_email_is_configured(self):
        with mock.patch.object(cli.http, "contact_email", return_value=""):
            with mock.patch.object(cli.pipeline, "collect", return_value=[]):
                stderr = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.redirect_stderr(stderr):
                        cli.run(
                            cli.parse_args(["collect", "--query", "cosmetic", "--db", self.path])
                        )
        self.assertIn("OPENALEX_EMAIL", stderr.getvalue())

    def test_stays_quiet_about_email_when_one_is_configured(self):
        with mock.patch.object(cli.http, "contact_email", return_value="a@b.com"):
            with mock.patch.object(cli.pipeline, "collect", return_value=[]):
                stderr = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.redirect_stderr(stderr):
                        cli.run(
                            cli.parse_args(["collect", "--query", "cosmetic", "--db", self.path])
                        )
        self.assertNotIn("OPENALEX_EMAIL", stderr.getvalue())

    def test_warns_when_no_openalex_api_key_is_configured(self):
        with mock.patch.dict("os.environ", {"OPENALEX_API_KEY": ""}, clear=False):
            with mock.patch.object(cli.http, "contact_email", return_value="a@b.com"):
                with mock.patch.object(cli.pipeline, "collect", return_value=[]):
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(io.StringIO()):
                        with contextlib.redirect_stderr(stderr):
                            cli.run(
                                cli.parse_args(
                                    ["collect", "--query", "cosmetic", "--db", self.path]
                                )
                            )
        self.assertIn("OPENALEX_API_KEY", stderr.getvalue())
        self.assertIn("1,000크레딧", stderr.getvalue())

    def test_stays_quiet_about_the_api_key_when_one_is_configured(self):
        with mock.patch.dict("os.environ", {"OPENALEX_API_KEY": "secret"}, clear=False):
            with mock.patch.object(cli.http, "contact_email", return_value="a@b.com"):
                with mock.patch.object(cli.pipeline, "collect", return_value=[]):
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(io.StringIO()):
                        with contextlib.redirect_stderr(stderr):
                            cli.run(
                                cli.parse_args(
                                    ["collect", "--query", "cosmetic", "--db", self.path]
                                )
                            )
        self.assertNotIn("OPENALEX_API_KEY", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
