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

import contextlib
import io
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

    def test_is_transport_error_when_a_transport_error_stopped_the_run(self):
        # 리뷰 대응(Finding 1): TransportError 중단도 stopped_reason 을 남겨야
        # RunLog 상태/CLI exit 코드가 정상 완료와 구분된다.
        self.assertEqual(
            collect.determine_stopped_reason(
                budget_exhausted=False, transport_error=True, hit_max_pages=False
            ),
            "transport_error",
        )

    def test_budget_exhausted_wins_over_transport_error(self):
        self.assertEqual(
            collect.determine_stopped_reason(
                budget_exhausted=True, transport_error=True, hit_max_pages=False
            ),
            "budget_exhausted",
        )

    def test_transport_error_wins_over_max_pages(self):
        self.assertEqual(
            collect.determine_stopped_reason(
                budget_exhausted=False, transport_error=True, hit_max_pages=True
            ),
            "transport_error",
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


class ProgressOutputTest(_CollectTestCase):
    """리뷰 finding: 레거시 collect_openalex.py 는 페이지별 진행 출력에 경과
    초를 포함했다(`f"...({pages}페이지, {elapsed:.0f}초){remaining_note}"`,
    time.monotonic() 기반). 이식 과정에서 조용히 빠졌던 것을 복원한다 — 전체
    문자열을 통째로 고정하면(초 값 자체가 비결정적) 취약하므로, 형식의
    핵심(페이지 수 뒤에 "N초)" 패턴이 온다)만 고정한다.
    """

    def test_page_progress_line_includes_elapsed_seconds(self):
        transport = self._transport([_count(1), _page([{"id": "https://openalex.org/W1"}])])
        _, run_log = self._run_log()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            collect.collect_profile("demo", "demo", self.config, transport, run_log, verbose=True)

        text = output.getvalue()
        self.assertRegex(text, r"\(1페이지, \d+초\)")


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

    def test_a_transport_error_mid_run_reports_transport_error_and_a_partial_run(self):
        """리뷰 대응(Finding 1): 페이지 수집 중 TransportError(예: 5xx 재시도
        소진)로 멈춰도 이전에는 stopped_reason 이 None 이 되어 RunLog 상태가
        "ok", CLI exit 코드가 0 으로 기록됐다 — README 의 exit-code 규약
        ("수집이 중간에 멈추면 exit 1")과 어긋났다. 5xx 를 max_attempts(5)회
        반복시켜 TransientError(TransportError 의 하위클래스)를 유발한다."""
        transport = self._transport([_count(10), *[FakeResponse(503) for _ in range(5)]])
        conn, run_log = self._run_log()
        meta = collect.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )
        self.assertEqual(meta["stopped_reason"], "transport_error")
        self.assertFalse(meta["is_census"])
        status = conn.execute("select status from run").fetchone()
        self.assertEqual(status, ("partial",))
        source_reason = conn.execute("select stopped_reason from run_source").fetchone()
        self.assertEqual(source_reason, ("transport_error",))


