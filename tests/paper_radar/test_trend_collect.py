"""trend.collect: 순수 헬퍼(determine_stopped_reason), 사이드카 id 인덱스,
예산 소진/최대 페이지 중단, RunLog 기록.

papers_trend/tests/test_collect_openalex.py 의 DetermineStoppedReasonTest 를
이식했다(로직 변경 없음). parse_budget_remaining 상당은 이제 별도 함수가
아니라 paper_radar.transport.budget.BudgetTracker 가 맡는다(이미
tests/paper_radar/test_budget.py 가 그 파싱을 검증한다) — trend.collect 는
transport.budget.remaining(host) 를 그대로 읽는다.

나머지는 FakeSession -> Transport 방식(tests.paper_radar.test_evidence_pipeline
과 동일 패턴)으로 collect_profile()/run() 의 페이지 루프·사이드카·RunLog
기록을 검증한다. 실제 네트워크는 쓰지 않는다.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from paper_radar.storage import schema
from paper_radar.storage.runlog import RunLog
from paper_radar.transport.http import Transport
from paper_radar.trend import collect, records
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep


def _page(results, next_cursor=None):
    body = json.dumps({"results": results, "meta": {"next_cursor": next_cursor}}).encode()
    return FakeResponse(200, body=body)


def _count(n):
    return FakeResponse(200, body=json.dumps({"meta": {"count": n}}).encode())


class DetermineStoppedReasonTest(unittest.TestCase):
    def test_is_none_on_normal_completion(self):
        self.assertIsNone(
            collect.determine_stopped_reason(budget_exhausted=False, hit_max_pages=False)
        )

    def test_is_budget_exhausted_when_the_budget_ran_out(self):
        self.assertEqual(
            collect.determine_stopped_reason(budget_exhausted=True, hit_max_pages=False),
            "budget_exhausted",
        )

    def test_is_max_pages_when_the_page_cap_was_hit(self):
        self.assertEqual(
            collect.determine_stopped_reason(budget_exhausted=False, hit_max_pages=True),
            "max_pages",
        )

    def test_budget_exhausted_wins_when_both_happened(self):
        # 페이지 상한에 닿은 바로 그 페이지가 마침 예산도 소진시켰다면,
        # 다음 실행이 마주할 진짜 문제는 예산이다.
        self.assertEqual(
            collect.determine_stopped_reason(budget_exhausted=True, hit_max_pages=True),
            "budget_exhausted",
        )


class _CollectTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._orig_new_raw_root = records.NEW_RAW_ROOT
        records.NEW_RAW_ROOT = self.root / "data" / "raw"
        self.addCleanup(self._restore)
        self.db_path = str(self.root / "papers.db")
        self.config = {
            "window": {"from": "2023-01-01", "to": "2026-12-31"},
            "per_page": 200,
            "limit": None,
            "profiles": {"demo": {"query": "demo"}},
        }

    def _restore(self):
        records.NEW_RAW_ROOT = self._orig_new_raw_root

    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep)

    def _run_log(self):
        """schema 가 적용된 sqlite3 연결과 그 위의 RunLog 를 만든다. addCleanup 으로 닫는다."""
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        schema.migrate(conn)
        return conn, RunLog(conn)


class SidecarTest(_CollectTestCase):
    """있으면 재스캔 생략 / 없으면 전체 재스캔으로 생성 / append 로 누적."""

    def test_builds_the_sidecar_by_rescanning_when_absent(self):
        directory = records.new_raw_dir("demo")
        directory.mkdir(parents=True)
        with open(directory / "2024-01-01.jsonl", "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "https://openalex.org/W1"}) + "\n")
            handle.write(json.dumps({"id": "https://openalex.org/W2"}) + "\n")

        self.assertFalse(collect.ids_path("demo").exists())
        seen = collect.load_seen_ids("demo")
        self.assertEqual(seen, {"https://openalex.org/W1", "https://openalex.org/W2"})
        self.assertTrue(collect.ids_path("demo").exists())
        with open(collect.ids_path("demo"), encoding="utf-8") as handle:
            lines = {line.strip() for line in handle if line.strip()}
        self.assertEqual(lines, seen)

    def test_reads_only_the_sidecar_when_present_without_touching_jsonl(self):
        directory = records.new_raw_dir("demo")
        directory.mkdir(parents=True)
        # jsonl 에는 W3 만 있지만, 사이드카가 있으면 그것만 신뢰한다(재스캔하지
        # 않는다) — 사이드카가 최신이라는 계약을 collect_profile() 이 매 append
        # 마다 지킨다.
        with open(directory / "2024-01-01.jsonl", "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "https://openalex.org/W3"}) + "\n")
        collect._write_ids("demo", "openalex", {"https://openalex.org/W1"})

        seen = collect.load_seen_ids("demo")
        self.assertEqual(seen, {"https://openalex.org/W1"})

    def test_collect_profile_appends_new_ids_to_the_sidecar(self):
        transport = self._transport(
            [
                _count(2),
                _page([{"id": "https://openalex.org/W1"}, {"id": "https://openalex.org/W2"}]),
            ]
        )
        _, run_log = self._run_log()
        collect.collect_profile("demo", "demo", self.config, transport, run_log, verbose=False)

        with open(collect.ids_path("demo"), encoding="utf-8") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        self.assertEqual(lines, ["https://openalex.org/W1", "https://openalex.org/W2"])


class BudgetAndMaxPagesTest(_CollectTestCase):
    def test_stops_cleanly_with_budget_exhausted_on_402(self):
        transport = self._transport([_count(10), FakeResponse(402)])
        _, run_log = self._run_log()
        meta = collect.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )
        self.assertEqual(meta["stopped_reason"], "budget_exhausted")
        self.assertFalse(meta["is_census"])

    def test_stops_cleanly_when_a_successful_page_reports_zero_remaining(self):
        page = _page([{"id": "https://openalex.org/W1"}], next_cursor="next")
        page.headers = {"x-ratelimit-remaining": "0"}
        transport = self._transport([_count(10), page])
        _, run_log = self._run_log()
        meta = collect.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )
        self.assertEqual(meta["stopped_reason"], "budget_exhausted")

    def test_max_pages_stops_and_reports_max_pages(self):
        transport = self._transport(
            [
                _count(10),
                _page([{"id": "https://openalex.org/W1"}], next_cursor="next"),
            ]
        )
        _, run_log = self._run_log()
        meta = collect.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False, max_pages=1
        )
        self.assertEqual(meta["stopped_reason"], "max_pages")
        self.assertEqual(meta["pages_this_run"], 1)


class RunLogWiringTest(_CollectTestCase):
    def test_records_a_run_run_source_and_fetch_log_rows(self):
        transport = self._transport([_count(1), _page([{"id": "https://openalex.org/W1"}])])
        results = collect.run(["demo"], self.config, db_path=self.db_path, transport=transport)
        self.assertEqual(results["demo"]["new_this_run"], 1)

        conn = sqlite3.connect(self.db_path)
        try:
            runs = conn.execute("select command, status from run").fetchall()
            sources = conn.execute("select source, requests, records from run_source").fetchall()
            fetch_count = conn.execute("select count(*) from fetch_log").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(runs, [("trend collect", "ok")])
        self.assertEqual(sources, [("openalex", 2, 1)])
        self.assertEqual(fetch_count, 2)

    def test_multiple_profiles_get_separate_runs(self):
        config = dict(self.config, profiles={"a": {"query": "a"}, "b": {"query": "b"}})
        transport = self._transport(
            [
                _count(1),
                _page([{"id": "https://openalex.org/A1"}]),
                _count(1),
                _page([{"id": "https://openalex.org/B1"}]),
            ]
        )
        collect.run(["a", "b"], config, db_path=self.db_path, transport=transport)
        conn = sqlite3.connect(self.db_path)
        try:
            run_count = conn.execute("select count(*) from run").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(run_count, 2)

    def test_dry_run_never_opens_the_database(self):
        transport = self._transport([_count(42)])
        results = collect.run(
            ["demo"], self.config, db_path=self.db_path, transport=transport, dry_run=True
        )
        self.assertEqual(results["demo"]["count"], 42)
        self.assertFalse(Path(self.db_path).exists())


if __name__ == "__main__":
    unittest.main()
