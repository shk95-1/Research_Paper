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
from paper_radar.models import OaLocationRecord, TrialRecord
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

    def test_trials_collect_requires_a_query(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["trials", "collect"])

    def test_trials_collect_reads_the_query_and_defaults_the_limit(self):
        args = cli.parse_args(["trials", "collect", "--query", "sunscreen"])
        self.assertEqual(args.query, "sunscreen")
        self.assertEqual(args.limit, cli.DEFAULT_LIMIT)

    def test_trials_collect_reads_an_explicit_limit(self):
        args = cli.parse_args(["trials", "collect", "--query", "sunscreen", "--limit", "100"])
        self.assertEqual(args.limit, 100)

    def test_trials_list_requires_a_keyword(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["trials", "list"])

    def test_trials_list_defaults_the_limit_to_twenty(self):
        args = cli.parse_args(["trials", "list", "--keyword", "sunscreen"])
        self.assertEqual(args.limit, 20)

    def test_no_command_under_trials_exits_with_usage(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["trials"])

    def test_trials_help_exits_cleanly(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
            cli.parse_args(["trials", "--help"])
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


def oa_location_record(**overrides):
    base = {
        "doi": "10.1016/j.test.2024.01.001",
        "is_oa": True,
        "oa_status": "hybrid",
        "pdf_url": "https://example.org/article.pdf",
        "landing_url": "https://example.org/landing",
        "host_type": "publisher",
        "license": "cc-by",
        "checked_at": "2026-08-21T00:00:00Z",
    }
    base.update(overrides)
    return OaLocationRecord(**base)


class CitePdfLinkTest(unittest.TestCase):
    """cite 출력의 PDF 링크 줄 — DOI 줄 다음에 있으면 추가, 없으면 기존 출력 불변."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _cite(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.run(cli.parse_args(argv + ["--db", self.path]))
        return exit_code, output.getvalue()

    def test_prints_a_pdf_line_right_after_the_doi_line_when_a_link_is_known(self):
        conn = repository.connect(self.path)
        repository.upsert(conn, record(), verification())
        repository.upsert_records(conn, [oa_location_record()])
        conn.close()

        _, text = self._cite(["evidence", "cite", "--keyword", "retinol"])
        doi_index = text.index("https://doi.org/10.1016/j.test.2024.01.001")
        pdf_index = text.index("PDF: https://example.org/article.pdf")
        self.assertGreater(pdf_index, doi_index, "PDF 줄은 DOI 줄 다음에 와야 한다")

    def test_prints_nothing_extra_when_no_oa_location_is_known_for_the_doi(self):
        conn = repository.connect(self.path)
        repository.upsert(conn, record(), verification())
        conn.close()

        _, text = self._cite(["evidence", "cite", "--keyword", "retinol"])
        self.assertNotIn("PDF:", text)

    def test_prints_nothing_extra_when_the_known_oa_location_has_no_pdf_url(self):
        conn = repository.connect(self.path)
        repository.upsert(conn, record(), verification())
        repository.upsert_records(
            conn,
            [
                oa_location_record(
                    is_oa=False, oa_status="closed", pdf_url=None, landing_url=None,
                    host_type=None, license=None,
                )
            ],
        )
        conn.close()

        _, text = self._cite(["evidence", "cite", "--keyword", "retinol"])
        self.assertNotIn("PDF:", text)


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


def trials_report(records=None, stopped_reason=None, status="ok"):
    """cli.trials_collect(paper_radar.trials.collect 별칭)의 TrialsReport.
    run/run_source 행·부분 결과 보존 등 파이프라인 레벨 검증은
    tests/paper_radar/test_trials_collect.py 로 옮겼다 — 여기(test_cli.py)는
    evidence collect 의 CollectOutputTest 와 같은 결로 인자 전달·출력·exit
    code 만 본다(trials_collect.run() 을 mock.patch 로 대체)."""
    return cli.trials_collect.TrialsReport(
        run_id="test-run", records=records or [], stopped_reason=stopped_reason, status=status
    )


def trial_record(**overrides):
    base = {
        "nct_id": "NCT01234567",
        "title": "SPF50 sunscreen trial",
        "status": "COMPLETED",
        "phase": "PHASE3",
        "sponsor_class": "INDUSTRY",
        "enrollment": 120,
        "conditions": ("Sunburn",),
        "interventions": ("SPF50 sunscreen",),
        "outcomes_json": "{}",
        "first_posted": "2023-05-01",
        "results_posted": True,
        "url": "https://clinicaltrials.gov/study/NCT01234567",
        "matched_query": "sunscreen",
        "captured_at": "2026-08-21T00:00:00Z",
    }
    base.update(overrides)
    return TrialRecord(**base)


class TrialsCollectCliTest(unittest.TestCase):
    """`trials collect` 의 인자 전달·출력·exit code — trials_collect.run() 을
    mock.patch 로 대체해 CollectOutputTest(evidence.pipeline.collect() 를
    대체하는 것과 같은 결)로 검증한다."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _collect(self, argv, collect_return):
        output = io.StringIO()
        with (
            mock.patch.object(cli.trials_collect, "run", return_value=collect_return) as collect,
            contextlib.redirect_stdout(output),
        ):
            exit_code = cli.run(cli.parse_args(argv + ["--db", self.path]))
        return exit_code, output.getvalue(), collect

    def test_passes_the_parsed_query_and_limit_through(self):
        _, _, collect = self._collect(
            ["trials", "collect", "--query", "sunscreen", "--limit", "50"],
            trials_report(),
        )
        _, _, query, limit = collect.call_args.args
        self.assertEqual((query, limit), ("sunscreen", 50))

    def test_reports_how_many_trials_were_stored(self):
        exit_code, text, _ = self._collect(
            ["trials", "collect", "--query", "sunscreen"],
            trials_report(records=[trial_record(), trial_record(nct_id="NCT2")]),
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("2", text)

    def test_returns_zero_for_a_fully_ok_report(self):
        exit_code, _, _ = self._collect(
            ["trials", "collect", "--query", "sunscreen"], trials_report(status="ok")
        )
        self.assertEqual(exit_code, 0)

    def test_returns_one_when_the_report_is_partial(self):
        exit_code, _, _ = self._collect(
            ["trials", "collect", "--query", "sunscreen"],
            trials_report(stopped_reason="budget_exhausted", status="partial"),
        )
        self.assertEqual(exit_code, 1)

    def test_exit_code_follows_report_status_rather_than_recomputing_it(self):
        """partial/ok 판정은 trials_collect.run() 한 곳에서만 계산한다 — CLI 는
        report.status 를 그대로 옮길 뿐, stopped_reason 을 다시 훑어
        재계산하지 않는다. stopped_reason 이 없어도 status="partial" 이면
        여전히 exit 1 이어야 한다."""
        exit_code, _, _ = self._collect(
            ["trials", "collect", "--query", "sunscreen"],
            trials_report(status="partial"),  # stopped_reason=None
        )
        self.assertEqual(exit_code, 1)

    def test_prints_a_progress_line_with_nct_id_phase_sponsor_class_and_title(self):
        def fake_collect(conn, transport, query, limit, *, on_progress=None):
            if on_progress:
                on_progress(1, 1, trial_record())
            return trials_report(records=[trial_record()])

        output = io.StringIO()
        with (
            mock.patch.object(cli.trials_collect, "run", side_effect=fake_collect),
            contextlib.redirect_stdout(output),
        ):
            cli.run(
                cli.parse_args(
                    ["trials", "collect", "--query", "sunscreen", "--db", self.path]
                )
            )
        text = output.getvalue()
        self.assertIn("NCT01234567", text)
        self.assertIn("PHASE3", text)
        self.assertIn("INDUSTRY", text)
        self.assertIn("SPF50 sunscreen trial", text)


class TrialsListCliTest(unittest.TestCase):
    """`trials list` — 로컬 DB 만 읽는다(네트워크 없음)."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _list(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli.run(cli.parse_args(argv + ["--db", self.path]))
        return exit_code, output.getvalue()

    def test_prints_the_matching_trial_with_its_key_fields(self):
        conn = repository.connect(self.path)
        repository.upsert_records(conn, [trial_record()])
        conn.close()

        exit_code, text = self._list(["trials", "list", "--keyword", "sunscreen"])
        self.assertEqual(exit_code, 0)
        self.assertIn("NCT01234567", text)
        self.assertIn("SPF50 sunscreen trial", text)
        self.assertIn("COMPLETED", text)
        self.assertIn("PHASE3", text)
        self.assertIn("INDUSTRY", text)
        self.assertIn("120", text)
        self.assertIn("결과 게시됨", text)
        self.assertIn("https://clinicaltrials.gov/study/NCT01234567", text)

    def test_omits_the_results_posted_line_when_no_results_are_posted(self):
        conn = repository.connect(self.path)
        repository.upsert_records(conn, [trial_record(results_posted=False)])
        conn.close()

        _, text = self._list(["trials", "list", "--keyword", "sunscreen"])
        self.assertNotIn("결과 게시됨", text)

    def test_reports_when_the_keyword_matches_nothing(self):
        exit_code, text = self._list(["trials", "list", "--keyword", "niacinamide"])
        self.assertEqual(exit_code, 0)
        self.assertIn("결과가 없습니다", text)


if __name__ == "__main__":
    unittest.main()
