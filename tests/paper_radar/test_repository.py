"""repository 모듈: record_key, public_record, 병합 upsert, 검색, JSON 덤프.

papers/tests/test_store.py 의 동등 케이스를 옮겨오고, 이 모듈에서 바뀐
동작(병합 upsert, is_retracted 최상위 노출, verification 분리 인자)에
해당하는 케이스를 추가한다.
"""

import json
import os
import tempfile
import unittest

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


class RecordKeyTest(unittest.TestCase):
    def test_uses_doi_when_present(self):
        self.assertEqual(repository.record_key(record()), "10.1016/j.test.2024.01.001")

    def test_falls_back_to_openalex_id_when_doi_is_missing(self):
        self.assertEqual(repository.record_key(record(doi=None)), "https://openalex.org/w1")

    def test_lowercases_and_strips(self):
        messy = record(doi="  10.1016/J.TEST.2024.01.001 ")
        self.assertEqual(repository.record_key(messy), "10.1016/j.test.2024.01.001")

    def test_returns_empty_string_when_nothing_identifies_the_record(self):
        self.assertEqual(repository.record_key({}), "")


class PublicRecordTest(unittest.TestCase):
    def test_emits_the_spec_schema_keys_plus_is_retracted(self):
        messy = record(publisher="Elsevier BV")
        messy["verification"] = verification()
        expected = [
            "abstract",
            "authors",
            "citation_count",
            "collected_at",
            "doi",
            "is_open_access",
            "is_retracted",
            "journal",
            "keywords",
            "openalex_id",
            "title",
            "tldr",
            "topics",
            "url",
            "verification",
            "year",
        ]
        self.assertEqual(sorted(repository.public_record(messy)), expected)

    def test_fills_missing_fields_with_none_or_empty_list(self):
        result = repository.public_record({"openalex_id": "https://openalex.org/W1"})
        self.assertIsNone(result["doi"])
        self.assertIsNone(result["abstract"])
        self.assertEqual(result["authors"], [])

    def test_exposes_is_retracted_at_top_level_from_nested_verification(self):
        # 버그 수정의 핵심: 최상위 is_retracted 는 verification.is_retracted 를 따른다.
        messy = record()
        messy["verification"] = verification(is_retracted=True)
        result = repository.public_record(messy)
        self.assertTrue(result["is_retracted"])

    def test_top_level_is_retracted_defaults_to_false_without_verification(self):
        result = repository.public_record(record())
        self.assertFalse(result["is_retracted"])


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = repository.connect(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        for path in (self.path, self.path + ".json"):
            if os.path.exists(path):
                os.unlink(path)

    def _count(self):
        return self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def test_connect_migrates_to_the_head_schema(self):
        # papers/cache 뿐 아니라 run/run_source/fetch_log/evidence 컬럼까지 있어야 한다.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(papers)")}
        self.assertIn("evidence", columns)
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertIn("run", tables)

    def test_upsert_inserts_one_row(self):
        repository.upsert(self.conn, record(), verification())
        self.assertEqual(self._count(), 1)

    def test_upsert_twice_with_same_doi_keeps_one_row_and_updates_it(self):
        repository.upsert(self.conn, record(), verification())
        repository.upsert(
            self.conn,
            record(title="Revised title", citation_count=99),
            verification(),
        )
        rows = self.conn.execute("SELECT title, citation_count FROM papers").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Revised title")
        self.assertEqual(rows[0]["citation_count"], 99)

    def test_flattens_verification_into_queryable_columns(self):
        repository.upsert(self.conn, record(), verification())
        row = self.conn.execute(
            "SELECT confidence_score, crossref_verified, has_doi, found_in_sources FROM papers"
        ).fetchone()
        self.assertEqual(row["confidence_score"], 85)
        self.assertEqual(row["crossref_verified"], 1)
        self.assertEqual(row["has_doi"], 1)
        self.assertEqual(json.loads(row["found_in_sources"]), ["openalex", "semantic_scholar"])

    def test_stores_doi_less_record_keyed_by_openalex_id(self):
        repository.upsert(
            self.conn,
            record(doi=None),
            verification(
                crossref_verified=False, title_match=False, has_doi=False, confidence_score=15
            ),
        )
        row = self.conn.execute("SELECT key, doi, has_doi FROM papers").fetchone()
        self.assertEqual(row["key"], "https://openalex.org/w1")
        self.assertIsNone(row["doi"])
        self.assertEqual(row["has_doi"], 0)

    def test_all_records_round_trips_the_spec_schema(self):
        repository.upsert(self.conn, record(), verification())
        got = repository.all_records(self.conn)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["verification"]["confidence_score"], 85)
        self.assertEqual(got[0]["keywords"], ["retinol", "skin barrier"])
        self.assertEqual(got[0]["authors"], ["Kim", "Lee"])
        self.assertTrue(got[0]["is_open_access"])
        self.assertFalse(got[0]["is_retracted"])

    def test_search_matches_keyword_case_insensitively_in_title(self):
        repository.upsert(self.conn, record(), verification())
        self.assertEqual(len(repository.search(self.conn, "RETINOL")), 1)

    def test_search_matches_keyword_in_abstract(self):
        repository.upsert(self.conn, record(), verification())
        self.assertEqual(len(repository.search(self.conn, "barrier function")), 1)

    def test_search_matches_keyword_in_keywords(self):
        repository.upsert(
            self.conn, record(title="Unrelated", abstract="Unrelated"), verification()
        )
        self.assertEqual(len(repository.search(self.conn, "skin barrier")), 1)

    def test_search_returns_nothing_for_an_absent_keyword(self):
        repository.upsert(self.conn, record(), verification())
        self.assertEqual(repository.search(self.conn, "niacinamide"), [])

    def test_search_applies_min_confidence_floor(self):
        repository.upsert(self.conn, record(), verification())
        self.assertEqual(len(repository.search(self.conn, "retinol", min_confidence=70)), 1)
        self.assertEqual(len(repository.search(self.conn, "retinol", min_confidence=90)), 0)

    def test_search_excludes_retracted_papers_by_default(self):
        repository.upsert(
            self.conn, record(), verification(is_retracted=True, confidence_score=0)
        )
        self.assertEqual(repository.search(self.conn, "retinol"), [])
        found = repository.search(self.conn, "retinol", include_retracted=True)
        self.assertEqual(len(found), 1)

    def test_search_orders_by_confidence_then_year_descending(self):
        repository.upsert(
            self.conn, record(doi="10.1/a", year=2020), verification(confidence_score=70)
        )
        repository.upsert(
            self.conn, record(doi="10.1/b", year=2018), verification(confidence_score=90)
        )
        repository.upsert(
            self.conn, record(doi="10.1/c", year=2024), verification(confidence_score=90)
        )
        got = repository.search(self.conn, "retinol")
        self.assertEqual([r["doi"] for r in got], ["10.1/c", "10.1/b", "10.1/a"])

    def test_search_honours_the_limit(self):
        for index in range(5):
            repository.upsert(self.conn, record(doi=f"10.1/{index}"), verification())
        self.assertEqual(len(repository.search(self.conn, "retinol", limit=2)), 2)

    def test_dump_json_writes_every_record(self):
        repository.upsert(self.conn, record(), verification())
        target = self.path + ".json"
        repository.dump_json(self.conn, target)
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["doi"], "10.1016/j.test.2024.01.001")

    def test_connect_is_idempotent_on_an_existing_database(self):
        repository.upsert(self.conn, record(), verification())
        again = repository.connect(self.path)
        self.addCleanup(again.close)
        self.assertEqual(again.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)