class CensusCompletionTest(_CollectTestCase):
    """항목1 회귀: cursor=None 완료 상태가 재실행 때 "*" 로 되살아나면서
    무변화 재실행이 표본으로 뒤집히던 버그. complete/complete_window 로
    already_complete 를 판정에 더해 고친다(collect.py 의 새 주석 참고).
    """

    def test_a_no_op_rerun_after_a_completed_census_stays_a_census(self):
        # 이게 회귀의 핵심: 새 논문이 0건인 재실행이어도 is_census 가 True
        # 로 유지돼야 한다 — _meta.json 이 표본으로 뒤집히면 aggregate() 의
        # census 가드가 산출물 생성을 거부한다.
        transport1 = self._transport([_count(1), _page([{"id": "https://openalex.org/W1"}])])
        _, run_log = self._run_log()
        meta1 = collect.collect_profile(
            "demo", "demo", self.config, transport1, run_log, verbose=False
        )
        self.assertTrue(meta1["is_census"])
        state1 = collect.load_state("demo")
        self.assertTrue(state1["complete"])
        self.assertEqual(state1["complete_window"], self.config["window"])

        # 두 번째 실행 — 건수 조회 응답 하나만 준다. 만약 페이지를 다시
        # 요청하면 FakeSession 이 "예상보다 많이 호출되었습니다" 로 실패한다
        # (무변화 재실행은 while 루프 자체가 건너뛰어져야 한다).
        transport2 = self._transport([_count(1)])
        meta2 = collect.collect_profile(
            "demo", "demo", self.config, transport2, run_log, verbose=False
        )
        self.assertEqual(meta2["new_this_run"], 0)
        self.assertTrue(meta2["is_census"])

    def test_shrinking_the_window_after_completion_still_recognizes_a_census(self):
        # 리뷰 Finding1(라이브 재현) 회귀: 창을 줄이면 이미 사이드카에 쌓인
        # already 가 새(더 작은) target 을 이미 넘어서서 while 루프 자체가
        # 통째로 건너뛰어진다 — cursor 가 "*" 인 채로 영원히 남아
        # `cursor is None` 판정만으로는 표본으로 영구 고착됐다(holds_target
        # 이 이 경로를 완료로 인정해 고친다).
        transport1 = self._transport(
            [
                _count(2),
                _page(
                    [
                        {"id": "https://openalex.org/W1"},
                        {"id": "https://openalex.org/W2"},
                    ]
                ),
            ]
        )
        _, run_log = self._run_log()
        meta1 = collect.collect_profile(
            "demo", "demo", self.config, transport1, run_log, verbose=False
        )
        self.assertTrue(meta1["is_census"])

        # 창을 좁힌다 — 새 target(1)이 이미 보유한 already(2)보다 작다.
        self.config["window"] = dict(self.config["window"], to="2023-06-30")
        transport2 = self._transport([_count(1)])
        meta2 = collect.collect_profile(
            "demo", "demo", self.config, transport2, run_log, verbose=False
        )
        self.assertTrue(meta2["is_census"])
        state2 = collect.load_state("demo")
        self.assertTrue(state2["complete"])
        self.assertEqual(state2["complete_window"], self.config["window"])

        # 한 번 더 재실행해도 True 를 유지한다(무변화 no-op — 건수 조회 응답
        # 하나만 준다. 페이지를 다시 요청하면 FakeSession 이 실패한다).
        transport3 = self._transport([_count(1)])
        meta3 = collect.collect_profile(
            "demo", "demo", self.config, transport3, run_log, verbose=False
        )
        self.assertTrue(meta3["is_census"])

    def test_extending_the_window_after_completion_recollects_and_updates_complete_window(self):
        transport1 = self._transport([_count(1), _page([{"id": "https://openalex.org/W1"}])])
        _, run_log = self._run_log()
        meta1 = collect.collect_profile(
            "demo", "demo", self.config, transport1, run_log, verbose=False
        )
        self.assertTrue(meta1["is_census"])

        self.config["window"] = dict(self.config["window"], to="2027-12-31")
        transport2 = self._transport(
            [
                _count(2),
                _page(
                    [
                        {"id": "https://openalex.org/W1"},  # 이미 있음 — 중복
                        {"id": "https://openalex.org/W2"},  # 신규
                    ]
                ),
            ]
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            meta2 = collect.collect_profile(
                "demo", "demo", self.config, transport2, run_log, verbose=False
            )
        self.assertIn("window", stderr.getvalue())
        self.assertIn("바뀌었습니다", stderr.getvalue())
        self.assertEqual(meta2["new_this_run"], 1)
        self.assertTrue(meta2["is_census"])
        state2 = collect.load_state("demo")
        self.assertEqual(state2["complete_window"], self.config["window"])

    def test_a_mid_run_interruption_is_still_not_a_census(self):
        # 기존 동작 불변: 완료 이전이든 이후든, 이번 실행이 예산/transport
        # 오류로 중단되면 is_census 는 False 다.
        transport = self._transport([_count(10), FakeResponse(402)])
        _, run_log = self._run_log()
        meta = collect.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )
        self.assertFalse(meta["is_census"])

    def test_backfill_growth_after_completion_that_hits_budget_clears_the_stale_complete_flag(
        self,
    ):
        # 같은 window 안에서 OpenAlex 가 소급 색인해 모집단이 늘어난 뒤(항목1
        # docstring 참고), 재실행이 다시 예산 소진으로 중단되면 이번 실행은
        # 전수가 아니다 — state["complete"] 도 True 로 남아 있으면 안 된다
        # (남아 있으면 그다음 재실행이 "이미 완료됨" 으로 잘못 판정한다).
        transport1 = self._transport([_count(1), _page([{"id": "https://openalex.org/W1"}])])
        _, run_log = self._run_log()
        meta1 = collect.collect_profile(
            "demo", "demo", self.config, transport1, run_log, verbose=False
        )
        self.assertTrue(meta1["is_census"])

        transport2 = self._transport([_count(2), FakeResponse(402)])
        meta2 = collect.collect_profile(
            "demo", "demo", self.config, transport2, run_log, verbose=False
        )
        self.assertFalse(meta2["is_census"])
        self.assertEqual(meta2["stopped_reason"], "budget_exhausted")
        state2 = collect.load_state("demo")
        self.assertFalse(state2["complete"])

    def test_loading_an_old_schema_state_file_defaults_the_new_fields(self):
        """구 상태 파일(complete/complete_window 도입 이전)을 읽어도 무해하다."""
        directory = records.new_raw_dir("demo")
        directory.mkdir(parents=True)
        with open(directory / "_state.json", "w", encoding="utf-8") as handle:
            json.dump({"cursor": None, "pages": 1, "written": 1}, handle)

        state = collect.load_state("demo")
        self.assertFalse(state["complete"])
        self.assertIsNone(state["complete_window"])


