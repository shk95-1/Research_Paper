"""reference.cosing — CosIng CSV 파서. parse_row() 는 순수 함수, iter_records()
는 실제 파일(utf-8-sig, 헤더 이름 기준 매핑)을 왕복한다.
"""

from __future__ import annotations

import csv
import os
import tempfile
import unittest

from paper_radar.reference import cosing

# 브리핑의 "대표 컬럼" 구성 예시 그대로. 실파일 헤더는 다를 수 있다는 게
# 브리핑의 요지라, 이 테스트도 헤더 "이름"만 신뢰하고 순서에는 의존하지 않는다.
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
    """HEADER 의 모든 컬럼을 빈 문자열로 채운 뒤 overrides 로 덮어쓴 dict."""
    base = dict.fromkeys(HEADER, "")
    base.update(overrides)
    return base


class ParseRowTest(unittest.TestCase):
    """parse_row() — 순수 함수. 네트워크·디스크 없음."""

    def test_parses_a_normal_row_into_an_ingredient_record(self):
        record = cosing.parse_row(
            _row(
                **{
                    "COSING Ref No": "12345",
                    "INCI name": "NIACINAMIDE",
                    "INN name": "Niacinamide",
                    "CAS No": "98-92-0",
                }
            )
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.name_key, "niacinamide")
        self.assertEqual(record.inci_name, "NIACINAMIDE")
        self.assertEqual(record.cas, "98-92-0")
        self.assertEqual(record.synonyms, ("Niacinamide",))
        self.assertEqual(record.sources, ("cosing",))
        self.assertIsNone(record.cid, "CosIng 은 CID 를 모른다 — merge 가 PubChem 값을 보존한다")

    def test_returns_none_when_inci_name_is_missing(self):
        record = cosing.parse_row(_row(**{"CAS No": "98-92-0"}))
        self.assertIsNone(record)

    def test_returns_none_when_inci_name_is_only_whitespace(self):
        record = cosing.parse_row(_row(**{"INCI name": "   "}))
        self.assertIsNone(record)

    def test_normalizes_an_empty_cas_number_to_none(self):
        record = cosing.parse_row(_row(**{"INCI name": "RETINOL", "CAS No": ""}))
        self.assertIsNotNone(record)
        self.assertIsNone(record.cas)

    def test_synonyms_exclude_columns_that_are_empty(self):
        # INN name 은 있고 Chem/IUPAC 는 없는 경우 — 빈 값이 synonyms 에 끼면
        # merge 시 빈 문자열이 "동의어"로 영구히 남는다(브리핑/모듈 docstring 참고).
        record = cosing.parse_row(
            _row(**{"INCI name": "RETINOL", "INN name": "Retinol", "CAS No": "68-26-8"})
        )
        self.assertEqual(record.synonyms, ("Retinol",))

    def test_works_regardless_of_the_dict_key_order(self):
        # csv.DictReader 는 dict 를 만들지만, 여기서는 일부러 컬럼 순서를
        # 뒤섞어 구성한다 — parse_row() 가 위치가 아니라 헤더 이름으로 찾는지
        # 확인한다("컬럼은 헤더 이름으로 찾는다" — 브리핑 명세).
        scrambled = {
            "CAS No": "68-26-8",
            "Update Date": "2024-01-01",
            "INCI name": "RETINOL",
            "Function": "Skin conditioning",
            "COSING Ref No": "999",
        }
        record = cosing.parse_row(scrambled)
        self.assertIsNotNone(record)
        self.assertEqual(record.name_key, "retinol")
        self.assertEqual(record.cas, "68-26-8")


class IterRecordsTest(unittest.TestCase):
    """iter_records() — 실제 CSV 파일(utf-8-sig)을 왕복."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _write_fixture(self, rows: list[dict]) -> None:
        # utf-8-sig 로 쓴다 — 엑셀 내보내기가 흔히 붙이는 BOM 을 재현해,
        # iter_records() 가 그 BOM 을 첫 컬럼 헤더에서 제대로 걷어내는지
        # 확인한다.
        with open(self.path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADER)
            writer.writeheader()
            writer.writerows(rows)

    def test_round_trips_a_five_row_utf8_sig_fixture(self):
        # 구성 예시: 정상 2행 + INCI name 없는 1행(건너뜀) + CAS 빈 1행 +
        # synonyms 없는 1행. 총 5행 중 4건만 IngredientRecord 로 나와야 한다.
        self._write_fixture(
            [
                _row(
                    **{
                        "INCI name": "NIACINAMIDE",
                        "INN name": "Niacinamide",
                        "CAS No": "98-92-0",
                    }
                ),
                _row(**{"INCI name": "RETINOL", "CAS No": "68-26-8"}),
                _row(**{"CAS No": "9999-99-9"}),  # INCI name 없음 -> 건너뜀
                _row(**{"INCI name": "TOCOPHEROL", "CAS No": ""}),  # CAS 빈 문자열
                _row(**{"INCI name": "WATER"}),  # synonyms 없음
            ]
        )
        records = list(cosing.iter_records(self.path, fetched_at="2026-08-21T00:00:00Z"))

        self.assertEqual(len(records), 4)
        by_key = {r.name_key: r for r in records}
        self.assertEqual(set(by_key), {"niacinamide", "retinol", "tocopherol", "water"})
        self.assertIsNone(by_key["tocopherol"].cas)
        self.assertEqual(by_key["water"].synonyms, ())
        self.assertTrue(all(r.fetched_at == "2026-08-21T00:00:00Z" for r in records))
        self.assertTrue(all(r.sources == ("cosing",) for r in records))

    def test_fetched_at_stays_the_placeholder_when_not_provided(self):
        self._write_fixture([_row(**{"INCI name": "RETINOL"})])
        (record,) = list(cosing.iter_records(self.path))
        self.assertEqual(record.fetched_at, "")


if __name__ == "__main__":
    unittest.main()
