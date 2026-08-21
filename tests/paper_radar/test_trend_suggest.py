"""trend.suggest: unmatched 검수용 PubChem/CosIng 동의어 후보 제안(T14).

build_suggestions() 는 순수 함수라 정확 일치 매칭 규칙(name_key/inci_name/
synonym, 대소문자·공백 정규화 경유, 불일치 제외, 기존 lexicon 키 병기)을
여기서 직접 검증한다. run() 은 픽스처 unmatched CSV + 실제 SQLite ingredient
테이블로 end-to-end(파일 IO 포함)를 검증한다.
"""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

from paper_radar.models import IngredientRecord
from paper_radar.storage import repository
from paper_radar.trend import suggest, unmatched


def _ingredient(**overrides):
    base = {
        "name_key": "niacinamide",
        "inci_name": None,
        "cid": 936,
        "cas": "98-92-0",
        "synonyms": (),
        "sources": ("pubchem",),
    }
    base.update(overrides)
    return base


def _unmatched_row(**overrides):
    base = {
        "rank": "1",
        "term": "niacinamide",
        "paper_count": "12",
        "first_seen_month": "2024-01",
        "last_seen_month": "2024-06",
        "months_present": "3",
        "example_title_1": "",
        "example_title_2": "",
        "example_title_3": "",
        "verdict": "",
    }
    base.update(overrides)
    return base


