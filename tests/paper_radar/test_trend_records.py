"""trend.records: 순수 변환 함수 + 새 raw 경로(provider, legacy fallback).

papers_trend/tests/test_papers_trend.py 의 ParseDateTest/BareDoiTest 를
이식했다(로직 변경 없음). 여기에 T6 신규 동작(provider 필드, 새/구 raw 경로
선택)을 추가한다.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paper_radar.trend import records


class ParseDateTest(unittest.TestCase):
    def test_full_date_gives_day_precision(self):
        self.assertEqual(records.parse_date("2024-04-14"), ("2024-04", "day", 2024))

    def test_year_month_gives_month_precision(self):
        self.assertEqual(records.parse_date("2024-04"), ("2024-04", "month", 2024))

    def test_year_only_is_kept_without_a_month_bucket(self):
        # 버리지 않는다. 월별 집계에서만 빠진다.
        self.assertEqual(records.parse_date("2024"), (None, "year_only", 2024))

    def test_missing_date_falls_back_to_publication_year(self):
        self.assertEqual(records.parse_date(None, 2019), (None, "year_only", 2019))

    def test_missing_everything_is_unknown(self):
        self.assertEqual(records.parse_date(None), (None, "unknown", None))

    def test_malformed_date_falls_back_to_the_year_without_raising(self):
        # 13월 99일은 파싱되지 않는다. 연도만 살리고 월별 집계에서 빠진다.
        self.assertEqual(records.parse_date("2024-13-99"), (None, "year_only", 2024))


class BareDoiTest(unittest.TestCase):
    def test_strips_the_resolver_prefix(self):
        self.assertEqual(records.bare_doi("https://doi.org/10.1/A"), "10.1/a")

    def test_returns_none_for_empty(self):
        self.assertIsNone(records.bare_doi(None))


class ToRecordProviderTest(unittest.TestCase):
    """T6: provider 가 레코드 dict 에 붙는다."""

    def test_defaults_to_openalex(self):
        record = records.to_record({"id": "https://openalex.org/W1"}, "sunscreen")
        self.assertEqual(record["provider"], "openalex")

    def test_accepts_an_explicit_provider(self):
        record = records.to_record(
            {"id": "https://openalex.org/W1"}, "sunscreen", provider="pubmed"
        )
        self.assertEqual(record["provider"], "pubmed")


class RawDirResolutionTest(unittest.TestCase):
    """T6: 새 경로(data/raw/{provider}/{query_id}) 우선, 없으면(openalex 한정) 구 경로."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._orig_new = records.NEW_RAW_ROOT
        self._orig_legacy = records.LEGACY_RAW_ROOT
        self.addCleanup(self._restore)
        records.NEW_RAW_ROOT = self.root / "data" / "raw"
        records.LEGACY_RAW_ROOT = self.root / "papers_trend" / "raw"

    def _restore(self):
        records.NEW_RAW_ROOT = self._orig_new
        records.LEGACY_RAW_ROOT = self._orig_legacy

    def test_prefers_the_new_path_when_it_exists(self):
        new_dir = records.new_raw_dir("sunscreen")
        new_dir.mkdir(parents=True)
        (records.LEGACY_RAW_ROOT / "sunscreen").mkdir(parents=True)
        self.assertEqual(records.raw_dir("sunscreen"), new_dir)

    def test_falls_back_to_the_legacy_path_for_openalex_when_the_new_one_is_missing(self):
        legacy_dir = records.LEGACY_RAW_ROOT / "sunscreen"
        legacy_dir.mkdir(parents=True)
        self.assertEqual(records.raw_dir("sunscreen"), legacy_dir)

    def test_does_not_fall_back_for_a_non_openalex_provider(self):
        # 구 경로는 provider 차원이 없던 시절 레이아웃이라 openalex 전용이다.
        (records.LEGACY_RAW_ROOT / "sunscreen").mkdir(parents=True)
        expected_new = records.new_raw_dir("sunscreen", provider="pubmed")
        self.assertEqual(records.raw_dir("sunscreen", provider="pubmed"), expected_new)

    def test_reads_works_from_the_legacy_path_transparently(self):
        legacy_dir = records.LEGACY_RAW_ROOT / "sunscreen"
        legacy_dir.mkdir(parents=True)
        work = {"id": "https://openalex.org/W1", "title": "legacy work"}
        with open(legacy_dir / "2024-01-01.jsonl", "w", encoding="utf-8") as handle:
            handle.write(json.dumps(work) + "\n")
        works = list(records.iter_raw("sunscreen"))
        self.assertEqual([w["title"] for w in works], ["legacy work"])


if __name__ == "__main__":
    unittest.main()
