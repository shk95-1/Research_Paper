"""trend.collect_pubmed: 순수 헬퍼(month_list/month_bounds/determine_stopped_reason),
월별 esearch(retstart 페이지네이션) + efetch 배치 수집, 사이드카 dedup, _meta.json.

tests/paper_radar/test_trend_collect.py 와 같은 방식(FakeSession -> Transport)
으로 collect_profile()/run() 을 검증한다. 실제 네트워크는 쓰지 않는다.
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

    def test_is_transport_error_when_a_transport_error_stopped_the_run(self):
        # 리뷰 대응(Finding 1): _fetch_month() 의 "transport_error" reason 이
        # 여기까지 살아남아야 RunLog 상태/CLI exit 코드가 정상 완료와 구분된다.
        self.assertEqual(
            collect_pubmed.determine_stopped_reason(
                budget_exhausted=False, transport_error=True, hit_max_months=False
            ),
            "transport_error",
        )

    def test_budget_exhausted_wins_over_transport_error(self):
        self.assertEqual(
            collect_pubmed.determine_stopped_reason(
                budget_exhausted=True, transport_error=True, hit_max_months=False
            ),
            "budget_exhausted",
        )

    def test_transport_error_wins_over_max_months(self):
        self.assertEqual(
            collect_pubmed.determine_stopped_reason(
                budget_exhausted=False, transport_error=True, hit_max_months=True
            ),
            "transport_error",
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


class MidPageFailureResumeTest(_CollectPubmedTestCase):
    """리뷰 Finding 1 회귀 테스트: 한 esearch 페이지(500개 이내)가 여러 efetch
    청크(200개씩)로 나뉠 때, 청크 중간에 실패해도 그 페이지의 retstart 를
    전진시키지 않는다 — 그래서 재실행이 같은 페이지를 다시 esearch 하고,
    사이드카 dedup 덕에 이미 받은 청크는 재요청하지 않으면서 실패/미시도
    청크만 재시도해 결국 census 를 완성한다(수정 전에는 실패 청크의 PMID
    가 영원히 재시도되지 않아 expected > collected+duplicates 가 고착됐다).
    """

    def test_a_chunk_failure_mid_page_does_not_advance_retstart_and_a_rerun_completes_the_census(
        self,
    ):
        pmids = [str(n) for n in range(1, 251)]  # 250건 -> efetch 청크 200 + 50

        # 1차: esearch 로 250개를 받고, 첫 청크(200)는 성공, 두 번째 청크(50)에서
        # 예산 소진.
        transport1, session1 = self._transport(
            [_esearch(pmids, 250), _efetch(pmids[:200]), FakeResponse(402)]
        )
        _, run_log = self._run_log()
        meta1 = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport1, run_log, verbose=False
        )
        self.assertEqual(meta1["stopped_reason"], "budget_exhausted")
        self.assertEqual(meta1["collected"], 200)  # 첫 청크만 반영
        self.assertFalse(meta1["is_census"])

        state = collect_pubmed.load_state("demo")
        self.assertEqual(state["month_cursor"], "2024-01")
        # 핵심 검증: retstart 가 전진하지 않았다(0 그대로) — 페이지 전체를 다시 esearch 한다.
        self.assertEqual(state["retstart"], 0)

        # 2차: 같은 페이지를 다시 esearch(같은 250개 응답). 이미 받은 200개는
        # already(사이드카)에 걸려 새 efetch 대상에서 빠지고, 나머지 50개만
        # 청크 하나로 재시도된다.
        transport2, session2 = self._transport([_esearch(pmids, 250), _efetch(pmids[200:])])
        meta2 = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport2, run_log, verbose=False
        )
        self.assertIsNone(meta2["stopped_reason"])
        self.assertEqual(meta2["collected"], 250)  # 누적 — 이번에 나머지 50건이 채워졌다
        self.assertTrue(meta2["is_census"])
        # esearch 1회 + efetch 1회(남은 50개) = 2 요청. 이미 성공한 200개 청크를
        # 다시 efetch 하지 않았다는 것도 이 호출 수로 확인된다.
        self.assertEqual(len(session2.calls), 2)
        self.assertEqual(session2.calls[1]["params"]["id"].count(","), 49)  # 50개

        lines = self._jsonl_lines()
        self.assertEqual(len(lines), 250)  # 200 + 50, 중복 기록 없음
        self.assertEqual(len({line["pmid"] for line in lines}), 250)


class WindowExtensionAfterCompletionTest(_CollectPubmedTestCase):
    """리뷰 Finding 2 회귀 테스트: 전 구간을 이미 완료한 뒤 window.to 를
    늘려(가장 흔한 운영 행위) 재실행하면, 이미 끝낸 달은 다시 esearch 하지
    않고 새로 늘어난 달만 수집해 census 를 유지한다.
    """

    def test_extending_the_window_after_a_completed_census_only_collects_the_new_month(self):
        self.config["window"] = {"from": "2024-01-01", "to": "2024-02-29"}
        transport1, session1 = self._transport(
            [_esearch(["1"], 1), _efetch(["1"]), _esearch(["2"], 1), _efetch(["2"])]
        )
        _, run_log = self._run_log()
        meta1 = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport1, run_log, verbose=False
        )
        self.assertTrue(meta1["is_census"])
        state = collect_pubmed.load_state("demo")
        self.assertTrue(state["complete"])
        self.assertEqual(state["complete_through_month"], "2024-02")

        # window.to 를 3월까지 늘린다 — Jan/Feb 는 다시 esearch 하지 않아야 한다
        # (응답 목록에 3월분만 넣는다 — FakeSession 이 예상보다 많이 불리면 실패한다).
        self.config["window"]["to"] = "2024-03-31"
        transport2, session2 = self._transport([_esearch(["3"], 1), _efetch(["3"])])
        meta2 = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport2, run_log, verbose=False
        )

        self.assertEqual(meta2["months_this_run"], 1)  # 3월 하나만 처리
        self.assertEqual(set(meta2["months"]), {"2024-01", "2024-02", "2024-03"})
        self.assertTrue(meta2["is_census"])
        self.assertEqual(len(session2.calls), 2)  # esearch 1 + efetch 1 (3월분만)

        state2 = collect_pubmed.load_state("demo")
        self.assertTrue(state2["complete"])
        self.assertEqual(state2["complete_through_month"], "2024-03")

    def test_extending_the_window_prints_a_warning(self):
        self.config["window"] = {"from": "2024-01-01", "to": "2024-01-31"}
        transport1, _ = self._transport([_esearch(["1"], 1), _efetch(["1"])])
        _, run_log = self._run_log()
        collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport1, run_log, verbose=False
        )

        self.config["window"]["to"] = "2024-02-29"
        transport2, _ = self._transport([_esearch(["2"], 1), _efetch(["2"])])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            collect_pubmed.collect_profile(
                "demo", "demo", self.config, transport2, run_log, verbose=False
            )
        self.assertIn("window", stderr.getvalue())
        self.assertIn("늘어났습니다", stderr.getvalue())


class CensusIsExhaustionBasedTest(_CollectPubmedTestCase):
    """리뷰 Finding B(실측 2026-08-22, sunscreen/pubmed 전수 재현): 월별
    esearch 결과가 서로 겹친다(발행일이 연도까지만 있는 논문은 그 해 12개
    월 창 전부에 매칭) — expected(월별 count 합)==collected(고유 PMID) 는
    수집이 완벽해도 성립하지 않는다(실측 중복 29%). census 는 count 일치가
    아니라 "이 달의 esearch 페이지네이션을 끝까지 걸었는가"(exhausted)로만
    판정해야 한다.
    """

    def test_heavy_duplicate_overlap_still_counts_as_a_census_when_every_month_is_exhausted(self):
        # 브리핑 예시 그대로 재현: 한 달이 expected=100, collected=60,
        # duplicates=40 이어도(1월에서 이미 본 PMID 1~40 이 2월 esearch 결과에
        # 다시 걸린다 — 연도만 있는 논문 시나리오) 두 달 다 페이지네이션을
        # 끝까지 걸었으면(한 페이지 안에서, 500 미만) census 다.
        self.config["window"] = {"from": "2024-01-01", "to": "2024-02-29"}
        month1_pmids = [str(n) for n in range(1, 61)]  # 60건, 전부 신규
        month2_new = [str(n) for n in range(101, 161)]  # 60건 신규
        month2_dupes = [str(n) for n in range(1, 41)]  # 1월과 겹치는 40건
        month2_pmids = month2_dupes + month2_new  # esearch idlist 100건
        transport, session = self._transport(
            [
                _esearch(month1_pmids, 60),
                _efetch(month1_pmids),
                _esearch(month2_pmids, 100),
                _efetch(month2_new),  # already 에 걸린 40건은 efetch 대상에서 빠진다
            ]
        )
        _, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )

        self.assertEqual(meta["expected_from_api"], 160)  # 60 + 100 (진단용, census 판정엔 안 씀)
        self.assertEqual(meta["collected"], 120)  # 60 + 60 (고유 PMID)
        self.assertEqual(meta["duplicates_skipped"], 40)
        # count 는 애초에 안 맞는다(월 사이 PMID 중복 — 브리핑 근거 참고).
        self.assertNotEqual(meta["expected_from_api"], meta["collected"])
        self.assertTrue(meta["months"]["2024-01"]["exhausted"])
        self.assertTrue(meta["months"]["2024-02"]["exhausted"])
        self.assertTrue(meta["is_census"])

    def test_one_unexhausted_month_makes_the_whole_run_not_a_census(self):
        # 1월은 끝까지 걷지만(exhausted=True) 2월은 예산 소진으로 중단된다
        # (exhausted=False) — 1월 하나만으로는 census 가 아니다.
        self.config["window"] = {"from": "2024-01-01", "to": "2024-02-29"}
        transport, session = self._transport(
            [_esearch(["1"], 1), _efetch(["1"]), FakeResponse(402)]
        )
        _, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )

        self.assertTrue(meta["months"]["2024-01"]["exhausted"])
        self.assertFalse(meta["months"]["2024-02"]["exhausted"])
        self.assertFalse(meta["is_census"])
        self.assertEqual(meta["stopped_reason"], "budget_exhausted")

    def test_an_old_schema_meta_without_exhausted_flags_is_not_trusted_and_gets_rewalked(self):
        # 리뷰 Finding B 의 핵심 회귀: 이 필드 도입 이전에 count 일치만으로
        # complete=True/_meta.json 을 써 둔 raw(지금 디스크의 sunscreen/pubmed
        # 가 정확히 이 상태)를 흉내낸다 — months 값에 "exhausted" 키가 아예
        # 없다. 이 상태로 재실행하면 완료로 신뢰하지 않고 처음부터 다시
        # 걸어야 한다(esearch 가 다시 불려야 한다 — 안 불리면 FakeSession 이
        # 응답 부족으로 실패한다).
        collect_pubmed.profile_dir("demo").mkdir(parents=True)
        collect_pubmed.save_state(
            "demo",
            {
                "month_cursor": None,
                "retstart": 0,
                "complete": True,
                "complete_through_month": "2024-01",
            },
        )
        collect_pubmed.write_meta(
            "demo",
            {
                "months": {
                    "2024-01": {"expected": 1, "collected": 1, "duplicates": 0}
                },  # exhausted 키 없음(구 스키마)
                "expected_from_api": 1,
                "collected": 1,
            },
        )
        collect_pubmed._write_ids("demo", {"1"})  # 사이드카에도 이미 실려 있다

        # esearch 는 다시 불려야 한다(자가 치유) — pmid "1" 은 사이드카에 이미
        # 있으므로 duplicates 로 걸러져 efetch 는 다시 나가지 않는다(멱등).
        transport, session = self._transport([_esearch(["1"], 1)])
        stderr = io.StringIO()
        _, run_log = self._run_log()
        with contextlib.redirect_stderr(stderr):
            meta = collect_pubmed.collect_profile(
                "demo", "demo", self.config, transport, run_log, verbose=False
            )

        self.assertEqual(len(session.calls), 1)  # esearch 만 다시 불렸다(efetch 없음 — 전부 중복)
        self.assertIn("자가 치유", stderr.getvalue())
        self.assertTrue(meta["months"]["2024-01"]["exhausted"])
        self.assertTrue(meta["is_census"])
        # 사이드카 덕에 이미 있던 PMID 1 은 jsonl 에 중복으로 다시 쓰이지 않는다.
        self.assertEqual(meta["new_this_run"], 0)
        lines = self._jsonl_lines()
        self.assertEqual(lines, [])

    def test_a_rerun_after_the_fix_has_populated_exhausted_flags_does_not_rewalk(self):
        # 자가 치유는 일회성이다 — exhausted 플래그가 채워진 뒤에는(이 수정
        # 이후 정상 완료한 실행) 다음 재실행이 다시 esearch 하지 않는다.
        transport1, _ = self._transport([_esearch(["1"], 1), _efetch(["1"])])
        _, run_log = self._run_log()
        meta1 = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport1, run_log, verbose=False
        )
        self.assertTrue(meta1["is_census"])
        self.assertTrue(meta1["months"]["2024-01"]["exhausted"])

        transport2, session2 = self._transport([])  # 응답을 하나도 주지 않는다
        meta2 = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport2, run_log, verbose=False
        )
        self.assertEqual(session2.calls, [])
        self.assertTrue(meta2["is_census"])


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

    def test_a_transport_error_mid_run_reports_transport_error_and_a_partial_run(self):
        """리뷰 대응(Finding 1): esearch 가 TransportError(5xx 재시도 소진)로
        멈춰도 이전에는 _fetch_profile() 이 reason="transport_error" 를
        budget_exhausted 만 보고 버려 stopped_reason 이 None 이 됐다 — RunLog
        상태가 "ok", CLI exit 코드가 0 으로 잘못 기록됐다(README 의 exit-code
        규약과 어긋남). 5xx 를 max_attempts(5)회 반복시켜 TransientError 를
        유발한다."""
        self.config["window"] = {"from": "2024-01-01", "to": "2024-01-31"}
        transport, session = self._transport([FakeResponse(503) for _ in range(5)])
        conn, run_log = self._run_log()
        meta = collect_pubmed.collect_profile(
            "demo", "demo", self.config, transport, run_log, verbose=False
        )
        self.assertEqual(meta["stopped_reason"], "transport_error")
        self.assertFalse(meta["is_census"])
        status = conn.execute("select status from run").fetchone()
        self.assertEqual(status, ("partial",))
        source_reason = conn.execute("select stopped_reason from run_source").fetchone()
        self.assertEqual(source_reason, ("transport_error",))


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


class WindowToOverrideTest(_CollectPubmedTestCase):
    """항목2: --window-to 가 month_list()/month_bounds() 로 그대로 전파되는지."""

    def test_window_to_extends_the_month_list_and_the_esearch_maxdate(self):
        # self.config 의 window 는 2024-01 한 달뿐이다 — window_to 로 2월까지
        # 늘리면 두 번째 달이 새로 생기고, 그 달의 esearch maxdate 도 늘어난
        # 값(2024-02-29)을 그대로 받아야 한다.
        transport, session = self._transport(
            [_esearch(["1"], 1), _efetch(["1"]), _esearch(["2"], 1), _efetch(["2"])]
        )
        results = collect_pubmed.run(
            ["demo"],
            self.config,
            db_path=self.db_path,
            transport=transport,
            window_to="2024-02-29",
        )

        self.assertEqual(set(results["demo"]["months"]), {"2024-01", "2024-02"})
        self.assertEqual(results["demo"]["window"]["to"], "2024-02-29")
        second_esearch_params = dict(session.calls[2]["params"])
        self.assertEqual(second_esearch_params["maxdate"], "2024-02-29")
        # 호출자가 넘긴 원본 config 는 건드리지 않는다.
        self.assertEqual(self.config["window"]["to"], "2024-01-31")


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
