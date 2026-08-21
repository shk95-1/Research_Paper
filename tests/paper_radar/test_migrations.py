"""schema 모듈: user_version 러너 — 빈 DB, 구 DB 입양, 멱등성, 실패 롤백."""

import os
import sqlite3
import tempfile
import unittest

from paper_radar.storage import schema
from paper_radar.storage.migrations import MIGRATIONS
from papers import store as legacy_store


def _fresh_connection(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


class MigrateOnAnEmptyDatabaseTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = _fresh_connection(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_starts_at_version_zero(self):
        self.assertEqual(schema.schema_version(self.conn), 0)

    def test_migrate_reaches_the_head_version(self):
        applied = schema.migrate(self.conn)
        self.assertEqual(applied, [number for number, _name, _apply in MIGRATIONS])
        self.assertEqual(schema.schema_version(self.conn), len(MIGRATIONS))

    def test_migrate_creates_every_expected_table(self):
        schema.migrate(self.conn)
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertEqual(
            tables, {"papers", "cache", "run", "run_source", "fetch_log", "sqlite_sequence"}
        )

    def test_migrate_adds_the_evidence_column(self):
        schema.migrate(self.conn)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(papers)")}
        self.assertIn("evidence", columns)


class AdoptsALegacyDatabaseTest(unittest.TestCase):
    """papers/store.py 의 connect() 로 만든(버전 개념이 없는) DB 위에서."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        legacy_conn = legacy_store.connect(self.path)
        legacy_store.upsert(
            legacy_conn,
            {
                "doi": "10.1/legacy",
                "title": "Legacy paper",
                "authors": ["Kim"],
                "collected_at": "2026-01-01T00:00:00Z",
                "verification": {
                    "crossref_verified": True,
                    "title_match": True,
                    "found_in_sources": ["openalex"],
                    "is_retracted": False,
                    "has_doi": True,
                    "confidence_score": 80,
                },
            },
        )
        legacy_conn.close()
        self.conn = _fresh_connection(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_legacy_database_starts_unstamped(self):
        self.assertEqual(schema.schema_version(self.conn), 0)

    def test_migrate_stamps_the_head_version_without_losing_data(self):
        applied = schema.migrate(self.conn)
        self.assertEqual(applied, [number for number, _name, _apply in MIGRATIONS])
        self.assertEqual(schema.schema_version(self.conn), len(MIGRATIONS))
        row = self.conn.execute(
            "SELECT title, doi FROM papers WHERE key = '10.1/legacy'"
        ).fetchone()
        self.assertEqual(row["title"], "Legacy paper")
        self.assertEqual(row["doi"], "10.1/legacy")

    def test_migrate_adds_runlog_tables_on_top_of_the_legacy_schema(self):
        schema.migrate(self.conn)
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertIn("run", tables)
        self.assertIn("run_source", tables)
        self.assertIn("fetch_log", tables)


class MigrateIsIdempotentTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = _fresh_connection(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_running_migrate_twice_applies_nothing_the_second_time(self):
        first = schema.migrate(self.conn)
        second = schema.migrate(self.conn)
        self.assertEqual(first, [number for number, _name, _apply in MIGRATIONS])
        self.assertEqual(second, [])
        self.assertEqual(schema.schema_version(self.conn), len(MIGRATIONS))


class FailedMigrationRollsBackTest(unittest.TestCase):
    """일부러 깨지는 가짜 마이그레이션으로 절반 적용 방지를 검증한다."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = _fresh_connection(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_a_broken_migration_leaves_no_trace_and_does_not_bump_the_version(self):
        def good(conn):
            conn.executescript("CREATE TABLE ok_marker (x TEXT);")

        def broken(conn):
            # 테이블은 만들지만 그다음에 터진다 — 두 변경 모두 롤백돼야 한다.
            conn.executescript("CREATE TABLE broken_marker (x TEXT);")
            raise RuntimeError("일부러 깨지는 마이그레이션")

        fake_migrations = ((1, "good", good), (2, "broken", broken))

        with self.assertRaises(RuntimeError):
            schema.migrate(self.conn, migrations=fake_migrations)

        # 1번은 성공했으니 남아 있어야 하고, 버전도 1 이어야 한다.
        self.assertEqual(schema.schema_version(self.conn), 1)
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertIn("ok_marker", tables)
        # 2번은 실패했으니 그 안에서 만든 테이블도 함께 롤백돼야 한다.
        self.assertNotIn("broken_marker", tables)

    def test_a_broken_migration_still_allows_normal_writes_afterwards(self):
        # migrate() 가 autocommit 모드를 원상복구하지 않으면, 실패 이후의
        # 평범한 conn.commit() 기반 쓰기가 깨질 수 있다.
        def broken(conn):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            schema.migrate(self.conn, migrations=((1, "broken", broken),))

        self.conn.execute("CREATE TABLE t (x TEXT)")
        self.conn.execute("INSERT INTO t (x) VALUES ('a')")
        self.conn.commit()
        reopened = _fresh_connection(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
