"""paper_radar.sources.pubchem: 이름 -> CID -> 동의어 2단계 조회, CAS 추출, name_key 정규화.

PAYLOAD_CIDS/PAYLOAD_SYNONYMS 픽스처는 태스크 브리핑(task-12-brief.md)의 API
사실(2026-08 조사, 실측 CID 936 = niacinamide) 절에 실린 응답 형태를 그대로
옮긴 것이다 — 실제 응답 형태 검증은 이 픽스처가 아니라 tool/live_smoke.py 가
한다.

목킹 지점: 다른 소스 테스트와 동일하게 FakeSession 을 실제 Transport 에
주입해 request 루프를 그대로 통과시킨다.
"""

import json
import unittest

from paper_radar.models import IngredientRecord
from paper_radar.registry import SOURCES
from paper_radar.sources import pubchem
from paper_radar.transport.errors import NotFound
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

PAYLOAD_CIDS = {"IdentifierList": {"CID": [936]}}

# 브리핑 API 사실 절의 구성 예시 — 실제로는 니아신아마이드 동의어가 수백
# 개지만, 여기서는 SYNONYM_LIMIT(50) 절단 테스트에 필요한 만큼만 둔다.
PAYLOAD_SYNONYMS = {
    "InformationList": {
        "Information": [
            {
                "CID": 936,
                "Synonym": [
                    "niacinamide",
                    "nicotinamide",
                    "98-92-0",
                    "vitamin B3",
                    "Nicotinic acid amide",
                ],
            }
        ]
    }
}


def _response(payload, status=200):
    return FakeResponse(status, body=json.dumps(payload).encode())


class ExtractCasTest(unittest.TestCase):
    def test_finds_a_cas_number_shaped_string(self):
        self.assertEqual(pubchem.extract_cas(["niacinamide", "98-92-0"]), "98-92-0")

    def test_returns_none_when_no_synonym_looks_like_a_cas_number(self):
        self.assertIsNone(pubchem.extract_cas(["niacinamide", "vitamin B3"]))

    def test_picks_the_first_match_when_multiple_candidates_are_present(self):
        self.assertEqual(
            pubchem.extract_cas(["59-67-6", "niacinamide", "98-92-0"]), "59-67-6"
        )

    def test_returns_none_for_an_empty_synonym_list(self):
        self.assertIsNone(pubchem.extract_cas([]))

    def test_does_not_match_a_string_with_the_wrong_shape(self):
        # 자릿수가 규칙(2~7자리-2자리-1자리)에서 벗어나면 매치하지 않는다.
        self.assertIsNone(pubchem.extract_cas(["1-2-345", "98-92-0x"]))


class NameKeyTest(unittest.TestCase):
    def test_lowercases_via_casefold(self):
        self.assertEqual(pubchem.name_key("Niacinamide"), "niacinamide")

    def test_strips_leading_and_trailing_whitespace(self):
        self.assertEqual(pubchem.name_key("  niacinamide  "), "niacinamide")

    def test_collapses_internal_whitespace_runs(self):
        self.assertEqual(pubchem.name_key("niacin   amide"), "niacin amide")

    def test_is_stable_for_already_normalized_input(self):
        self.assertEqual(pubchem.name_key("niacinamide"), "niacinamide")

    def test_two_differently_cased_and_spaced_names_produce_the_same_key(self):
        self.assertEqual(pubchem.name_key("  Niacinamide "), pubchem.name_key("niacinamide"))


