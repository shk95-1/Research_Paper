"""trend.mesh_aggregate: PubMed MeSH 월별 집계(mesh_monthly.csv).

합성 raw JSONL 픽스처(이 파일 안에서 직접 만든다 — trend_synthetic/ 은
openalex 골든 전용이라 pubmed 픽스처를 거기 얹지 않는다)로 검산한다:
denominator(total_papers 가 그 달의 실제 raw 건수와 일치), prevalence,
is_low_sample/is_provisional, 한 논문 안의 대소문자 중복 접기, utf-8-sig
인코딩. is_low_sample/is_provisional 판정 함수가 trend.aggregate 의 것과
동일 객체(재사용, 중복 구현 아님)인지도 확인한다.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from paper_radar.trend import aggregate, mesh_aggregate, records


def _write_raw(directory, filename, rows):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / filename, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_meta(directory, meta):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "_meta.json", "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False)


RAW_ROWS = [
    {
        "pmid": "1",
        "doi": "10.1/1",
        "title": "Sunscreen paper 1",
        "journal": "J1",
        "mesh_terms": ["Sunscreening Agents", "sunscreening agents"],  # 대소문자만 다른 중복
        "month_bucket": "2024-01",
        "provider": "pubmed",
    },
    {
        "pmid": "2",
        "doi": "10.1/2",
        "title": "Sunscreen paper 2",
        "journal": "J1",
        "mesh_terms": ["Skin Neoplasms"],
        "month_bucket": "2024-01",
        "provider": "pubmed",
    },
    {
        "pmid": "3",
        "doi": None,
        "title": "Sunscreen paper 3",
        "journal": "J2",
        "mesh_terms": ["Sunscreening Agents", "Skin Neoplasms"],
        "month_bucket": "2024-01",
        "provider": "pubmed",
    },
    {
        "pmid": "4",
        "doi": "10.1/4",
        "title": "Sunscreen paper 4",
        "journal": "J2",
        "mesh_terms": ["Sunscreening Agents"],
        "month_bucket": "2024-02",
        "provider": "pubmed",
    },
    {
        "pmid": "5",
        "doi": "10.1/5",
        "title": "Sunscreen paper 5 without mesh terms yet",
        "journal": "J2",
        "mesh_terms": [],
        "month_bucket": "2024-02",
        "provider": "pubmed",
    },
]

CONFIG = {
    "low_sample_threshold": 3,
    "provisional_months": 1,
}


class MeshAggregateFixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_root = Path(self._tmp.name) / "data" / "raw"
        self._orig_new_raw_root = records.NEW_RAW_ROOT
        records.NEW_RAW_ROOT = self.raw_root
        self.addCleanup(self._restore)

        directory = records.new_raw_dir("sunscreen", "pubmed")
        _write_raw(directory, "2024-02-15.jsonl", RAW_ROWS)
        _write_meta(
            directory,
            {
                "is_census": True,
                "collected": 5,
                "expected_from_api": 5,
                "collected_at": "2024-02-15T00:00:00Z",
            },
        )

    def _restore(self):
        records.NEW_RAW_ROOT = self._orig_new_raw_root


class MeshMonthlyDenominatorTest(MeshAggregateFixtureCase):
    """denominator 검산: mesh_monthly 의 total_papers 가 그 달 raw 건수와 일치.

    한 행의 total_papers 는 그 달의 분모(라인 전체 건수)를 그대로 반복해
    싣는다(용어마다 새로 세는 값이 아니다) — 그래서 검산은 "모든 행의
    total_papers 를 합산"이 아니라 "월별로 유일한 total_papers 값이 그 달의
    실제 raw 건수와 같다"로 한다(합산하면 그 달에 등장한 서로 다른 MeSH
    용어 수만큼 중복 계산된다).
    """

    def test_total_papers_matches_the_actual_raw_count_per_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = mesh_aggregate.run("sunscreen", config=CONFIG, out_dir=tmp)
        rows = result["rows"]
        total_papers_by_month = {row["month_bucket"]: row["total_papers"] for row in rows}
        self.assertEqual(total_papers_by_month, {"2024-01": 3, "2024-02": 2})
        # 월별로 total_papers 값이 단 하나로 고정돼 있어야 한다(용어마다 달라지면 안 된다).
        for month in ("2024-01", "2024-02"):
            values = {row["total_papers"] for row in rows if row["month_bucket"] == month}
            self.assertEqual(values, {total_papers_by_month[month]})

    def test_records_returned_equals_the_deduplicated_raw_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = mesh_aggregate.run("sunscreen", config=CONFIG, out_dir=tmp)
        self.assertEqual(result["records"], 5)


class MeshMonthlyPrevalenceTest(MeshAggregateFixtureCase):
    def test_a_mesh_term_repeated_within_one_paper_counts_once(self):
        # pmid 1 은 "Sunscreening Agents"/"sunscreening agents" 를 둘 다
        # 갖지만, 소문자화 후 같은 용어라 그 논문에서는 한 번만 센다.
        with tempfile.TemporaryDirectory() as tmp:
            result = mesh_aggregate.run("sunscreen", config=CONFIG, out_dir=tmp)
        jan_sunscreening = [
            row
            for row in result["rows"]
            if row["month_bucket"] == "2024-01" and row["mesh_term"] == "sunscreening agents"
        ]
        self.assertEqual(len(jan_sunscreening), 1)
        self.assertEqual(jan_sunscreening[0]["paper_count"], 2)  # pmid 1, 3 (pmid 1 은 한 번만)
        # round_or_none(digits=6)(aggregate.py 와 같은 반올림 규칙)으로 저장되므로
        # 그 반올림을 거친 값과 비교한다.
        self.assertEqual(jan_sunscreening[0]["prevalence"], round(2 / 3, 6))

    def test_mesh_term_is_lowercased_but_not_lexicon_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = mesh_aggregate.run("sunscreen", config=CONFIG, out_dir=tmp)
        terms = {row["mesh_term"] for row in result["rows"]}
        self.assertIn("sunscreening agents", terms)
        self.assertIn("skin neoplasms", terms)
        # 원문 표기가 그대로(소문자화 외에는 손대지 않는다) — 대문자 표기는 없어야 한다.
        self.assertTrue(all(term == term.lower() for term in terms))

    def test_a_paper_with_no_mesh_terms_contributes_to_the_denominator_only(self):
        # pmid 5(2024-02, mesh_terms=[])는 total_papers 에는 들어가지만
        # 어떤 mesh_term 행도 만들지 않는다.
        with tempfile.TemporaryDirectory() as tmp:
            result = mesh_aggregate.run("sunscreen", config=CONFIG, out_dir=tmp)
        feb_rows = [row for row in result["rows"] if row["month_bucket"] == "2024-02"]
        self.assertEqual(len(feb_rows), 1)  # "sunscreening agents" 하나뿐(pmid 4)
        self.assertEqual(feb_rows[0]["total_papers"], 2)  # 분모에는 pmid 5 도 들어간다


class MeshMonthlyFlagsTest(MeshAggregateFixtureCase):
    def test_low_sample_threshold_is_applied_per_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = mesh_aggregate.run("sunscreen", config=CONFIG, out_dir=tmp)
        by_month = {row["month_bucket"]: row["is_low_sample"] for row in result["rows"]}
        self.assertFalse(by_month["2024-01"])  # 3편, threshold=3 -> 3 < 3 은 False
        self.assertTrue(by_month["2024-02"])  # 2편, 2 < 3 은 True

    def test_provisional_month_is_flagged_from_collected_at(self):
        # collected_at=2024-02-15, provisional_months=1 -> {"2024-02"} 만 provisional.
        with tempfile.TemporaryDirectory() as tmp:
            result = mesh_aggregate.run("sunscreen", config=CONFIG, out_dir=tmp)
        by_month = {row["month_bucket"]: row["is_provisional"] for row in result["rows"]}
        self.assertFalse(by_month["2024-01"])
        self.assertTrue(by_month["2024-02"])


class MeshMonthlyReuseTest(unittest.TestCase):
    """중복 구현 금지 — aggregate.py 의 판정 함수를 그대로 재사용했는지 직접 확인한다."""

    def test_is_low_sample_is_the_same_function_object_as_aggregate(self):
        self.assertIs(mesh_aggregate.is_low_sample, aggregate.is_low_sample)

    def test_is_provisional_month_is_the_same_function_object_as_aggregate(self):
        self.assertIs(mesh_aggregate.is_provisional_month, aggregate.is_provisional_month)

    def test_census_guard_and_censuserror_are_the_same_objects_as_aggregate(self):
        self.assertIs(mesh_aggregate.census_guard, aggregate.census_guard)
        self.assertIs(mesh_aggregate.CensusError, aggregate.CensusError)


class MeshMonthlyCsvSurfaceTest(MeshAggregateFixtureCase):
    def test_no_citation_columns_are_present(self):
        # 브리핑: PubMed 는 인용수를 제공하지 않는다 — citation 계열 컬럼을 흉내 내지 않는다.
        self.assertEqual(
            mesh_aggregate.MESH_MONTHLY_FIELDS,
            [
                "month_bucket",
                "query_id",
                "mesh_term",
                "paper_count",
                "total_papers",
                "prevalence",
                "is_low_sample",
                "is_provisional",
            ],
        )

    def test_writes_utf8_sig_csv_readable_by_the_csv_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = mesh_aggregate.run("sunscreen", config=CONFIG, out_dir=tmp)
            target = Path(tmp) / "sunscreen" / "mesh_monthly.csv"
            self.assertEqual(target, result["out_dir"] / "mesh_monthly.csv")
            raw_bytes = target.read_bytes()
            self.assertTrue(raw_bytes.startswith(b"\xef\xbb\xbf"))  # utf-8-sig BOM
            with open(target, encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        # jan: sunscreening agents, skin neoplasms / feb: sunscreening agents
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["is_low_sample"] for row in rows}, {"True", "False"})


class MeshAggregateCensusGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_root = Path(self._tmp.name) / "data" / "raw"
        self._orig_new_raw_root = records.NEW_RAW_ROOT
        records.NEW_RAW_ROOT = self.raw_root
        self.addCleanup(self._restore)

        directory = records.new_raw_dir("partial", "pubmed")
        _write_raw(directory, "2024-01-01.jsonl", RAW_ROWS[:2])
        _write_meta(
            directory,
            {
                "is_census": False,
                "collected": 2,
                "expected_from_api": 500,
                "collected_at": "2024-01-01T00:00:00Z",
            },
        )

    def _restore(self):
        records.NEW_RAW_ROOT = self._orig_new_raw_root

    def test_a_non_census_profile_raises_censuserror_without_allow_sample(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(mesh_aggregate.CensusError):
            mesh_aggregate.run("partial", config=CONFIG, out_dir=tmp)

    def test_allow_sample_passes_through_with_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = mesh_aggregate.run(
                    "partial", config=CONFIG, out_dir=tmp, allow_sample=True
                )
            self.assertIn("모집단 비율이 아닙니다", stderr.getvalue())
            self.assertTrue((result["out_dir"] / "mesh_monthly.csv").exists())


if __name__ == "__main__":
    unittest.main()
