"""trials.collect.run() — FakeSession 을 통해 실제 request 루프를 통과시켜
저장·RunLog 자기기록(run/run_source)·부분 결과 보존을 검증한다.

evidence.pipeline 의 test_evidence_pipeline.py::CollectTest, trend.collect 의
test_trend_collect.py 와 같은 결의 파이프라인 레벨 테스트다 — trials.collect
가 원래 cli.py 안(`_collect_trials()`)에 있다가 코드리뷰 지적(cli.py 가
RunLog/observer 오케스트레이션까지 떠안아 자신의 "인자 파싱과 출력만
담당한다"는 계약을 어겼다)으로 evidence/trend 와 대칭 구조로 옮겨지면서 이
파일도 함께 분리됐다 — 단언은 옮기기 전(tests/paper_radar/test_cli.py 의
TrialsCollectPipelineTest)과 동일하고, 호출 대상 경로만 `cli._collect_trials`
-> `trials_collect.run`로 바뀌었다.
"""

import json
import os
import tempfile
import unittest

from paper_radar.contract import Fetch, SourcePolicy
from paper_radar.storage import repository
from paper_radar.transport.http import Transport
from paper_radar.trials import collect as trials_collect
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

STUDY = {
    "protocolSection": {
        "identificationModule": {"nctId": "NCT01234567", "briefTitle": "SPF50 sunscreen trial"},
        "statusModule": {
            "overallStatus": "COMPLETED",
            "studyFirstPostDateStruct": {"date": "2023-05-01"},
        },
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Acme Corp", "class": "INDUSTRY"}},
        "designModule": {"phases": ["PHASE3"], "enrollmentInfo": {"count": 120}},
        "conditionsModule": {"conditions": ["Sunburn"]},
        "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "SPF50 sunscreen"}]},
        "outcomesModule": {"primaryOutcomes": [{"measure": "Erythema score"}]},
    },
    "hasResults": True,
}


def _json_response(payload, status=200):
    return FakeResponse(status, body=json.dumps(payload).encode())


class RunTest(unittest.TestCase):
    """trials.collect.run() — 검색부터 저장, RunLog 자기기록까지 전 과정."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = repository.connect(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def test_a_full_run_stores_and_records_run_metadata(self):
        transport, session = self._transport(
            [_json_response({"totalCount": 1, "studies": [STUDY]})]
        )
        report = trials_collect.run(self.conn, transport, "sunscreen", 10)

        self.assertEqual(report.status, "ok")
        self.assertEqual(len(report.records), 1)
        self.assertIsNone(report.stopped_reason)
        self.assertEqual(len(session.calls), 1)

        stored_count = self.conn.execute("SELECT COUNT(*) FROM trial").fetchone()[0]
        self.assertEqual(stored_count, 1)

        run_row = self.conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (report.run_id,)
        ).fetchone()
        self.assertEqual(run_row["status"], "ok")
        self.assertIsNotNone(run_row["finished_at"])
        self.assertEqual(run_row["command"], "trials collect")

        source_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'clinicaltrials'",
            (report.run_id,),
        ).fetchone()
        self.assertEqual(source_row["requests"], 1)
        self.assertEqual(source_row["records"], 1)
        self.assertIsNone(source_row["stopped_reason"])

    def test_budget_exhausted_stops_but_keeps_already_collected_records_and_marks_partial(self):
        responses = [
            _json_response(
                {"totalCount": 3, "studies": [STUDY, STUDY], "nextPageToken": "p2"}
            ),
            FakeResponse(402),  # BudgetExhausted
        ]
        transport, _ = self._transport(responses)
        report = trials_collect.run(self.conn, transport, "sunscreen", 10)

        self.assertEqual(report.status, "partial")
        self.assertEqual(report.stopped_reason, "budget_exhausted")
        self.assertEqual(len(report.records), 2, "1페이지에서 이미 받은 레코드는 보존돼야 한다")

        stored_count = self.conn.execute("SELECT COUNT(*) FROM trial").fetchone()[0]
        self.assertEqual(stored_count, 1, "STUDY 가 같은 nct_id 라 한 행으로 합쳐진다")

        source_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'clinicaltrials'",
            (report.run_id,),
        ).fetchone()
        self.assertEqual(source_row["stopped_reason"], "budget_exhausted")

    def test_returns_no_records_when_the_search_finds_nothing(self):
        transport, _ = self._transport([_json_response({"totalCount": 0, "studies": []})])
        report = trials_collect.run(self.conn, transport, "sunscreen", 10)
        self.assertEqual(report.records, [])
        self.assertEqual(report.status, "ok")

    def test_calls_on_progress_once_per_collected_record_with_the_running_total(self):
        transport, _ = self._transport(
            [_json_response({"totalCount": 1, "studies": [STUDY]})]
        )
        calls = []
        trials_collect.run(
            self.conn,
            transport,
            "sunscreen",
            10,
            on_progress=lambda index, total, trial: calls.append((index, total, trial.nct_id)),
        )
        self.assertEqual(calls, [(1, 1, "NCT01234567")])

    def test_reusing_the_same_transport_after_collect_does_not_leak_into_the_finished_run(self):
        transport, _ = self._transport(
            [
                _json_response({"totalCount": 1, "studies": [STUDY]}),
                FakeResponse(200),  # collect 종료 후 같은 transport 로 보내는 요청
            ]
        )
        report = trials_collect.run(self.conn, transport, "sunscreen", 10)
        fetch_count_after_collect = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (report.run_id,)
        ).fetchone()[0]
        self.assertGreater(fetch_count_after_collect, 0)

        transport.request(
            Fetch(url="https://api.example.org/x"),
            SourcePolicy(host="api.example.org", min_interval_s=0.0),
        )
        fetch_count_after_reuse = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (report.run_id,)
        ).fetchone()[0]
        self.assertEqual(fetch_count_after_reuse, fetch_count_after_collect)


if __name__ == "__main__":
    unittest.main()
