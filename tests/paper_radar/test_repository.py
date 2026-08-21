"""repository 모듈: record_key, public_record, 병합 upsert, 검색, JSON 덤프.

papers/tests/test_store.py 의 동등 케이스를 옮겨오고, 이 모듈에서 바뀐
동작(병합 upsert, is_retracted 최상위 노출, verification 분리 인자)에
해당하는 케이스를 추가한다. T8 이 도입한 upsert_records()/TABLE_FOR(dataclass
레코드 upsert, papers 밖의 신규 소스가 쓰는 기계)와 oa_pdf_urls() 읽기
헬퍼도 여기서 검증한다.
"""

import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from typing import ClassVar
from unittest import mock

from paper_radar.models import OaLocationRecord, RetractionRecord
from paper_radar.storage import repository, runlog


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
            "mesh_terms",
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
        self.assertEqual(result["mesh_terms"], [])  # T10 — 다른 list 필드와 동일하게 [] 기본값

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

    def test_connect_enables_foreign_key_enforcement(self):
        # SQLite 는 연결마다 새로 켜야 한다 — 꺼진 채로 두면 run_source/
        # fetch_log 의 "ON DELETE CASCADE" 선언이 장식으로만 남는다.
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_deleting_a_run_cascades_to_its_run_source_and_fetch_log_rows(self):
        # run 삭제 API 는 아직 없지만(원장 deferred minor), PRAGMA 가 실제로
        # 켜져 있어야 그 API 가 생기는 순간 이 동작을 공짜로 얻는다 — 지금
        # DELETE FROM run 을 직접 실행해 그 계약을 미리 고정해 둔다.
        log = runlog.RunLog(self.conn)
        run_id = log.start("evidence collect", {})
        log.record_source(run_id, "openalex", requests=1, records=1, errors=0)
        log.log_fetch(
            run_id, source="openalex", url="https://api.openalex.org/works", status=200, attempt=1
        )

        self.conn.execute("DELETE FROM run WHERE run_id = ?", (run_id,))
        self.conn.commit()

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM run_source WHERE run_id = ?", (run_id,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?", (run_id,)
            ).fetchone()[0],
            0,
        )

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

    def test_stores_and_round_trips_mesh_terms(self):
        # T10 — PubMed 가 채우는 필드. tuple 로 와도(파이프라인의 실제 사용
        # 형태) JSON 직렬화/역직렬화를 거쳐 리스트로 round-trip 해야 한다.
        repository.upsert(
            self.conn, record(mesh_terms=("Retinol", "Skin Aging")), verification()
        )
        row = self.conn.execute("SELECT mesh_terms FROM papers").fetchone()
        self.assertEqual(json.loads(row["mesh_terms"]), ["Retinol", "Skin Aging"])
        got = repository.all_records(self.conn)
        self.assertEqual(got[0]["mesh_terms"], ["Retinol", "Skin Aging"])

    def test_an_absent_mesh_terms_stays_null(self):
        # NULL = "아직 PubMed 를 조회하지 않았다 또는 무결과"(m0006 docstring).
        repository.upsert(self.conn, record(), verification())
        row = self.conn.execute("SELECT mesh_terms FROM papers").fetchone()
        self.assertIsNone(row["mesh_terms"])

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

    def test_an_empty_mesh_terms_tuple_is_stored_as_null_and_restored_as_an_empty_list(self):
        # T10 — pipeline.enrich() 가 "PubMed 는 응답했지만 MeSH 가 없었다"를
        # 빈 튜플로 기록한다. m0006 docstring 대로 그 상태도 NULL 로 접힌다
        # (즉 "무결과"와 "아직 조회 안 함"을 이 컬럼만으로는 구분하지 않는다).
        repository.upsert(self.conn, record(mesh_terms=()), verification())
        row = self.conn.execute("SELECT mesh_terms FROM papers").fetchone()
        self.assertIsNone(row["mesh_terms"])
        self.assertEqual(self._stored()["mesh_terms"], [])

    def test_a_poor_re_collection_does_not_erase_previously_collected_mesh_terms(self):
        # PubMed 조회를 건너뛴(또는 실패한) 재수집이 예전에 얻은 MeSH 를
        # 지우면 안 된다 — 다른 content 필드(abstract/tldr)와 같은 병합 규칙.
        repository.upsert(self.conn, record(mesh_terms=("Retinol", "Skin Aging")), verification())
        repository.upsert(self.conn, record(mesh_terms=None), verification())
        self.assertEqual(self._stored()["mesh_terms"], ["Retinol", "Skin Aging"])

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