class BuildSuggestionsMatchingTest(unittest.TestCase):
    """정확 일치 매칭 규칙 하나하나를 확인한다."""

    def test_matches_by_name_key(self):
        rows = suggest.build_suggestions(
            [_unmatched_row(term="niacinamide")],
            [_ingredient(name_key="niacinamide")],
            lexicon_keys=set(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matched_via"], "name_key")
        self.assertEqual(rows[0]["matched_name_key"], "niacinamide")

    def test_matches_by_inci_name(self):
        rows = suggest.build_suggestions(
            [_unmatched_row(term="tocopherol")],
            [_ingredient(name_key="vitamin e", inci_name="Tocopherol")],
            lexicon_keys=set(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matched_via"], "inci_name")
        self.assertEqual(rows[0]["matched_name_key"], "vitamin e")

    def test_matches_by_synonym_and_records_the_original_synonym_text(self):
        rows = suggest.build_suggestions(
            [_unmatched_row(term="ZnO")],
            [_ingredient(name_key="zinc oxide", synonyms=("ZnO", "zinc oxide"))],
            lexicon_keys=set(),
        )
        self.assertEqual(len(rows), 1)
        # matched_via 는 정규화 전 원문을 그대로 담는다(브리핑 명세).
        self.assertEqual(rows[0]["matched_via"], "synonym:ZnO")

    def test_excludes_unmatched_rows_with_no_ingredient_hit(self):
        rows = suggest.build_suggestions(
            [_unmatched_row(term="a totally unknown compound")],
            [_ingredient(name_key="niacinamide")],
            lexicon_keys=set(),
        )
        self.assertEqual(rows, [])

    def test_matches_across_case_and_whitespace_via_normalize_term(self):
        rows = suggest.build_suggestions(
            [_unmatched_row(term="  Niacinamide  ")],
            [_ingredient(name_key="niacinamide")],
            lexicon_keys=set(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matched_via"], "name_key")

    def test_rejects_partial_matches(self):
        """부분 일치는 매칭이 아니다 — 유사도 매칭 금지(브리핑 지시)."""
        rows = suggest.build_suggestions(
            [_unmatched_row(term="niacinamide serum")],
            [_ingredient(name_key="niacinamide")],
            lexicon_keys=set(),
        )
        self.assertEqual(rows, [])


class BuildSuggestionsSuggestedKeyTest(unittest.TestCase):
    def test_converts_name_key_to_uppercase_snake_case(self):
        rows = suggest.build_suggestions(
            [_unmatched_row(term="zinc oxide")],
            [_ingredient(name_key="zinc oxide")],
            lexicon_keys=set(),
        )
        self.assertEqual(rows[0]["suggested_key"], "ZINC_OXIDE")

    def test_annotates_when_the_key_already_exists_in_the_lexicon(self):
        rows = suggest.build_suggestions(
            [_unmatched_row(term="zinc oxide")],
            [_ingredient(name_key="zinc oxide")],
            lexicon_keys={"ZINC_OXIDE"},
        )
        self.assertEqual(rows[0]["suggested_key"], "ZINC_OXIDE (기존 키 있음)")

    def test_leaves_the_verdict_column_blank(self):
        """제안은 결정이 아니다 — verdict 는 항상 사람 몫."""
        rows = suggest.build_suggestions(
            [_unmatched_row(term="niacinamide")],
            [_ingredient(name_key="niacinamide")],
            lexicon_keys=set(),
        )
        self.assertEqual(rows[0]["verdict"], "")


class BuildSuggestionsSortTest(unittest.TestCase):
    def test_sorts_by_paper_count_descending(self):
        rows = suggest.build_suggestions(
            [
                _unmatched_row(term="niacinamide", paper_count="3"),
                _unmatched_row(term="zinc oxide", paper_count="10"),
            ],
            [_ingredient(name_key="niacinamide"), _ingredient(name_key="zinc oxide")],
            lexicon_keys=set(),
        )
        self.assertEqual([row["term"] for row in rows], ["zinc oxide", "niacinamide"])


class RunTest(unittest.TestCase):
    """픽스처 unmatched CSV + 실제 ingredient 테이블로 end-to-end."""

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.db_path)
        self.addCleanup(self._cleanup_db)
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)

    def _cleanup_db(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _write_unmatched_csv(self, query_id, rows):
        path = unmatched.default_path(query_id, "keywords_norm", out_dir=self.tmp_dir.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=unmatched.FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_writes_lexicon_suggestions_csv_round_tripping_utf8_sig(self):
        self._write_unmatched_csv(
            "sunscreen",
            [_unmatched_row(term="niacinamide", paper_count="7")],
        )
        conn = repository.connect(self.db_path)
        repository.upsert_records(
            conn,
            [
                IngredientRecord(
                    name_key="niacinamide",
                    inci_name="Niacinamide",
                    cid=936,
                    cas="98-92-0",
                    synonyms=("nicotinamide",),
                    sources=("pubchem",),
                    fetched_at="2026-08-21T00:00:00Z",
                )
            ],
        )
        conn.close()

        result = suggest.run("sunscreen", self.db_path, out_dir=self.tmp_dir.name)

        self.assertFalse(result["ingredient_table_empty"])
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(result["suggested"], 1)
        expected = Path(self.tmp_dir.name) / "sunscreen" / "lexicon_suggestions.csv"
        self.assertEqual(result["target"], expected)
        self.assertTrue(expected.exists())

        with open(expected, encoding="utf-8-sig", newline="") as fh:
            written_rows = list(csv.DictReader(fh))
        self.assertEqual(len(written_rows), 1)
        self.assertEqual(written_rows[0]["term"], "niacinamide")
        self.assertEqual(written_rows[0]["matched_name_key"], "niacinamide")
        self.assertEqual(written_rows[0]["verdict"], "")

    def test_returns_an_empty_result_without_writing_a_file_when_ingredient_table_is_empty(self):
        self._write_unmatched_csv("sunscreen", [_unmatched_row(term="niacinamide")])
        repository.connect(self.db_path).close()  # DB 는 만들되 ingredient 는 비워 둔다.

        result = suggest.run("sunscreen", self.db_path, out_dir=self.tmp_dir.name)

        self.assertTrue(result["ingredient_table_empty"])
        self.assertIsNone(result["target"])
        self.assertEqual(result["rows"], [])
        expected = Path(self.tmp_dir.name) / "sunscreen" / "lexicon_suggestions.csv"
        self.assertFalse(expected.exists())


if __name__ == "__main__":
    unittest.main()
