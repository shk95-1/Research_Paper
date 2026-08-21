"""runlog 모듈: RunLog 왕복, scrub_url, log_fetch 즉시 커밋."""

import os
import sqlite3
import tempfile
import unittest

from paper_radar.storage import repository, runlog


class ScrubUrlTest(unittest.TestCase):
    def test_removes_the_api_key_query_parameter(self):
        scrubbed = runlog.scrub_url("https://api.openalex.org/works?search=retinol&api_key=SECRET")
        self.assertNotIn("SECRET", scrubbed)
        self.assertNotIn("api_key", scrubbed)

    def test_keeps_other_query_parameters_intact(self):
        scrubbed = runlog.scrub_url("https://api.example.org/x?search=retinol&api_key=SECRET&page=2")
        self.assertIn("search=retinol", scrubbed)
        self.assertIn("page=2", scrubbed)

    def test_is_case_insensitive_about_the_parameter_name(self):
        scrubbed = runlog.scrub_url("https://api.example.org/x?API_KEY=SECRET")
        self.assertNotIn("SECRET", scrubbed)

    def test_leaves_a_url_without_credentials_unchanged_in_substance(self):
        scrubbed = runlog.scrub_url("https://api.example.org/x?search=retinol")
        self.assertIn("search=retinol", scrubbed)

    def test_leaves_a_url_without_any_query_untouched(self):
        self.assertEqual(runlog.scrub_url("https://api.example.org/x"), "https://api.example.org/x")


class RunLogTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = repository.connect(self.path)
        self.log = runlog.RunLog(self.conn)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_start_returns_a_run_id_and_records_a_running_row(self):
        run_id = self.log.start("evidence collect", {"query": "sunscreen", "year": 2026})
        row = self.conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["command"], "evidence collect")
        self.assertIsNone(row["finished_at"])
        self.assertEqual(row["schema_version"], 3)

    def test_record_source_then_finish_round_trips(self):
        run_id = self.log.start("evidence collect", {})
        self.log.record_source(
            run_id,
            "openalex",
            requests=10,
            records=8,
            errors=0,
            budget_remaining=90,
            stopped_reason=None,
        )
        self.log.finish(run_id, "ok")

        run_row = self.conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
        self.assertEqual(run_row["status"], "ok")
        self.assertIsNotNone(run_row["finished_at"])

        source_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'openalex'", (run_id,)
        ).fetchone()
        self.assertEqual(source_row["requests"], 10)
        self.assertEqual(source_row["records"], 8)
        self.assertEqual(source_row["budget_remaining"], 90)
        self.assertIsNone(source_row["stopped_reason"])

    def test_record_source_called_twice_for_the_same_source_updates_in_place(self):
        run_id = self.log.start("evidence collect", {})
        self.log.record_source(run_id, "openalex", requests=1, records=1, errors=0)
        self.log.record_source(
            run_id,
            "openalex",
            requests=5,
            records=4,
            errors=1,
            stopped_reason="max_pages",
        )
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM run_source WHERE run_id = ?", (run_id,)
        ).fetchone()
        self.assertEqual(rows[0], 1)
        row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ?", (run_id,)
        ).fetchone()
        self.assertEqual(row["requests"], 5)
        self.assertEqual(row["stopped_reason"], "max_pages")

    def test_log_fetch_scrubs_credentials_out_of_the_stored_url(self):
        run_id = self.log.start("evidence collect", {})
        self.log.log_fetch(
            run_id,
            source="openalex",
            url="https://api.openalex.org/works?api_key=SECRET&search=x",
            status=200,
            attempt=1,
            elapsed_ms=120,
        )
        row = self.conn.execute(
            "SELECT url FROM fetch_log WHERE run_id = ?", (run_id,)
        ).fetchone()
        self.assertNotIn("SECRET", row["url"])

    def test_log_fetch_commits_immediately_so_a_second_connection_can_observe_it(self):
        run_id = self.log.start("evidence collect", {})
        self.log.log_fetch(
            run_id, source="openalex", url="https://api.openalex.org/works", status=200, attempt=1
        )
        observer = sqlite3.connect(self.path)
        self.addCleanup(observer.close)
        count = observer.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_log_fetch_records_a_null_status_for_a_transport_failure(self):
        run_id = self.log.start("evidence collect", {})
        self.log.log_fetch(
            run_id,
            source="openalex",
            url="https://api.openalex.org/works",
            status=None,
            attempt=3,
            error="connection reset",
        )
        row = self.conn.execute(
            "SELECT status, error FROM fetch_log WHERE run_id = ?", (run_id,)
        ).fetchone()
        self.assertIsNone(row["status"])
        self.assertEqual(row["error"], "connection reset")


if __name__ == "__main__":
    unittest.main()