def oa_record(**overrides):
    base = {
        "doi": "10.1/oa",
        "is_oa": True,
        "oa_status": "hybrid",
        "pdf_url": "https://example.org/article.pdf",
        "landing_url": "https://example.org/landing",
        "host_type": "publisher",
        "license": "cc-by",
        "checked_at": "2026-08-21T00:00:00Z",
    }
    base.update(overrides)
    return OaLocationRecord(**base)


@dataclass(frozen=True, slots=True)
class _FakeTupleRecord:
    """upsert_records() 의 일반 동작(tuple 직렬화·bool 직렬화·모르는 타입
    KeyError)을 검증하기 위한 테스트 전용 dataclass — 실제 소스가 만드는
    타입이 아니다. TABLE_FOR 는 각 테스트에서 mock.patch.dict 로 임시
    등록한다(프로덕션 TABLE_FOR 를 이 가짜 타입으로 오염시키지 않기 위해)."""

    NATURAL_KEY: ClassVar[tuple[str, ...]] = ("key",)

    key: str
    tags: tuple[str, ...]
    active: bool


class UpsertRecordsTest(unittest.TestCase):
    """upsert_records()/TABLE_FOR — dataclass 레코드를 자연키 기준으로 upsert."""

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

    def test_inserts_a_new_oa_location_row(self):
        count = repository.upsert_records(self.conn, [oa_record()])
        self.assertEqual(count, 1)
        row = self.conn.execute("SELECT * FROM oa_location WHERE doi = '10.1/oa'").fetchone()
        self.assertEqual(row["oa_status"], "hybrid")
        self.assertEqual(row["pdf_url"], "https://example.org/article.pdf")
        self.assertEqual(row["is_oa"], 1)

    def test_conflicting_natural_key_overwrites_every_column_not_merges_it(self):
        """"overwrite" 정책 확인의 핵심: papers.upsert() 의 COALESCE 병합과 달리,
        새 값이 None 이어도(예: 이번엔 PDF 직링크가 없어졌다) 옛 값을 지키지
        않고 그대로 NULL 로 덮어써야 한다."""
        repository.upsert_records(self.conn, [oa_record()])
        repository.upsert_records(
            self.conn,
            [
                oa_record(
                    is_oa=False,
                    oa_status="closed",
                    pdf_url=None,
                    landing_url=None,
                    host_type=None,
                    license=None,
                    checked_at="2026-08-22T00:00:00Z",
                )
            ],
        )
        count = self.conn.execute("SELECT COUNT(*) FROM oa_location").fetchone()[0]
        self.assertEqual(count, 1, "같은 doi 는 한 행으로 유지되어야 한다")
        row = self.conn.execute("SELECT * FROM oa_location WHERE doi = '10.1/oa'").fetchone()
        self.assertEqual(row["oa_status"], "closed")
        self.assertIsNone(row["pdf_url"])
        self.assertEqual(row["is_oa"], 0)
        self.assertEqual(row["checked_at"], "2026-08-22T00:00:00Z")

    def test_serializes_bool_fields_as_zero_or_one(self):
        repository.upsert_records(self.conn, [oa_record(is_oa=False)])
        row = self.conn.execute("SELECT is_oa FROM oa_location WHERE doi = '10.1/oa'").fetchone()
        self.assertEqual(row["is_oa"], 0)

    def test_serializes_tuple_fields_as_a_json_string(self):
        self.conn.executescript(
            "CREATE TABLE fake_tuple_table (key TEXT PRIMARY KEY, tags TEXT, active INTEGER)"
        )
        record = _FakeTupleRecord(key="a", tags=("x", "y"), active=True)
        with mock.patch.dict(
            repository.TABLE_FOR, {_FakeTupleRecord: ("fake_tuple_table", "overwrite")}
        ):
            repository.upsert_records(self.conn, [record])
        row = self.conn.execute("SELECT tags, active FROM fake_tuple_table").fetchone()
        self.assertEqual(json.loads(row["tags"]), ["x", "y"])
        self.assertEqual(row["active"], 1)

    def test_raises_key_error_for_an_unregistered_record_type(self):
        with self.assertRaises(KeyError) as ctx:
            repository.upsert_records(self.conn, [_FakeTupleRecord(key="a", tags=(), active=True)])
        self.assertIn("OaLocationRecord", str(ctx.exception))

    def test_returns_zero_for_an_empty_iterable(self):
        self.assertEqual(repository.upsert_records(self.conn, []), 0)


def retraction_record(**overrides):
    base = {
        "doi": "10.1/retracted",
        "retraction_doi": "10.1/notice",
        "update_type": "retraction",
        "update_date": "2024-03-15",
        "source": "crossref",
    }
    base.update(overrides)
    return RetractionRecord(**base)