class MergeUpsertTest(unittest.TestCase):
    """구 store.py 와의 핵심 차이: 병합 upsert."""

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

    def _stored(self):
        return repository.all_records(self.conn)[0]

    def test_a_poor_re_collection_does_not_erase_a_richer_abstract_and_tldr(self):
        repository.upsert(self.conn, record(), verification())
        # 재수집이 abstract/tldr 를 못 얻은 경우를 흉내낸다.
        repository.upsert(
            self.conn,
            record(abstract=None, tldr=None, keywords=[], topics=[]),
            verification(),
        )
        stored = self._stored()
        self.assertEqual(stored["abstract"], "Retinol improves the skin barrier function.")
        self.assertEqual(stored["tldr"], "Retinol helps the barrier.")
        self.assertEqual(stored["keywords"], ["retinol", "skin barrier"])
        self.assertEqual(stored["topics"], ["Dermatology"])

    def test_a_richer_re_collection_still_overwrites_content_fields(self):
        repository.upsert(self.conn, record(), verification())
        repository.upsert(
            self.conn,
            record(abstract="Updated abstract.", citation_count=200),
            verification(),
        )
        stored = self._stored()
        self.assertEqual(stored["abstract"], "Updated abstract.")
        self.assertEqual(stored["citation_count"], 200)

    def test_verification_fields_are_always_overwritten_even_when_they_shrink(self):
        repository.upsert(self.conn, record(), verification(confidence_score=90))
        repository.upsert(
            self.conn,
            record(),
            verification(
                confidence_score=10,
                crossref_verified=False,
                found_in_sources=["openalex"],
            ),
        )
        stored = self._stored()
        self.assertEqual(stored["verification"]["confidence_score"], 10)
        self.assertFalse(stored["verification"]["crossref_verified"])
        self.assertEqual(stored["verification"]["found_in_sources"], ["openalex"])

    def test_is_retracted_correction_is_reflected_not_merged_away(self):
        # 정정: 지난 실행엔 철회였는데 이번엔 철회가 취소됐다.
        repository.upsert(
            self.conn, record(), verification(is_retracted=True, confidence_score=0)
        )
        repository.upsert(
            self.conn, record(), verification(is_retracted=False, confidence_score=85)
        )
        stored = self._stored()
        self.assertFalse(stored["is_retracted"])
        self.assertFalse(stored["verification"]["is_retracted"])
        self.assertEqual(stored["verification"]["confidence_score"], 85)

    def test_collected_at_reflects_the_latest_run(self):
        repository.upsert(self.conn, record(collected_at="2026-01-01T00:00:00Z"), verification())
        repository.upsert(self.conn, record(collected_at="2026-08-20T00:00:00Z"), verification())
        self.assertEqual(self._stored()["collected_at"], "2026-08-20T00:00:00Z")

    def test_an_empty_list_is_stored_as_null_and_restored_as_an_empty_list(self):
        repository.upsert(self.conn, record(keywords=[], topics=[]), verification())
        row = self.conn.execute("SELECT keywords, topics FROM papers").fetchone()
        self.assertIsNone(row["keywords"])
        self.assertIsNone(row["topics"])
        stored = self._stored()
        self.assertEqual(stored["keywords"], [])
        self.assertEqual(stored["topics"], [])

    def test_a_missing_is_open_access_does_not_clobber_a_previously_known_true(self):
        repository.upsert(self.conn, record(is_open_access=True), verification())
        repository.upsert(self.conn, record(is_open_access=None), verification())
        self.assertTrue(self._stored()["is_open_access"])

    def test_evidence_column_is_always_overwritten_with_the_latest_verification(self):
        repository.upsert(
            self.conn, record(), verification(evidence={"crossref": {"found": True}})
        )
        row = self.conn.execute("SELECT evidence FROM papers").fetchone()
        self.assertEqual(json.loads(row["evidence"]), {"crossref": {"found": True}})
        repository.upsert(self.conn, record(), verification())
        row = self.conn.execute("SELECT evidence FROM papers").fetchone()
        self.assertIsNone(row["evidence"])


if __name__ == "__main__":
    unittest.main()