class LimitRunNeverPersistsCompleteTest(_CollectTestCase):
    """리뷰 Finding A(재리뷰 재현): holds_target 저장이 is_census 와 같은
    `not limit` 게이트를 거치지 않으면, limit 실행이 부분 데이터를 완료로
    디스크에 남긴다 — 그 뒤 limit 없는 실행이 그 낡은 플래그를 물려받아
    (already_complete) 부분 데이터만 가진 채 is_census: True 를 보고할 수
    있다.
    """

    def test_a_limited_run_reports_a_sample_and_does_not_persist_complete(self):
        # 모집단 10건 중 limit=2 만 받는다. len(already)=2 는 target(=2) 을
        # 채우지만(holds_target 성립) cursor 는 아직 살아 있다("다음 페이지"
        # 커서) — 두 조건 다 limit 실행에서는 "전 모집단을 받았다"는 뜻이
        # 아니다.
        self.config["limit"] = 2
        transport = self._transport(
            [
                _count(10),
                _page(
                    [
                        {"id": "https://openalex.org/W1"},
                        {"id": "https://openalex.org/W2"},
                    ],
                    next_cursor="cursor-after-page-1",
                ),
            ]
        )
        _, run_log = self._run_log()
        meta = collect.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )
        self.assertEqual(meta["collected"], 2)
        # (a) is_census 는 False 다.
        self.assertFalse(meta["is_census"])
        # (b) state 에 complete 가 저장되지 않는다 — 다음(limit 없는) 실행이
        # already_complete 로 이 부분 데이터를 전수로 오인하면 안 된다.
        state = collect.load_state("demo")
        self.assertFalse(state["complete"])
        self.assertIsNone(state["complete_window"])


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


class WindowToOverrideTest(_CollectTestCase):
    """항목2: --window-to 가 실제 요청 필터·_meta.json·원본 config 불변에 반영되는지."""

    def test_window_to_overrides_the_to_publication_date_filter(self):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession([_count(1), _page([{"id": "https://openalex.org/W1"}])])
        transport = Transport(session=session, clock=clock, sleep=sleep)
        original_to = self.config["window"]["to"]
        results = collect.run(
            ["demo"], self.config, db_path=self.db_path, transport=transport, window_to="2024-06-30"
        )

        filter_value = dict(session.calls[0]["params"])["filter"]
        self.assertIn("to_publication_date:2024-06-30", filter_value)
        self.assertEqual(results["demo"]["window"]["to"], "2024-06-30")
        # 호출자가 넘긴 원본 config 딕셔너리는 건드리지 않는다(config.json 을
        # 쓰지 않는다는 브리핑 제약과 같은 방향 — 여기서는 인메모리 원본도 그대로).
        self.assertEqual(self.config["window"]["to"], original_to)

    def test_without_window_to_the_existing_window_is_unchanged(self):
        transport = self._transport([_count(1), _page([{"id": "https://openalex.org/W1"}])])
        results = collect.run(["demo"], self.config, db_path=self.db_path, transport=transport)
        self.assertEqual(results["demo"]["window"]["to"], self.config["window"]["to"])


if __name__ == "__main__":
    unittest.main()
