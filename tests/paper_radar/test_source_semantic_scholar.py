"""paper_radar.sources.semantic_scholar: tldr 요약. tldr 이 객체라는 점이 핵심.

PAYLOAD 픽스처는 papers/tests/test_semantic_scholar.py 의 2026-08-19 실제
응답 형태를 그대로 옮긴 것이다(T5a 에서 papers/tests 로부터 이식). abstract
가 null 인데 tldr 은 있는 실제 사례다.

목킹 지점: 기존 테스트는 source.http.get_json 을 patch 했지만, 여기서는
FakeSession 을 실제 Transport 에 주입해 request 루프를 그대로 통과시킨다.
"""

import json
import os
import unittest
from unittest import mock

from paper_radar.registry import SOURCES
from paper_radar.sources import semantic_scholar as s2
from paper_radar.transport.errors import NotFound
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

PAYLOAD = {
    "paperId": "abc123",
    "title": "Integrating habits and practices data for soaps and cosmetics",
    "abstract": None,
    "tldr": {"model": "tldr@v2.0.0", "text": "The Phase 2 Creme RIFM model is described."},
    "citationCount": 1341,
    "year": 2017,
    "venue": "Regulatory toxicology and pharmacology : RTP",
    "isOpenAccess": False,
}

RECORD_KEYS = {
    "title",
    "abstract",
    "tldr",
    "citation_count",
    "journal",
    "year",
    "is_open_access",
}


class FetchTest(unittest.TestCase):
    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def _fetch(self, payload, **kwargs):
        transport, session = self._transport([FakeResponse(200, body=json.dumps(payload).encode())])
        result = s2.fetch(transport, **kwargs)
        return result, session

    def test_returns_none_without_a_doi(self):
        transport, session = self._transport([])
        self.assertIsNone(s2.fetch(transport, doi=None, title="Some title"))
        self.assertEqual(session.calls, [])

    def test_unwraps_the_tldr_text_out_of_its_object(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["tldr"], "The Phase 2 Creme RIFM model is described.")

    def test_reports_a_null_abstract_even_when_a_tldr_exists(self):
        # 초록과 tldr 은 서로 독립이다. 하나가 있어도 다른 하나가 없을 수 있다.
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertIsNone(result["abstract"])

    def test_maps_venue_to_journal(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["journal"], "Regulatory toxicology and pharmacology : RTP")

    def test_carries_the_citation_count(self):
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(result["citation_count"], 1341)

    def test_prefixes_the_doi_for_the_lookup_path(self):
        _, session = self._fetch(PAYLOAD, doi="10.1016/j.yrtph.2017.05.017")
        self.assertIn("DOI:10.1016/j.yrtph.2017.05.017", session.calls[0]["url"])

    def test_propagates_not_found_instead_of_absorbing_it(self):
        """구 papers/http.py 는 404 를 None 으로 흡수했지만, 새 계약에서는 transport
        의 NotFound 를 여기서 잡지 않고 그대로 전파해야 한다."""
        transport, session = self._transport([FakeResponse(404)])
        with self.assertRaises(NotFound):
            s2.fetch(transport, doi="10.9999/absent")
        self.assertEqual(len(session.calls), 1)

    def test_returns_none_for_an_empty_payload(self):
        result, _ = self._fetch({}, doi="10.1/a")
        self.assertIsNone(result)

    def test_survives_a_tldr_that_is_a_plain_string(self):
        result, _ = self._fetch(dict(PAYLOAD, tldr="already a string"), doi="10.1/a")
        self.assertEqual(result["tldr"], "already a string")

    def test_survives_a_missing_tldr(self):
        result, _ = self._fetch(dict(PAYLOAD, tldr=None), doi="10.1/a")
        self.assertIsNone(result["tldr"])

    def test_treats_an_empty_venue_as_absent(self):
        result, _ = self._fetch(dict(PAYLOAD, venue=""), doi="10.1/a")
        self.assertIsNone(result["journal"])

    def test_record_has_the_same_key_set_as_the_legacy_module(self):
        """T5b 의 동등성 전제 — 반환 dict 키가 papers/sources/semantic_scholar.py 와 같아야 한다."""
        result, _ = self._fetch(PAYLOAD, doi="10.1/a")
        self.assertEqual(set(result), RECORD_KEYS)


class CurrentPolicyTest(unittest.TestCase):
    def test_uses_the_conservative_interval_without_an_api_key(self):
        with mock.patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": ""}, clear=False):
            self.assertEqual(s2.current_policy().min_interval_s, 4.0)

    def test_uses_the_keyed_interval_when_an_api_key_is_set(self):
        with mock.patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": "secret"}, clear=False):
            self.assertEqual(s2.current_policy().min_interval_s, 1.0)

    def test_keeps_the_auth_declaration_in_both_cases(self):
        with mock.patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": "secret"}, clear=False):
            policy = s2.current_policy()
        self.assertEqual(policy.auth_kind, "header")
        self.assertEqual(policy.auth_name, "x-api-key")
        self.assertEqual(policy.auth_env, "SEMANTIC_SCHOLAR_API_KEY")


class RegistryTest(unittest.TestCase):
    def test_registers_semantic_scholar_under_its_key(self):
        self.assertIs(SOURCES["semantic_scholar"], s2.SemanticScholar)


if __name__ == "__main__":
    unittest.main()
