"""pipeline: 소스 조립. 한 소스가 죽어도 나머지가 계속 간다는 것이 핵심."""

import os
import tempfile
import unittest
from unittest import mock

from papers import http, pipeline, store


def quiet():
    """의도된 경고를 삼킨다. 경고 문구 자체는 test_http 에서 검증한다."""
    return mock.patch.object(http, "warn")

OPENALEX_RECORD = {
    "doi": "10.1/a",
    "openalex_id": "https://openalex.org/W1",
    "title": "Retinol and the skin barrier",
    "authors": ["Kim"],
    "year": 2024,
    "journal": None,
    "abstract": None,
    "tldr": None,
    "keywords": [],
    "topics": ["Dermatology"],
    "citation_count": 12,
    "is_open_access": True,
    "url": "https://doi.org/10.1/a",
    "is_retracted": False,
}

S2 = {
    "title": "Retinol and the skin barrier",
    "abstract": "Retinol improves the barrier.",
    "tldr": "Retinol helps.",
    "citation_count": 9,
    "journal": "J Cosmet Sci",
    "year": 2024,
    "is_open_access": True,
}

EPMC = {
    "title": "Retinol and the skin barrier",
    "abstract": "From Europe PMC.",
    "journal": "Journal of Cosmetic Science",
    "keywords": ["retinol", "skin barrier"],
    "europepmc_id": "123",
    "is_life_science": True,
}