class ResolveTest(unittest.TestCase):
    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def test_returns_none_for_an_empty_name(self):
        transport, session = self._transport([])
        self.assertIsNone(pubchem.resolve(transport, ""))
        self.assertIsNone(pubchem.resolve(transport, "   "))
        self.assertEqual(session.calls, [])

    def test_resolves_a_known_name_across_two_requests(self):
        transport, session = self._transport(
            [_response(PAYLOAD_CIDS), _response(PAYLOAD_SYNONYMS)]
        )
        record = pubchem.resolve(transport, "niacinamide")

        self.assertIsInstance(record, IngredientRecord)
        self.assertEqual(record.name_key, "niacinamide")
        self.assertIsNone(record.inci_name)
        self.assertEqual(record.cid, 936)
        self.assertEqual(record.cas, "98-92-0")
        self.assertEqual(
            record.synonyms,
            ("niacinamide", "nicotinamide", "98-92-0", "vitamin B3", "Nicotinic acid amide"),
        )
        self.assertEqual(record.sources, ("pubchem",))
        self.assertEqual(len(session.calls), 2)

    def test_first_request_hits_the_name_to_cid_endpoint(self):
        transport, session = self._transport(
            [_response(PAYLOAD_CIDS), _response(PAYLOAD_SYNONYMS)]
        )
        pubchem.resolve(transport, "niacinamide")
        self.assertIn("compound/name/niacinamide/cids/JSON", session.calls[0]["url"])

    def test_second_request_hits_the_cid_to_synonyms_endpoint(self):
        transport, session = self._transport(
            [_response(PAYLOAD_CIDS), _response(PAYLOAD_SYNONYMS)]
        )
        pubchem.resolve(transport, "niacinamide")
        self.assertIn("compound/cid/936/synonyms/JSON", session.calls[1]["url"])

    def test_propagates_not_found_instead_of_absorbing_it(self):
        """모르는 이름 = 404(브리핑 API 사실 절) — 여기서 흡수하지 않고
        그대로 전파해야 한다(호출자가 부재 처리)."""
        transport, session = self._transport([FakeResponse(404)])
        with self.assertRaises(NotFound):
            pubchem.resolve(transport, "not-a-real-ingredient")
        self.assertEqual(len(session.calls), 1, "CID 조회가 404 면 동의어 요청은 없어야 한다")

    def test_truncates_synonyms_to_the_first_fifty(self):
        many_synonyms = [f"synonym-{i}" for i in range(120)]
        payload = {
            "InformationList": {"Information": [{"CID": 936, "Synonym": many_synonyms}]}
        }
        transport, _ = self._transport([_response(PAYLOAD_CIDS), _response(payload)])
        record = pubchem.resolve(transport, "niacinamide")
        self.assertEqual(len(record.synonyms), pubchem.SYNONYM_LIMIT)
        self.assertEqual(record.synonyms[0], "synonym-0")
        self.assertEqual(record.synonyms[-1], f"synonym-{pubchem.SYNONYM_LIMIT - 1}")

    def test_extracts_cas_from_beyond_the_truncation_point(self):
        """CAS 는 절단 "전" 원본 전체에서 찾는다 — 51번째 이후에 있어도
        놓치지 않아야 한다(sources/pubchem.py resolve() docstring 참고)."""
        many_synonyms = [f"synonym-{i}" for i in range(60)]
        many_synonyms.append("98-92-0")  # 61번째(절단선 너머)
        payload = {
            "InformationList": {"Information": [{"CID": 936, "Synonym": many_synonyms}]}
        }
        transport, _ = self._transport([_response(PAYLOAD_CIDS), _response(payload)])
        record = pubchem.resolve(transport, "niacinamide")
        self.assertEqual(record.cas, "98-92-0")
        self.assertNotIn("98-92-0", record.synonyms)

    def test_uses_the_first_cid_when_multiple_are_returned(self):
        payload = {"IdentifierList": {"CID": [936, 999]}}
        transport, session = self._transport([_response(payload), _response(PAYLOAD_SYNONYMS)])
        record = pubchem.resolve(transport, "niacinamide")
        self.assertEqual(record.cid, 936)
        self.assertIn("compound/cid/936/synonyms/JSON", session.calls[1]["url"])

    def test_returns_none_when_the_cid_list_is_empty(self):
        payload = {"IdentifierList": {"CID": []}}
        transport, session = self._transport([_response(payload)])
        self.assertIsNone(pubchem.resolve(transport, "empty-cid-list"))
        self.assertEqual(len(session.calls), 1)

    def test_uses_the_captured_at_from_the_synonyms_response(self):
        """fetched_at 은 이 소스가 직접 만들지 않는다 — transport 가
        Payload.captured_at 에 스탬프한 값을 그대로 옮긴다(unpaywall.fetch()
        와 같은 계약)."""
        transport, _ = self._transport([_response(PAYLOAD_CIDS), _response(PAYLOAD_SYNONYMS)])
        record = pubchem.resolve(transport, "niacinamide")
        self.assertRegex(record.fetched_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


class RegistryTest(unittest.TestCase):
    def test_registers_pubchem_under_its_key(self):
        self.assertIs(SOURCES["pubchem"], pubchem.PubChem)


if __name__ == "__main__":
    unittest.main()
