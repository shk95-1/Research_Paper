"""ingredients.resolve.run() — FakeSession 을 통해 실제 request 루프를 통과시켜
저장·RunLog 자기기록(run/run_source)·NotFound 흡수를 검증한다.

trials.collect 의 test_trials_collect.py 와 같은 결의 파이프라인 레벨
테스트다 — trials.collect 와 마찬가지로 처음부터 cli.py 밖(ingredients/resolve.py)
에 있다.
"""

import json
import os
import tempfile
import unittest

from paper_radar.contract import Fetch, SourcePolicy
from paper_radar.ingredients import resolve as ingredients_resolve
from paper_radar.storage import repository
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

PAYLOAD_CIDS = {"IdentifierList": {"CID": [936]}}
PAYLOAD_SYNONYMS = {
    "InformationList": {
        "Information": [
            {"CID": 936, "Synonym": ["niacinamide", "nicotinamide", "98-92-0"]}
        ]
    }
}


def _json_response(payload, status=200):
    return FakeResponse(status, body=json.dumps(payload).encode())


class RunTest(unittest.TestCase):
    """ingredients.resolve.run() — 이름 조회부터 저장, RunLog 자기기록까지 전 과정."""

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
            [_json_response(PAYLOAD_CIDS), _json_response(PAYLOAD_SYNONYMS)]
        )
        report = ingredients_resolve.run(self.conn, transport, "niacinamide")

        self.assertEqual(report.status, "ok")
        self.assertIsNotNone(report.record)
        self.assertEqual(report.record.cid, 936)
        self.assertEqual(len(session.calls), 2)

        stored_count = self.conn.execute("SELECT COUNT(*) FROM ingredient").fetchone()[0]
        self.assertEqual(stored_count, 1)

        run_row = self.conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (report.run_id,)
        ).fetchone()
        self.assertEqual(run_row["status"], "ok")
        self.assertIsNotNone(run_row["finished_at"])
        self.assertEqual(run_row["command"], "ingredient resolve")

        source_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'pubchem'",
            (report.run_id,),
        ).fetchone()
        self.assertEqual(source_row["requests"], 2)
        self.assertEqual(source_row["records"], 1)
        self.assertIsNone(source_row["stopped_reason"])

    def test_an_unknown_name_is_absorbed_as_not_found_not_an_exception(self):
        transport, _ = self._transport([FakeResponse(404)])
        report = ingredients_resolve.run(self.conn, transport, "not-a-real-ingredient")

        self.assertEqual(report.status, "not_found")
        self.assertIsNone(report.record)

        run_row = self.conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (report.run_id,)
        ).fetchone()
        self.assertEqual(run_row["status"], "not_found")

        source_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'pubchem'",
            (report.run_id,),
        ).fetchone()
        self.assertEqual(source_row["stopped_reason"], "not_found")
        self.assertEqual(source_row["records"], 0)

        stored_count = self.conn.execute("SELECT COUNT(*) FROM ingredient").fetchone()[0]
        self.assertEqual(stored_count, 0, "찾지 못한 이름은 저장되면 안 된다")

    def test_calls_on_progress_once_with_the_resolved_record(self):
        transport, _ = self._transport(
            [_json_response(PAYLOAD_CIDS), _json_response(PAYLOAD_SYNONYMS)]
        )
        calls = []
        ingredients_resolve.run(
            self.conn,
            transport,
            "niacinamide",
            on_progress=lambda record: calls.append(record.name_key),
        )
        self.assertEqual(calls, ["niacinamide"])

    def test_reusing_the_same_transport_after_run_does_not_leak_into_the_finished_run(self):
        transport, _ = self._transport(
            [
                _json_response(PAYLOAD_CIDS),
                _json_response(PAYLOAD_SYNONYMS),
                FakeResponse(200),  # run() 종료 후 같은 transport 로 보내는 요청
            ]
        )
        report = ingredients_resolve.run(self.conn, transport, "niacinamide")
        fetch_count_after_run = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (report.run_id,)
        ).fetchone()[0]
        self.assertGreater(fetch_count_after_run, 0)

        transport.request(
            Fetch(url="https://api.example.org/x"),
            SourcePolicy(host="api.example.org", min_interval_s=0.0),
        )
        fetch_count_after_reuse = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (report.run_id,)
        ).fetchone()[0]
        self.assertEqual(fetch_count_after_reuse, fetch_count_after_run)


if __name__ == "__main__":
    unittest.main()