class RetractionRecordUpsertTest(unittest.TestCase):
    """T9: RetractionRecord 를 retraction 테이블에 저장. retraction_doi 가
    None 이면 저장 전에 '' 로 강제되어야 한다(PK NULL 특례 방어 — 자세한
    이유는 repository.upsert_records() docstring 참고)."""

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

    def test_inserts_a_new_retraction_row(self):
        count = repository.upsert_records(self.conn, [retraction_record()])
        self.assertEqual(count, 1)
        row = self.conn.execute(
            "SELECT * FROM retraction WHERE doi = '10.1/retracted'"
        ).fetchone()
        self.assertEqual(row["retraction_doi"], "10.1/notice")
        self.assertEqual(row["update_type"], "retraction")
        self.assertEqual(row["update_date"], "2024-03-15")
        self.assertEqual(row["source"], "crossref")

    def test_none_retraction_doi_is_coerced_to_an_empty_string_not_null(self):
        repository.upsert_records(self.conn, [retraction_record(retraction_doi=None)])
        row = self.conn.execute(
            "SELECT retraction_doi FROM retraction WHERE doi = '10.1/retracted'"
        ).fetchone()
        self.assertEqual(row["retraction_doi"], "")
        self.assertIsNotNone(row["retraction_doi"])

    def test_repeated_upserts_with_a_none_retraction_doi_do_not_pile_up_duplicate_rows(self):
        """SQLite 는 PK 컬럼의 NULL 도 서로 "다르다"고 보므로, 강제 coercion
        없이는 (doi, NULL) 이 upsert 될 때마다 새 행이 쌓인다 — 이 테스트가
        그 특례를 실제로 피해가는지 확인한다."""
        repository.upsert_records(self.conn, [retraction_record(retraction_doi=None)])
        repository.upsert_records(
            self.conn, [retraction_record(retraction_doi=None, update_date="2024-04-01")]
        )
        count = self.conn.execute(
            "SELECT COUNT(*) FROM retraction WHERE doi = '10.1/retracted'"
        ).fetchone()[0]
        self.assertEqual(count, 1, "같은 (doi, '') 자연키는 한 행으로 유지되어야 한다")
        row = self.conn.execute(
            "SELECT update_date FROM retraction WHERE doi = '10.1/retracted'"
        ).fetchone()
        self.assertEqual(row["update_date"], "2024-04-01", "overwrite 정책이 최신값으로 갱신")

    def test_conflicting_natural_key_overwrites_rather_than_duplicates(self):
        repository.upsert_records(self.conn, [retraction_record()])
        repository.upsert_records(self.conn, [retraction_record(update_type="retraction-updated")])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM retraction WHERE doi = '10.1/retracted'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        row = self.conn.execute(
            "SELECT update_type FROM retraction WHERE doi = '10.1/retracted'"
        ).fetchone()
        self.assertEqual(row["update_type"], "retraction-updated")

    def test_a_different_retraction_doi_for_the_same_paper_is_a_separate_row(self):
        """같은 논문이 서로 다른 철회 공지 DOI 를 두 번 관측할 수 있다(예:
        공지 DOI 가 나중에 정정되는 경우) — (doi, retraction_doi) 복합키라
        별도 행으로 남는다."""
        repository.upsert_records(self.conn, [retraction_record(retraction_doi="10.1/notice-a")])
        repository.upsert_records(self.conn, [retraction_record(retraction_doi="10.1/notice-b")])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM retraction WHERE doi = '10.1/retracted'"
        ).fetchone()[0]
        self.assertEqual(count, 2)


class OaPdfUrlsTest(unittest.TestCase):
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

    def test_returns_pdf_url_for_a_known_doi(self):
        repository.upsert_records(self.conn, [oa_record(doi="10.1/a")])
        got = repository.oa_pdf_urls(self.conn, ["10.1/a"])
        self.assertEqual(got, {"10.1/a": "https://example.org/article.pdf"})

    def test_omits_dois_with_no_pdf_url(self):
        repository.upsert_records(self.conn, [oa_record(doi="10.1/a", pdf_url=None)])
        got = repository.oa_pdf_urls(self.conn, ["10.1/a"])
        self.assertEqual(got, {})

    def test_omits_dois_absent_from_oa_location(self):
        got = repository.oa_pdf_urls(self.conn, ["10.1/unknown"])
        self.assertEqual(got, {})

    def test_ignores_empty_or_none_entries_in_the_doi_list(self):
        repository.upsert_records(self.conn, [oa_record(doi="10.1/a")])
        got = repository.oa_pdf_urls(self.conn, ["10.1/a", None, ""])
        self.assertEqual(got, {"10.1/a": "https://example.org/article.pdf"})

    def test_returns_an_empty_dict_for_an_empty_doi_list(self):
        self.assertEqual(repository.oa_pdf_urls(self.conn, []), {})


if __name__ == "__main__":
    unittest.main()
