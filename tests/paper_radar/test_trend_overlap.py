"""trend.overlap: openalex/pubmed raw 를 DOI 로 대조하는 진단(지표 아님).

both_by_doi/openalex_only/pubmed_only/pubmed_doi_missing 계산과, 한쪽(또는
양쪽) raw 가 없을 때의 MissingRawError 경로를 합성 픽스처로 검증한다.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paper_radar.trend import overlap, records

OPENALEX_ROWS = [
    # id/doi/publication_date/publication_year 는 records.to_record() 가 읽는
    # OpenAlex work 원본 모양이다(trend.records.to_record 참고).
    {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.1/1",
        "title": "Paper 1",
        "publication_date": "2024-01-10",
    },
    {
        "id": "https://openalex.org/W2",
        "doi": "https://doi.org/10.1/2",
        "title": "Paper 2",
        "publication_date": "2024-01-15",
    },
    {
        "id": "https://openalex.org/W3",
        "doi": None,
        "title": "Paper 3 (no DOI)",
        "publication_date": "2024-01-20",
    },
]

PUBMED_ROWS = [
    {
        "pmid": "101",
        "doi": "10.1/1",  # openalex 의 bare_doi() 정규화 후와 같은 값 -> both
        "title": "Paper 1",
        "journal": "J1",
        "mesh_terms": ["Sunscreening Agents"],
        "month_bucket": "2024-01",
        "provider": "pubmed",
    },
    {
        "pmid": "102",
        "doi": "10.1/999",  # openalex 에 없는 DOI -> pubmed_only
        "title": "Paper unique to pubmed",
        "journal": "J1",
        "mesh_terms": [],
        "month_bucket": "2024-01",
        "provider": "pubmed",
    },
    {
        "pmid": "103",
        "doi": None,  # DOI 없음 -> pubmed_doi_missing (pubmed_only 에 섞지 않는다)
        "title": "Paper without a DOI",
        "journal": "J1",
        "mesh_terms": [],
        "month_bucket": "2024-01",
        "provider": "pubmed",
    },
]


def _write_jsonl(directory, filename, rows):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / filename, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_meta(directory, meta):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "_meta.json", "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False)


class OverlapRowsTest(unittest.TestCase):
    """순수 함수 overlap_rows() 를 직접 검증 — raw 읽기와 분리한다."""

    def test_classifies_both_only_and_missing_correctly(self):
        openalex_records = [records.to_record(work, "sunscreen") for work in OPENALEX_ROWS]
        rows = overlap.overlap_rows(openalex_records, PUBMED_ROWS)
        self.assertEqual(len(rows), 1)  # 전부 2024-01
        row = rows[0]
        self.assertEqual(row["month_bucket"], "2024-01")
        self.assertEqual(row["openalex_papers"], 3)
        self.assertEqual(row["pubmed_papers"], 3)
        self.assertEqual(row["both_by_doi"], 1)  # 10.1/1
        self.assertEqual(row["openalex_only"], 1)  # 10.1/2
        self.assertEqual(row["pubmed_only"], 1)  # 10.1/999
        self.assertEqual(row["pubmed_doi_missing"], 1)  # pmid 103

    def test_a_month_with_only_pubmed_records_still_appears(self):
        rows = overlap.overlap_rows([], PUBMED_ROWS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["openalex_papers"], 0)
        self.assertEqual(rows[0]["pubmed_papers"], 3)


class OverlapRunFixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_root = Path(self._tmp.name) / "data" / "raw"
        self._orig_new_raw_root = records.NEW_RAW_ROOT
        records.NEW_RAW_ROOT = self.raw_root
        self.addCleanup(self._restore)

    def _restore(self):
        records.NEW_RAW_ROOT = self._orig_new_raw_root

    def _seed_both(self, query_id):
        oa_dir = records.new_raw_dir(query_id, "openalex")
        _write_jsonl(oa_dir, "2024-01-01.jsonl", OPENALEX_ROWS)
        _write_meta(oa_dir, {"is_census": True, "collected": 3, "expected_from_api": 3})

        pm_dir = records.new_raw_dir(query_id, "pubmed")
        _write_jsonl(pm_dir, "2024-01-01.jsonl", PUBMED_ROWS)
        _write_meta(pm_dir, {"is_census": True, "collected": 3, "expected_from_api": 3})


class RunWritesCsvTest(OverlapRunFixtureCase):
    def test_writes_provider_overlap_csv_under_the_query_id_namespace(self):
        self._seed_both("sunscreen")
        with tempfile.TemporaryDirectory() as tmp:
            result = overlap.run("sunscreen", out_dir=tmp)
            target = Path(tmp) / "sunscreen" / "provider_overlap.csv"
            self.assertEqual(result["target"], target)
            self.assertTrue(target.exists())
            raw_bytes = target.read_bytes()
            self.assertTrue(raw_bytes.startswith(b"\xef\xbb\xbf"))  # utf-8-sig
        self.assertEqual(result["written"], 1)
        self.assertEqual(result["rows"][0]["both_by_doi"], 1)


class RunMissingRawTest(OverlapRunFixtureCase):
    def test_raises_missing_raw_error_when_pubmed_raw_is_absent(self):
        oa_dir = records.new_raw_dir("openalex_only", "openalex")
        _write_jsonl(oa_dir, "2024-01-01.jsonl", OPENALEX_ROWS)
        _write_meta(oa_dir, {"is_census": True, "collected": 3, "expected_from_api": 3})

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(overlap.MissingRawError) as ctx:
                overlap.run("openalex_only", out_dir=tmp)
            self.assertIn("pubmed", str(ctx.exception))
            # 파일을 만들지 않는다(모듈 docstring: 한쪽 raw 가 없으면 파일을 만들지 않는다).
            self.assertFalse((Path(tmp) / "openalex_only").exists())

    def test_raises_missing_raw_error_when_openalex_raw_is_absent(self):
        pm_dir = records.new_raw_dir("pubmed_only", "pubmed")
        _write_jsonl(pm_dir, "2024-01-01.jsonl", PUBMED_ROWS)
        _write_meta(pm_dir, {"is_census": True, "collected": 3, "expected_from_api": 3})

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(overlap.MissingRawError) as ctx:
                overlap.run("pubmed_only", out_dir=tmp)
            self.assertIn("openalex", str(ctx.exception))

    def test_raises_missing_raw_error_when_neither_provider_has_been_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(overlap.MissingRawError) as ctx:
                overlap.run("nothing_yet", out_dir=tmp)
            message = str(ctx.exception)
            self.assertIn("openalex", message)
            self.assertIn("pubmed", message)


if __name__ == "__main__":
    unittest.main()
