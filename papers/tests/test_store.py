"""store 모듈: 스키마, record_key, upsert 멱등성, 검색, JSON 덤프."""

import json
import os
import tempfile
import unittest

from papers import store


def record(**overrides):
    """스펙 8절 레코드 스키마를 따르는 최소 레코드."""
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
        "verification": {
            "crossref_verified": True,
            "title_match": True,
            "found_in_sources": ["openalex", "semantic_scholar"],
            "is_retracted": False,
            "has_doi": True,
            "confidence_score": 85,
        },
    }
    base.update(overrides)
    return base


def verification(**overrides):
    base = {
        "crossref_verified": True,
        "title_match": True,
        "found_in_sources": ["openalex"],
        "is_retracted": False,
        "has_doi": True,
        "confidence_score": 70,
    }
    base.update(overrides)
    return base


class RecordKeyTest(unittest.TestCase):
    def test_uses_doi_when_present(self):
        self.assertEqual(store.record_key(record()), "10.1016/j.test.2024.01.001")

    def test_falls_back_to_openalex_id_when_doi_is_missing(self):
        self.assertEqual(store.record_key(record(doi=None)), "https://openalex.org/w1")

    def test_lowercases_and_strips(self):
        messy = record(doi="  10.1016/J.TEST.2024.01.001 ")
        self.assertEqual(store.record_key(messy), "10.1016/j.test.2024.01.001")

    def test_returns_empty_string_when_nothing_identifies_the_record(self):
        self.assertEqual(store.record_key({}), "")


class PublicRecordTest(unittest.TestCase):
    def test_emits_exactly_the_spec_schema_keys(self):
        messy = record(is_retracted=False, publisher="Elsevier BV")
        expected = [
            "abstract", "authors", "citation_count", "collected_at", "date", "doi",
            "is_open_access", "journal", "keywords", "openalex_id", "title",
            "tldr", "topics", "url", "verification", "year",
        ]
        self.assertEqual(sorted(store.public_record(messy)), expected)

    def test_fills_missing_fields_with_none(self):
        result = store.public_record({"openalex_id": "https://openalex.org/W1"})
        self.assertIsNone(result["doi"])
        self.assertIsNone(result["abstract"])
        self.assertEqual(result["authors"], [])


class StoreTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = store.connect(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        for path in (self.path, self.path + ".json"):
            if os.path.exists(path):
                os.unlink(path)

    def _count(self):
        return self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def test_upsert_inserts_one_row(self):
        store.upsert(self.conn, record())
        self.assertEqual(self._count(), 1)

    def test_upsert_twice_with_same_doi_keeps_one_row_and_updates_it(self):
        store.upsert(self.conn, record())
        store.upsert(self.conn, record(title="Revised title", citation_count=99))
        rows = self.conn.execute("SELECT title, citation_count FROM papers").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Revised title")
        self.assertEqual(rows[0]["citation_count"], 99)

    def test_flattens_verification_into_queryable_columns(self):
        store.upsert(self.conn, record())
        row = self.conn.execute(
            "SELECT confidence_score, crossref_verified, has_doi, found_in_sources"
            " FROM papers"
        ).fetchone()
        self.assertEqual(row["confidence_score"], 85)
        self.assertEqual(row["crossref_verified"], 1)
        self.assertEqual(row["has_doi"], 1)
        self.assertEqual(
            json.loads(row["found_in_sources"]), ["openalex", "semantic_scholar"]
        )

    def test_stores_doi_less_record_keyed_by_openalex_id(self):
        store.upsert(
            self.conn,
            record(
                doi=None,
                verification=verification(
                    crossref_verified=False,
                    title_match=False,
                    has_doi=False,
                    confidence_score=15,
                ),
            ),
        )
        row = self.conn.execute("SELECT key, doi, has_doi FROM papers").fetchone()
        self.assertEqual(row["key"], "https://openalex.org/w1")
        self.assertIsNone(row["doi"])
        self.assertEqual(row["has_doi"], 0)

    def test_all_records_round_trips_the_spec_schema(self):
        store.upsert(self.conn, record())
        got = store.all_records(self.conn)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["verification"]["confidence_score"], 85)
        self.assertEqual(got[0]["keywords"], ["retinol", "skin barrier"])
        self.assertEqual(got[0]["authors"], ["Kim", "Lee"])
        self.assertTrue(got[0]["is_open_access"])

    def test_search_matches_keyword_case_insensitively_in_title(self):
        store.upsert(self.conn, record())
        self.assertEqual(len(store.search(self.conn, "RETINOL")), 1)

    def test_search_matches_keyword_in_abstract(self):
        store.upsert(self.conn, record())
        self.assertEqual(len(store.search(self.conn, "barrier function")), 1)

    def test_search_matches_keyword_in_keywords(self):
        store.upsert(self.conn, record(title="Unrelated", abstract="Unrelated"))
        self.assertEqual(len(store.search(self.conn, "skin barrier")), 1)

    def test_search_returns_nothing_for_an_absent_keyword(self):
        store.upsert(self.conn, record())
        self.assertEqual(store.search(self.conn, "niacinamide"), [])

    def test_search_applies_min_confidence_floor(self):
        store.upsert(self.conn, record())
        self.assertEqual(len(store.search(self.conn, "retinol", min_confidence=70)), 1)
        self.assertEqual(len(store.search(self.conn, "retinol", min_confidence=90)), 0)

    def test_search_excludes_retracted_papers_by_default(self):
        store.upsert(
            self.conn,
            record(verification=verification(is_retracted=True, confidence_score=0)),
        )
        self.assertEqual(store.search(self.conn, "retinol"), [])
        found = store.search(self.conn, "retinol", include_retracted=True)
        self.assertEqual(len(found), 1)

    def test_search_orders_by_confidence_then_year_descending(self):
        store.upsert(self.conn, record(doi="10.1/a", year=2020,
                                      verification=verification(confidence_score=70)))
        store.upsert(self.conn, record(doi="10.1/b", year=2018,
                                      verification=verification(confidence_score=90)))
        store.upsert(self.conn, record(doi="10.1/c", year=2024,
                                      verification=verification(confidence_score=90)))
        got = store.search(self.conn, "retinol")
        self.assertEqual([r["doi"] for r in got], ["10.1/c", "10.1/b", "10.1/a"])

    def test_search_honours_the_limit(self):
        for index in range(5):
            store.upsert(self.conn, record(doi=f"10.1/{index}"))
        self.assertEqual(len(store.search(self.conn, "retinol", limit=2)), 2)

    def test_dump_json_writes_every_record(self):
        store.upsert(self.conn, record())
        target = self.path + ".json"
        store.dump_json(self.conn, target)
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["doi"], "10.1016/j.test.2024.01.001")

    def test_connect_is_idempotent_on_an_existing_database(self):
        store.upsert(self.conn, record())
        again = store.connect(self.path)
        self.addCleanup(again.close)
        self.assertEqual(again.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
