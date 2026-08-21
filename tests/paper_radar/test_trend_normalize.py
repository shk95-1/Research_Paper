"""trend.normalize: 표면형 정규화 + 사전/불용어 적용.

papers_trend/tests/test_papers_trend.py 의 NormalizeTermTest/LexiconTest 를
이식했다(로직 변경 없음). 실제 사전/불용어 파일(src/paper_radar/trend/
keyword_lexicon.json, stopwords.json)을 기본 경로로 그대로 쓴다 — 관례 유지.
"""

from __future__ import annotations

import unittest

from paper_radar.trend import normalize


class NormalizeTermTest(unittest.TestCase):
    def test_lowercases_and_collapses_separators(self):
        self.assertEqual(normalize.normalize_term("Anti-Aging"), "anti aging")
        self.assertEqual(normalize.normalize_term("anti_aging"), "anti aging")
        self.assertEqual(normalize.normalize_term("anti   aging"), "anti aging")

    def test_nfkc_folds_chemical_subscripts(self):
        # 4단계 화학식 표기 통일은 NFKC 가 대신한다
        self.assertEqual(normalize.normalize_term("TiO₂"), "tio2")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(normalize.normalize_term("  Zinc Oxide  "), "zinc oxide")

    def test_empty_input_is_empty(self):
        self.assertEqual(normalize.normalize_term(None), "")


class LexiconTest(unittest.TestCase):
    def setUp(self):
        self.entries, self.alias_map = normalize.load_lexicon()
        self.stopwords = normalize.load_stopwords()

    def _resolve(self, term):
        return normalize.resolve(term, self.alias_map, self.entries, self.stopwords)

    def test_chemical_formula_and_name_reach_the_same_key(self):
        self.assertEqual(self._resolve("TiO2")["keyword_key"], "TITANIUM_DIOXIDE")
        self.assertEqual(self._resolve("titanium dioxide")["keyword_key"], "TITANIUM_DIOXIDE")
        self.assertEqual(self._resolve("TiO₂")["keyword_key"], "TITANIUM_DIOXIDE")

    def test_abbreviation_and_expansion_reach_the_same_key(self):
        for term in ("SOD", "sods", "superoxide dismutase", "Cu/Zn-SOD"):
            self.assertEqual(self._resolve(term)["keyword_key"], "SUPEROXIDE_DISMUTASE", term)

    def test_singular_and_plural_reach_the_same_key(self):
        self.assertEqual(self._resolve("antioxidant")["keyword_key"], "ANTIOXIDANT")
        self.assertEqual(self._resolve("antioxidants")["keyword_key"], "ANTIOXIDANT")

    def test_british_and_american_spelling_reach_the_same_key(self):
        self.assertEqual(self._resolve("ageing")["keyword_key"], "ANTI_AGING")
        self.assertEqual(self._resolve("aging")["keyword_key"], "ANTI_AGING")

    def test_hyphen_variants_reach_the_same_key(self):
        for term in ("anti-aging", "anti aging", "antiaging"):
            self.assertEqual(self._resolve(term)["keyword_key"], "ANTI_AGING", term)

    def test_niacinamide_and_nicotinamide_stay_separate(self):
        # 정규화가 과하면 파서는 성공하고 테스트는 초록인데 값만 거짓이 된다
        self.assertEqual(self._resolve("niacinamide")["keyword_key"], "NIACINAMIDE")
        self.assertEqual(self._resolve("nicotinamide")["keyword_key"], "NICOTINAMIDE")

    def test_retinol_and_retinoid_stay_separate(self):
        self.assertEqual(self._resolve("retinol")["keyword_key"], "RETINOL")
        self.assertEqual(self._resolve("retinoids")["keyword_key"], "RETINOID")

    def test_field_labels_are_dropped(self):
        self.assertIsNone(self._resolve("Chemistry"))
        self.assertIsNone(self._resolve("materials science"))

    def test_unmatched_terms_pass_through_rather_than_disappear(self):
        resolved = self._resolve("some novel peptide complex")
        self.assertEqual(resolved["keyword_key"], "some novel peptide complex")
        self.assertFalse(resolved["is_in_lexicon"])

    def test_lexicon_wins_over_stopwords(self):
        # dermatology 는 분야명이지만 일부러 남긴 신호다
        self.assertIsNotNone(self._resolve("dermatology"))

    def test_a_paper_mentioning_two_aliases_counts_the_keyword_once(self):
        # 접지 않으면 paper_count 가 부풀어 prevalence 가 조용히 틀린다
        resolved = normalize.resolve_field(
            ["ZnO", "zinc oxide", "nano-ZnO"],
            self.alias_map,
            self.entries,
            self.stopwords,
        )
        self.assertEqual([item["keyword_key"] for item in resolved], ["ZINC_OXIDE"])

    def test_lexicon_has_no_duplicate_aliases(self):
        # load_lexicon 이 중복 별칭에 예외를 던진다. 여기까지 왔으면 통과다.
        self.assertGreater(len(self.alias_map), len(self.entries))

    def test_every_entry_has_the_korean_bridge_field(self):
        for key, entry in self.entries.items():
            self.assertIn("kr_colloquial", entry, key)
            self.assertIsInstance(entry["kr_colloquial"], list, key)


if __name__ == "__main__":
    unittest.main()
