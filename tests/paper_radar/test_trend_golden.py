"""골든 동일성 테스트 — Task 6 의 핵심 검증.

tests/fixtures/trend_synthetic/ (합성 OpenAlex work 38건)을 신 파이프라인
(paper_radar.trend)에 돌려 tests/fixtures/trend_golden/ 의 CSV 5개와 파일별로
바이트 동일한지 비교한다. 그 골든은 구 papers_trend 코드로 같은 픽스처를
돌려 만든 것이다(tests/fixtures/make_trend_golden.py, papers_trend/ 가 아직
코드로 살아 있던 시점에 생성·커밋) — 이 테스트가 신 코드와 구 코드의 출력이
같음을 고정한다.

CSV 표면(컬럼/순서/값 형식/인코딩)은 과거 산출물과의 조인이 걸린 공개
계약이다. 한 셀이라도 달라지면 이 테스트가 실패해야 한다 — 아래
_assert_files_equal() 은 diff 를 직접 계산해 "어느 파일 몇 번째 줄"까지
실패 메시지에 남긴다(brief 의 요구사항).

골든 재생성 절차는 tests/fixtures/make_trend_golden.py 의 모듈 docstring 을
참고할 것.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paper_radar.trend import aggregate, records, unmatched

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "trend_synthetic"
GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "trend_golden"
QUERY_ID = "synthetic"

GOLDEN_FILES = (
    "monthly_denominator.csv",
    "keyword_monthly.csv",
    "topic_monthly.csv",
    "trend_metrics.csv",
    "unmatched_synthetic_keywords.csv",
)


def _assert_files_equal(test_case, expected_path, actual_path):
    """두 파일을 줄 단위로 비교한다. 다르면 어느 파일 몇 번째 줄인지 메시지에 남긴다."""
    expected_bytes = expected_path.read_bytes()
    actual_bytes = actual_path.read_bytes()
    if expected_bytes == actual_bytes:
        return
    expected_lines = expected_bytes.decode("utf-8-sig").splitlines()
    actual_lines = actual_bytes.decode("utf-8-sig").splitlines()
    for line_no, (expected_line, actual_line) in enumerate(
        zip(expected_lines, actual_lines, strict=False), start=1
    ):
        if expected_line != actual_line:
            test_case.fail(
                f"{expected_path.name}:{line_no} 불일치\n"
                f"  golden : {expected_line!r}\n"
                f"  actual : {actual_line!r}"
            )
    if len(expected_lines) != len(actual_lines):
        test_case.fail(
            f"{expected_path.name}: 줄 수가 다르다 "
            f"(golden {len(expected_lines)}줄, actual {len(actual_lines)}줄)"
        )
    test_case.fail(f"{expected_path.name}: 바이트가 다르지만(인코딩 등) 줄 단위로는 같다")


class TrendGoldenTest(unittest.TestCase):
    def setUp(self):
        self._orig_new_raw_root = records.NEW_RAW_ROOT
        records.NEW_RAW_ROOT = FIXTURE_ROOT / "raw"
        self.addCleanup(self._restore)
        with open(FIXTURE_ROOT / "config.json", encoding="utf-8") as handle:
            self.config = json.load(handle)

    def _restore(self):
        records.NEW_RAW_ROOT = self._orig_new_raw_root

    def test_new_pipeline_matches_the_golden_csvs_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            aggregate.run(QUERY_ID, config=self.config, out_dir=out_dir, allow_sample=False)
            unmatched.run(QUERY_ID, field="keywords_norm", top=200, out_dir=out_dir)

            namespace = out_dir / QUERY_ID
            for filename in GOLDEN_FILES:
                with self.subTest(filename=filename):
                    _assert_files_equal(self, GOLDEN_DIR / filename, namespace / filename)

    def test_a_deliberately_wrong_golden_would_actually_fail(self):
        # 골든 비교 메커니즘 자체가 살아있는지: 골든과 다른 파일을 비교하면
        # 반드시 실패해야 한다(이 테스트는 그 실패를 기대하고 검증한다).
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / "wrong.csv"
            wrong.write_text(
                "month_bucket,query_id,paper_count\n2099-01,x,999\n", encoding="utf-8-sig"
            )
            with self.assertRaises(AssertionError):
                _assert_files_equal(self, GOLDEN_DIR / "monthly_denominator.csv", wrong)


if __name__ == "__main__":
    unittest.main()
