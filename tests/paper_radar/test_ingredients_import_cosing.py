"""ingredients.import_cosing.run() — 픽스처 CSV 를 실제로 읽어 저장·RunLog
자기기록(run/run_source)·merge 정책을 검증한다. 네트워크가 전혀 없다는 게
ingredients.resolve.run()/trials.collect.run() 과의 차이라, FakeSession/
Transport 없이 conn 하나만으로 끝난다.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest

from paper_radar.ingredients import import_cosing
from paper_radar.models import IngredientRecord
from paper_radar.storage import repository

HEADER = (
    "COSING Ref No",
    "INCI name",
    "INN name",
    "CAS No",
    "EC No",
    "Chem/IUPAC Name / Description",
    "Function",
    "Restriction",
    "Update Date",
)


def _row(**overrides) -> dict:
    base = dict.fromkeys(HEADER, "")
    base.update(overrides)
    return base


def _pubchem_niacinamide() -> IngredientRecord:
    return IngredientRecord(
        name_key="niacinamide",
        inci_name=None,
        cid=936,
        cas="98-92-0",
        synonyms=("niacinamide", "nicotinamide"),
        sources=("pubchem",),
        fetched_at="2026-08-20T00:00:00Z",
    )


class RunTest(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.db_path)
        self.conn = repository.connect(self.db_path)
        self.addCleanup(self._cleanup_db)

        handle, self.csv_path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.csv_path) and os.unlink(self.csv_path))

    def _cleanup_db(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _write_fixture(self, rows: list[dict]) -> None:
        with open(self.csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADER)
            writer.writeheader()
            writer.writerows(rows)

    def test_reports_read_saved_and_skipped_counts(self):
        self._write_fixture(
            [
                _row(**{"INCI name": "NIACINAMIDE", "CAS No": "98-92-0"}),
                _row(**{"INCI name": "RETINOL", "CAS No": "68-26-8"}),
                _row(**{"CAS No": "no-inci-name"}),  # INCI name 없음 -> 건너뜀
            ]
        )
        report = import_cosing.run(self.conn, self.csv_path)

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.read, 3)
        self.assertEqual(report.saved, 2)
        self.assertEqual(report.skipped, 1)

        stored = self.conn.execute("SELECT COUNT(*) FROM ingredient").fetchone()[0]
        self.assertEqual(stored, 2)

    def test_merges_into_a_pre_existing_pubchem_record_without_erasing_its_fields(self):
        repository.upsert_records(self.conn, [_pubchem_niacinamide()])
        self._write_fixture(
            [_row(**{"INCI name": "NIACINAMIDE", "INN name": "Niacinamide", "CAS No": "98-92-0"})]
        )

        import_cosing.run(self.conn, self.csv_path)

        merged = repository.get_ingredient(self.conn, "niacinamide")
        self.assertEqual(merged.inci_name, "NIACINAMIDE")
        self.assertEqual(merged.cid, 936, "PubChem 이 채운 cid 는 CosIng 이 몰라도 보존돼야 한다")
        self.assertEqual(set(merged.sources), {"pubchem", "cosing"})
        self.assertIn("Niacinamide", merged.synonyms)
        self.assertIn("niacinamide", merged.synonyms, "PubChem 이 채운 synonym 도 남아 있어야 한다")

    def test_records_a_run_and_a_cosing_run_source_row(self):
        self._write_fixture([_row(**{"INCI name": "RETINOL", "CAS No": "68-26-8"})])
        report = import_cosing.run(self.conn, self.csv_path)

        run_row = self.conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (report.run_id,)
        ).fetchone()
        self.assertEqual(run_row["status"], "ok")
        self.assertEqual(run_row["command"], "ingredient import-cosing")
        self.assertIsNotNone(run_row["finished_at"])

        source_row = self.conn.execute(
            "SELECT * FROM run_source WHERE run_id = ? AND source = 'cosing'",
            (report.run_id,),
        ).fetchone()
        self.assertEqual(source_row["requests"], 0, "네트워크를 쓰지 않았다")
        self.assertEqual(source_row["records"], 1)
        self.assertIsNone(source_row["stopped_reason"])

    def test_uses_fetched_at_from_a_sibling_meta_json_when_present(self):
        # tool/fetch_cosing.py 가 CSV 와 같은 디렉터리에 고정 이름("_meta.json")
        # 으로 쓰는 관례를 재현한다. 다른 테스트와의 파일 충돌을 피하려고
        # 전용 임시 디렉터리를 만든다(self.csv_path 의 공유 temp 디렉터리를
        # 그대로 쓰지 않는다).
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "cosing.csv")
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=HEADER)
                writer.writeheader()
                writer.writerow(_row(**{"INCI name": "RETINOL", "CAS No": "68-26-8"}))
            with open(os.path.join(tmp_dir, "_meta.json"), "w", encoding="utf-8") as handle:
                json.dump({"fetched_at": "2026-08-15T00:00:00Z"}, handle)

            fetched_at = import_cosing._resolve_fetched_at(csv_path)
            self.assertEqual(fetched_at, "2026-08-15T00:00:00Z")

            report = import_cosing.run(self.conn, csv_path)
            saved = repository.get_ingredient(self.conn, "retinol")
            self.assertEqual(saved.fetched_at, "2026-08-15T00:00:00Z")
            self.assertEqual(report.saved, 1)

    def test_falls_back_to_now_when_no_meta_json_exists(self):
        fetched_at = import_cosing._resolve_fetched_at(self.csv_path)
        self.assertRegex(fetched_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


if __name__ == "__main__":
    unittest.main()
