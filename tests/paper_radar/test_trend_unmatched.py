"""trend.unmatched: 사전 미매칭 표현 집계 + 출력 경로.

papers_trend 에는 unmatched.py 전용 단위 테스트가 없었다(사람이 보는 도구라
CSV 표면은 tests/fixtures/trend_golden/unmatched_synthetic_keywords.csv 가
검증한다). 여기서는 T6 신규(default_path 의 네임스페이스, run() 이 실제로
그 경로에 쓰는지)만 다룬다.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_radar.trend import unmatched


class DefaultPathTest(unittest.TestCase):
    def test_keeps_the_legacy_filename_under_the_query_id_namespace(self):
        path = unmatched.default_path("sunscreen", "keywords_norm", out_dir="/tmp/out/trend")
        self.assertEqual(path, Path("/tmp/out/trend/sunscreen/unmatched_sunscreen_keywords.csv"))

    def test_strips_the_norm_suffix_from_the_field_name(self):
        path = unmatched.default_path("sunscreen", "topics_norm", out_dir="/tmp/out/trend")
        self.assertEqual(path.name, "unmatched_sunscreen_topics.csv")


class CollectUnmatchedTest(unittest.TestCase):
    def test_skips_terms_already_in_the_lexicon(self):
        record_list = [{"title": "A", "month_bucket": "2024-01"}]
        normalized = [
            {
                "keywords_norm": [
                    {"keyword_key": "ZINC_OXIDE", "is_in_lexicon": True},
                    {"keyword_key": "some novel term", "is_in_lexicon": False},
                ]
            }
        ]
        stats = unmatched.collect_unmatched(record_list, normalized)
        self.assertEqual(list(stats), ["some novel term"])

    def test_caps_example_titles_at_three(self):
        record_list = [{"title": f"Title {i}", "month_bucket": "2024-01"} for i in range(5)]
        normalized = [
            {"keywords_norm": [{"keyword_key": "novel term", "is_in_lexicon": False}]}
            for _ in range(5)
        ]
        stats = unmatched.collect_unmatched(record_list, normalized)
        self.assertEqual(stats["novel term"]["papers"], 5)
        self.assertEqual(len(stats["novel term"]["titles"]), 3)


class ToRowsTest(unittest.TestCase):
    def test_ranks_by_paper_count_descending_then_term(self):
        stats = {
            "b term": {"papers": 1, "titles": [], "months": set()},
            "a term": {"papers": 3, "titles": [], "months": set()},
        }
        rows = unmatched.to_rows(stats)
        self.assertEqual([r["term"] for r in rows], ["a term", "b term"])
        self.assertEqual([r["rank"] for r in rows], [1, 2])

    def test_top_truncates_the_row_count(self):
        stats = {f"term{i}": {"papers": i, "titles": [], "months": set()} for i in range(5)}
        rows = unmatched.to_rows(stats, top=2)
        self.assertEqual(len(rows), 2)


class RunTest(unittest.TestCase):
    """합성 픽스처를 재료로 run() 이 실제 파일을 쓰는지 확인한다."""

    FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "trend_synthetic"

    def setUp(self):
        from paper_radar.trend import records

        self._records = records
        self._orig = records.NEW_RAW_ROOT
        records.NEW_RAW_ROOT = self.FIXTURE_ROOT / "raw"
        self.addCleanup(self._restore)

    def _restore(self):
        self._records.NEW_RAW_ROOT = self._orig

    def test_writes_under_the_query_id_namespace_with_the_legacy_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = unmatched.run("synthetic", out_dir=tmp)
            expected = Path(tmp) / "synthetic" / "unmatched_synthetic_keywords.csv"
            self.assertEqual(result["target"], expected)
            self.assertTrue(expected.exists())


if __name__ == "__main__":
    unittest.main()