CROSSREF = {
    "title": "Retinol and the skin barrier",
    "journal": "Journal of Cosmetic Science",
    "publisher": "Elsevier BV",
    "type": "journal-article",
    "year": 2024,
}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = store.connect(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _enrich(self, record=None, s2=S2, epmc=EPMC, crossref=CROSSREF):
        patches = {
            "semantic_scholar": s2,
            "europepmc": epmc,
            "crossref": crossref,
        }
        managers = []
        for name, value in patches.items():
            module = getattr(pipeline, name)
            side_effect = value if isinstance(value, Exception) else None
            manager = mock.patch.object(
                module, "fetch",
                side_effect=side_effect,
                return_value=None if side_effect else value,
            )
            managers.append(manager)
            manager.start()
            self.addCleanup(manager.stop)
        return pipeline.enrich(self.conn, dict(record or OPENALEX_RECORD))

    def test_fills_the_tldr_from_semantic_scholar(self):
        result = self._enrich()
        self.assertEqual(result["tldr"], "Retinol helps.")

    def test_fills_a_missing_abstract_from_semantic_scholar(self):
        result = self._enrich()
        self.assertEqual(result["abstract"], "Retinol improves the barrier.")

    def test_keeps_the_openalex_abstract_when_it_already_has_one(self):
        record = dict(OPENALEX_RECORD, abstract="From OpenAlex.")
        result = self._enrich(record=record)
        self.assertEqual(result["abstract"], "From OpenAlex.")

    def test_falls_back_to_europepmc_for_the_abstract(self):
        result = self._enrich(s2=None)
        self.assertEqual(result["abstract"], "From Europe PMC.")

    def test_prefers_the_openalex_citation_count(self):
        # 실측에서 두 소스의 인용수가 달랐다 (1805 vs 1341). OpenAlex 를 믿는다.
        result = self._enrich()
        self.assertEqual(result["citation_count"], 12)

    def test_fills_a_missing_citation_count_from_semantic_scholar(self):
        record = dict(OPENALEX_RECORD, citation_count=None)
        result = self._enrich(record=record)
        self.assertEqual(result["citation_count"], 9)

    def test_fills_a_missing_journal_from_the_first_source_that_has_one(self):
        result = self._enrich()
        self.assertEqual(result["journal"], "J Cosmet Sci")

    def test_fills_empty_keywords_from_europepmc(self):
        result = self._enrich()
        self.assertEqual(result["keywords"], ["retinol", "skin barrier"])

    def test_records_every_source_that_found_the_paper(self):
        result = self._enrich()
        self.assertEqual(
            result["verification"]["found_in_sources"],
            ["openalex", "semantic_scholar", "europepmc"],
        )

    def test_omits_a_source_that_did_not_find_the_paper(self):
        result = self._enrich(s2=None)
        self.assertEqual(
            result["verification"]["found_in_sources"], ["openalex", "europepmc"]
        )

    def test_scores_a_fully_enriched_paper_at_one_hundred(self):
        result = self._enrich()
        self.assertEqual(result["verification"]["confidence_score"], 100)

    def test_marks_crossref_as_unverified_when_the_doi_is_unknown(self):
        result = self._enrich(crossref=None)
        self.assertFalse(result["verification"]["crossref_verified"])

    def test_stamps_a_utc_collection_timestamp(self):
        result = self._enrich()
        self.assertTrue(result["collected_at"].endswith("Z"))

    def test_a_raising_source_does_not_stop_the_others(self):
        with quiet():
            result = self._enrich(s2=RuntimeError("소스가 터졌다"))
        self.assertEqual(result["abstract"], "From Europe PMC.")
        self.assertIsNone(result["tldr"])
        self.assertNotIn("semantic_scholar", result["verification"]["found_in_sources"])

    def test_skips_doi_only_sources_for_a_doi_less_paper(self):
        record = dict(OPENALEX_RECORD, doi=None)
        with mock.patch.object(pipeline.crossref, "fetch") as crossref_fetch, \
             mock.patch.object(pipeline.semantic_scholar, "fetch") as s2_fetch, \
             mock.patch.object(pipeline.europepmc, "fetch", return_value=EPMC):
            result = pipeline.enrich(self.conn, record)
        crossref_fetch.assert_not_called()
        s2_fetch.assert_not_called()
        self.assertFalse(result["verification"]["has_doi"])
        self.assertEqual(result["verification"]["confidence_score"], 30)

    def test_caches_enrichment_so_a_rerun_makes_no_further_calls(self):
        self._enrich()
        with mock.patch.object(pipeline.crossref, "fetch") as crossref_fetch, \
             mock.patch.object(pipeline.semantic_scholar, "fetch") as s2_fetch, \
             mock.patch.object(pipeline.europepmc, "fetch") as epmc_fetch:
            result = pipeline.enrich(self.conn, dict(OPENALEX_RECORD))
        crossref_fetch.assert_not_called()
        s2_fetch.assert_not_called()
        epmc_fetch.assert_not_called()
        self.assertEqual(result["tldr"], "Retinol helps.")


class CollectTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.conn = store.connect(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _collect(self, works, **kwargs):
        with mock.patch.object(pipeline.openalex, "search", return_value=works), \
             mock.patch.object(pipeline.semantic_scholar, "fetch", return_value=S2), \
             mock.patch.object(pipeline.europepmc, "fetch", return_value=EPMC), \
             mock.patch.object(pipeline.crossref, "fetch", return_value=CROSSREF):
            return pipeline.collect(self.conn, "cosmetic", 2016, 2026, 10, **kwargs)

    def test_stores_every_collected_paper(self):
        works = [dict(OPENALEX_RECORD, doi=f"10.1/{i}") for i in range(3)]
        records = self._collect(works)
        self.assertEqual(len(records), 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 3)

    def test_returns_nothing_when_the_search_finds_nothing(self):
        self.assertEqual(self._collect([]), [])

    def test_a_second_run_updates_rather_than_duplicates(self):
        works = [dict(OPENALEX_RECORD)]
        self._collect(works)
        self._collect(works)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)

    def test_one_broken_paper_does_not_abort_the_batch(self):
        works = [
            dict(OPENALEX_RECORD, doi="10.1/ok1"),
            {"openalex_id": None, "doi": None, "title": None},
            dict(OPENALEX_RECORD, doi="10.1/ok2"),
        ]
        with quiet():
            records = self._collect(works)
        stored = self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        self.assertEqual(stored, 2, "식별자 없는 레코드는 건너뛰고 나머지는 저장한다")
        self.assertEqual(len(records), 2)

    def test_writes_the_json_backup_when_asked(self):
        target = self.path + ".json"
        self.addCleanup(lambda: os.path.exists(target) and os.unlink(target))
        self._collect([dict(OPENALEX_RECORD)], json_path=target)
        self.assertTrue(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
