"""trend.collect_pubmed: 순수 헬퍼(month_list/month_bounds/determine_stopped_reason),
월별 esearch(retstart 페이지네이션) + efetch 배치 수집, 사이드카 dedup, _meta.json.

tests/paper_radar/test_trend_collect.py 와 같은 방식(FakeSession -> Transport)
으로 collect_profile()/run() 을 검증한다. 실제 네트워크는 쓰지 않는다.
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
from paper_radar.trend import collect_pubmed, records
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep


def _esearch(pmids, count):
    body = json.dumps({"esearchresult": {"idlist": pmids, "count": str(count)}}).encode()
    return FakeResponse(200, body=body)


def _efetch(pmids):
    """PubmedArticleSet 다건 XML — pmid 마다 최소한의 Article + MeshHeadingList."""
    articles = "".join(
        f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID>"
        f"<Article><ArticleTitle>Title {pmid}</ArticleTitle></Article>"
        f"<MeshHeadingList><MeshHeading><DescriptorName>Sunscreening Agents"
        f"</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation>"
        f"<PubmedData><ArticleIdList><ArticleId IdType=\"doi\">10.1/{pmid}"
        f"</ArticleId></ArticleIdList></PubmedData></PubmedArticle>"
        for pmid in pmids
    )
    return FakeResponse(200, body=f"<PubmedArticleSet>{articles}</PubmedArticleSet>".encode())


class MonthListTest(unittest.TestCase):
    def test_splits_a_multi_month_window_inclusive_of_both_ends(self):
        window = {"from": "2023-09-01", "to": "2023-11-30"}
        self.assertEqual(collect_pubmed.month_list(window), ["2023-09", "2023-10", "2023-11"])

    def test_a_single_month_window_returns_one_month(self):
        window = {"from": "2024-01-01", "to": "2024-01-31"}
        self.assertEqual(collect_pubmed.month_list(window), ["2024-01"])


class MonthBoundsTest(unittest.TestCase):
    def test_uses_the_full_calendar_month_for_a_middle_month(self):
        window = {"from": "2023-09-01", "to": "2023-11-30"}
        self.assertEqual(
            collect_pubmed.month_bounds("2023-10", window), ("2023-10-01", "2023-10-31")
        )

    def test_clips_the_first_month_to_the_window_start(self):
        window = {"from": "2023-09-15", "to": "2023-11-30"}
        self.assertEqual(
            collect_pubmed.month_bounds("2023-09", window), ("2023-09-15", "2023-09-30")
        )

    def test_clips_the_last_month_to_the_window_end(self):
        window = {"from": "2023-09-01", "to": "2023-11-15"}
        self.assertEqual(
            collect_pubmed.month_bounds("2023-11", window), ("2023-11-01", "2023-11-15")
        )


class DetermineStoppedReasonTest(unittest.TestCase):
    def test_is_none_on_normal_completion(self):
        self.assertIsNone(
            collect_pubmed.determine_stopped_reason(budget_exhausted=False, hit_max_months=False)
        )

    def test_is_budget_exhausted_when_the_budget_ran_out(self):
        self.assertEqual(
            collect_pubmed.determine_stopped_reason(budget_exhausted=True, hit_max_months=False),
            "budget_exhausted",
        )

    def test_is_max_months_when_the_month_cap_was_hit(self):
        self.assertEqual(
            collect_pubmed.determine_stopped_reason(budget_exhausted=False, hit_max_months=True),
            "max_months",
        )

    def test_budget_exhausted_wins_when_both_happened(self):
        self.assertEqual(
            collect_pubmed.determine_stopped_reason(budget_exhausted=True, hit_max_months=True),
            "budget_exhausted",
        )


class _CollectPubmedTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._orig_new_raw_root = records.NEW_RAW_ROOT
        records.NEW_RAW_ROOT = self.root / "data" / "raw"
        self.addCleanup(self._restore)
        self.db_path = str(self.root / "papers.db")
        self.config = {
            "window": {"from": "2024-01-01", "to": "2024-01-31"},
            "profiles": {"demo": {"pubmed_query": "demo"}},
        }

    def _restore(self):
        records.NEW_RAW_ROOT = self._orig_new_raw_root

    def _transport(self, responses):
        clock, sleep, calls = make_clock_and_sleep()
        self.sleep_calls = calls
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def _run_log(self):
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        schema.migrate(conn)
        return conn, RunLog(conn)

    def _jsonl_lines(self, query_id="demo"):
        path = collect_pubmed.jsonl_path(query_id)
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class SingleMonthCollectTest(_CollectPubmedTestCase):
    def test_collects_a_full_month_and_marks_it_a_census(self):
        transport, session = self._transport(
            [_esearch(["1", "2"], 2), _efetch(["1", "2"])]
        )
        _, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )

        self.assertEqual(meta["collected"], 2)
        self.assertEqual(meta["new_this_run"], 2)
        self.assertEqual(meta["duplicates_skipped"], 0)
        self.assertIsNone(meta["stopped_reason"])
        self.assertTrue(meta["is_census"])
        self.assertEqual(meta["months"]["2024-01"]["expected"], 2)
        self.assertEqual(meta["provider"], "pubmed")

        lines = self._jsonl_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual({line["pmid"] for line in lines}, {"1", "2"})
        self.assertTrue(all(line["month_bucket"] == "2024-01" for line in lines))
        self.assertTrue(all(line["provider"] == "pubmed" for line in lines))
        self.assertEqual(lines[0]["mesh_terms"], ["Sunscreening Agents"])

        with open(collect_pubmed.ids_path("demo"), encoding="utf-8") as handle:
            sidecar = {line.strip() for line in handle if line.strip()}
        self.assertEqual(sidecar, {"1", "2"})

    def test_a_second_run_after_a_completed_census_does_not_repeat_esearch(self):
        # T15 리뷰 대응: month_cursor=None 은 "시작 전"과 "전 구간 완료" 양쪽에
        # 다 해당할 수 있어 complete 플래그로 구분한다(collect_pubmed.load_state
        # docstring 참고) — 구분하지 않으면 이미 끝난 창을 다시 실행할 때마다
        # esearch 를 반복하게 된다.
        transport, session = self._transport([_esearch(["1"], 1), _efetch(["1"])])
        _, run_log = self._run_log()
        collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )
        self.assertEqual(len(session.calls), 2)

        # 두 번째 실행 — 응답을 하나도 주지 않는다. 만약 esearch 를 다시
        # 호출하면 FakeSession 이 "예상보다 많이 호출되었습니다" 로 실패한다.
        transport2, session2 = self._transport([])
        meta2 = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport2, run_log, verbose=False
        )
        self.assertEqual(session2.calls, [])
        self.assertEqual(meta2["new_this_run"], 0)
        self.assertTrue(meta2["is_census"])


class BatchSplitTest(_CollectPubmedTestCase):
    def test_splits_efetch_into_chunks_of_at_most_efetch_batch_max(self):
        pmids = [str(n) for n in range(1, 251)]  # 250건 — EFETCH_BATCH_MAX(200) 초과
        transport, session = self._transport(
            [_esearch(pmids, 250), _efetch(pmids[:200]), _efetch(pmids[200:])]
        )
        _, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )

        self.assertEqual(meta["collected"], 250)
        # esearch 1회 + efetch 2회(200 + 50) = 3 요청
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[1]["params"]["id"].count(","), 199)  # 200개
        self.assertEqual(session.calls[2]["params"]["id"].count(","), 49)  # 50개


class SidecarDedupTest(_CollectPubmedTestCase):
    def test_a_pmid_already_in_the_sidecar_is_not_written_again(self):
        collect_pubmed.profile_dir("demo").mkdir(parents=True)
        collect_pubmed._write_ids("demo", {"1"})

        transport, session = self._transport([_esearch(["1", "2"], 2), _efetch(["2"])])
        _, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )

        self.assertEqual(meta["collected"], 1)  # pmid 2 만 신규
        self.assertEqual(meta["duplicates_skipped"], 1)  # pmid 1 은 중복으로 센다
        lines = self._jsonl_lines()
        self.assertEqual([line["pmid"] for line in lines], ["2"])


class MonthBoundaryCollectTest(_CollectPubmedTestCase):
    def test_walks_every_month_in_the_window_with_separate_date_ranged_esearch(self):
        self.config["window"] = {"from": "2024-01-01", "to": "2024-02-29"}
        transport, session = self._transport(
            [
                _esearch(["1"], 1),
                _efetch(["1"]),
                _esearch(["2"], 1),
                _efetch(["2"]),
            ]
        )
        _, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )

        self.assertEqual(set(meta["months"]), {"2024-01", "2024-02"})
        self.assertEqual(session.calls[0]["params"]["mindate"], "2024-01-01")
        self.assertEqual(session.calls[0]["params"]["maxdate"], "2024-01-31")
        self.assertEqual(session.calls[2]["params"]["mindate"], "2024-02-01")
        self.assertEqual(session.calls[2]["params"]["maxdate"], "2024-02-29")
        self.assertTrue(meta["is_census"])


class BudgetAndMaxMonthsTest(_CollectPubmedTestCase):
    def test_stops_cleanly_with_budget_exhausted_on_402_and_saves_the_month_cursor(self):
        self.config["window"] = {"from": "2024-01-01", "to": "2024-02-29"}
        transport, session = self._transport([FakeResponse(402)])
        _, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )
        self.assertEqual(meta["stopped_reason"], "budget_exhausted")
        self.assertFalse(meta["is_census"])
        state = collect_pubmed.load_state("demo")
        self.assertEqual(state["month_cursor"], "2024-01")
        self.assertFalse(state["complete"])

    def test_max_months_stops_after_the_first_month_and_reports_max_months(self):
        self.config["window"] = {"from": "2024-01-01", "to": "2024-02-29"}
        transport, session = self._transport([_esearch(["1"], 1), _efetch(["1"])])
        _, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False, max_months=1
        )
        self.assertEqual(meta["stopped_reason"], "max_months")
        self.assertEqual(meta["months_this_run"], 1)
        self.assertFalse(meta["is_census"])


class RateLimitBackoffTest(_CollectPubmedTestCase):
    def test_a_429_is_retried_transparently_by_transport(self):
        # 이 모듈은 429 를 위한 별도 코드를 갖지 않는다 — transport.http.Transport
        # 의 재시도/백오프가 처리한다(모듈 docstring 참고). FakeSession 이
        # 429 -> 200 순서로 응답하면 collect_profile() 이 그 재시도 뒤의
        # 성공 응답을 그대로 쓸 수 있어야 한다.
        transport, session = self._transport(
            [FakeResponse(429, headers={"retry-after": "1"}), _esearch(["1"], 1), _efetch(["1"])]
        )
        _, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )
        self.assertEqual(meta["collected"], 1)
        self.assertIsNone(meta["stopped_reason"])
        self.assertTrue(self.sleep_calls)  # 백오프가 실제로 걸렸다(가짜 sleep 이 기록됨)


class RunLogWiringTest(_CollectPubmedTestCase):
    def test_records_a_run_run_source_and_fetch_log_rows(self):
        transport = self._transport([_esearch(["1"], 1), _efetch(["1"])])[0]
        results = collect_pubmed.run(
            ["demo"], self.config, db_path=self.db_path, transport=transport
        )
        self.assertEqual(results["demo"]["new_this_run"], 1)

        conn = sqlite3.connect(self.db_path)
        try:
            runs = conn.execute("select command, status from run").fetchall()
            sources = conn.execute("select source, requests, records from run_source").fetchall()
        finally:
            conn.close()
        self.assertEqual(runs, [("trend collect", "ok")])
        self.assertEqual(sources, [("pubmed", 2, 1)])

    def test_dry_run_never_opens_the_database(self):
        transport = self._transport([_esearch(["1"], 1)])[0]
        results = collect_pubmed.run(
            ["demo"], self.config, db_path=self.db_path, transport=transport, dry_run=True
        )
        self.assertEqual(results["demo"]["count"], 1)
        self.assertFalse(Path(self.db_path).exists())


if __name__ == "__main__":
    unittest.main()
