"""paper_radar.sources.clinicaltrials: study -> TrialRecord 변환, pageToken 페이지네이션.

STUDY 픽스처는 T11 브리핑의 API 사실(2026-08 조사, 구성 예시)에 나온 경로를
그대로 옮긴 것이다 — 실제 응답 형태 검증은 tool/live_smoke.py 몫이다.

목킹 지점: test_source_openalex.py 와 같은 방식으로 FakeSession 을 실제
Transport 에 주입해 request 루프를 그대로 통과시킨다.
"""

import json
import unittest

from paper_radar.models import TrialRecord
from paper_radar.registry import SOURCES
from paper_radar.sources import clinicaltrials
from paper_radar.transport.errors import BudgetExhausted
from paper_radar.transport.http import Transport
from tests.paper_radar.test_transport import FakeResponse, FakeSession, make_clock_and_sleep

STUDY = {
    "protocolSection": {
        "identificationModule": {"nctId": "NCT01234567", "briefTitle": "SPF50 sunscreen trial"},
        "statusModule": {
            "overallStatus": "COMPLETED",
            "studyFirstPostDateStruct": {"date": "2023-05-01"},
        },
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Acme Corp", "class": "INDUSTRY"}},
        "designModule": {"phases": ["PHASE3"], "enrollmentInfo": {"count": 120}},
        "conditionsModule": {"conditions": ["Sunburn"]},
        "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "SPF50 sunscreen"}]},
        "outcomesModule": {"primaryOutcomes": [{"measure": "Erythema score"}]},
    },
    "hasResults": True,
}

CAPTURED_AT = "2026-08-21T00:00:00Z"


def _json_response(payload, status=200):
    return FakeResponse(status, body=json.dumps(payload).encode())


class ToRecordTest(unittest.TestCase):
    def setUp(self):
        self.record = clinicaltrials.to_record(STUDY, "sunscreen", CAPTURED_AT)

    def test_returns_a_trial_record(self):
        self.assertIsInstance(self.record, TrialRecord)

    def test_reads_the_nct_id_and_title(self):
        self.assertEqual(self.record.nct_id, "NCT01234567")
        self.assertEqual(self.record.title, "SPF50 sunscreen trial")

    def test_reads_the_overall_status(self):
        self.assertEqual(self.record.status, "COMPLETED")

    def test_reads_the_first_phase(self):
        self.assertEqual(self.record.phase, "PHASE3")

    def test_reads_the_lead_sponsor_class(self):
        self.assertEqual(self.record.sponsor_class, "INDUSTRY")

    def test_reads_the_enrollment_count(self):
        self.assertEqual(self.record.enrollment, 120)

    def test_reads_conditions_and_intervention_names(self):
        self.assertEqual(self.record.conditions, ("Sunburn",))
        self.assertEqual(self.record.interventions, ("SPF50 sunscreen",))

    def test_reads_the_first_posted_date(self):
        self.assertEqual(self.record.first_posted, "2023-05-01")

    def test_reads_results_posted_from_has_results(self):
        self.assertTrue(self.record.results_posted)

    def test_builds_the_canonical_study_url(self):
        self.assertEqual(self.record.url, "https://clinicaltrials.gov/study/NCT01234567")

    def test_carries_the_matched_query_and_captured_at(self):
        self.assertEqual(self.record.matched_query, "sunscreen")
        self.assertEqual(self.record.captured_at, CAPTURED_AT)

    def test_serializes_outcomes_module_as_sorted_json(self):
        expected = json.dumps(
            STUDY["protocolSection"]["outcomesModule"], ensure_ascii=False, sort_keys=True
        )
        self.assertEqual(self.record.outcomes_json, expected)


class ToRecordMissingModulesTest(unittest.TestCase):
    """결손 경로 — 브리핑이 명시한 세 가지(phases 없음/enrollment 없음/hasResults 없음)와
    모듈 전체가 빠진 극단적인 경우."""

    def test_phase_is_none_when_phases_is_absent(self):
        study = {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT1"},
                "designModule": {},
            }
        }
        record = clinicaltrials.to_record(study, "q", CAPTURED_AT)
        self.assertIsNone(record.phase)

    def test_phase_is_none_when_phases_is_an_empty_list(self):
        study = {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT1"},
                "designModule": {"phases": []},
            }
        }
        record = clinicaltrials.to_record(study, "q", CAPTURED_AT)
        self.assertIsNone(record.phase)

    def test_enrollment_is_none_when_enrollment_info_is_absent(self):
        study = {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT1"},
                "designModule": {},
            }
        }
        record = clinicaltrials.to_record(study, "q", CAPTURED_AT)
        self.assertIsNone(record.enrollment)

    def test_results_posted_defaults_to_false_when_has_results_is_absent(self):
        study = {"protocolSection": {"identificationModule": {"nctId": "NCT1"}}}
        record = clinicaltrials.to_record(study, "q", CAPTURED_AT)
        self.assertFalse(record.results_posted)

    def test_survives_a_response_stripped_of_every_optional_module(self):
        record = clinicaltrials.to_record({}, "q", CAPTURED_AT)
        self.assertEqual(record.nct_id, "")
        self.assertEqual(record.title, "")
        self.assertEqual(record.status, "")
        self.assertIsNone(record.phase)
        self.assertIsNone(record.sponsor_class)
        self.assertIsNone(record.enrollment)
        self.assertEqual(record.conditions, ())
        self.assertEqual(record.interventions, ())
        self.assertIsNone(record.first_posted)
        self.assertFalse(record.results_posted)
        self.assertEqual(record.url, "https://clinicaltrials.gov/study/")

    def test_outcomes_json_serialization_is_reproducible_for_the_same_input(self):
        """정렬(sort_keys=True) 덕분에 같은 입력이면 항상 같은 문자열이 되어야
        한다 — dict 키 순서가 달라도 결과 JSON 문자열은 동일해야 한다."""
        study_a = {
            "protocolSection": {
                "outcomesModule": {"primaryOutcomes": [{"measure": "x"}], "otherOutcomes": []}
            }
        }
        study_b = {
            "protocolSection": {
                "outcomesModule": {"otherOutcomes": [], "primaryOutcomes": [{"measure": "x"}]}
            }
        }
        record_a = clinicaltrials.to_record(study_a, "q", CAPTURED_AT)
        record_b = clinicaltrials.to_record(study_b, "q", CAPTURED_AT)
        self.assertEqual(record_a.outcomes_json, record_b.outcomes_json)


class IterStudiesTest(unittest.TestCase):
    """iter_studies() — pageToken 페이지네이션. openalex.iter_search() 와 같은
    "페이지 단위 즉시 yield" 계약을 검증한다."""

    def _transport(self, responses):
        clock, sleep, _ = make_clock_and_sleep()
        session = FakeSession(responses)
        return Transport(session=session, clock=clock, sleep=sleep), session

    def test_follows_the_page_token_until_it_is_exhausted(self):
        pages = [
            _json_response(
                {"totalCount": 3, "studies": [STUDY, STUDY], "nextPageToken": "p2"}
            ),
            _json_response({"totalCount": 3, "studies": [STUDY]}),  # 마지막 페이지엔 토큰 없음
        ]
        transport, session = self._transport(pages)
        records = list(clinicaltrials.iter_studies(transport, "sunscreen", limit=10))
        self.assertEqual(len(records), 3)
        self.assertEqual(session.calls[1]["params"]["pageToken"], "p2")

    def test_never_yields_more_than_the_limit(self):
        page = _json_response(
            {"totalCount": 50, "studies": [STUDY] * 50, "nextPageToken": "p2"}
        )
        transport, _ = self._transport([page])
        records = list(clinicaltrials.iter_studies(transport, "sunscreen", limit=2))
        self.assertEqual(len(records), 2)

    def test_stops_when_a_page_comes_back_with_no_studies(self):
        transport, session = self._transport(
            [_json_response({"totalCount": 0, "studies": []})]
        )
        records = list(clinicaltrials.iter_studies(transport, "sunscreen", limit=10))
        self.assertEqual(records, [])
        self.assertEqual(len(session.calls), 1)

    def test_requests_a_page_size_capped_at_the_limit(self):
        transport, session = self._transport(
            [_json_response({"totalCount": 1, "studies": [STUDY]})]
        )
        list(clinicaltrials.iter_studies(transport, "sunscreen", limit=5))
        self.assertEqual(session.calls[0]["params"]["pageSize"], "5")

    def test_yields_records_page_by_page_without_prefetching_further_pages(self):
        pages = [
            _json_response(
                {"totalCount": 3, "studies": [STUDY, STUDY], "nextPageToken": "p2"}
            ),
            _json_response({"totalCount": 3, "studies": [STUDY]}),
        ]
        transport, session = self._transport(pages)
        records = clinicaltrials.iter_studies(transport, "sunscreen", limit=3)

        next(records)
        self.assertEqual(len(session.calls), 1)
        next(records)
        self.assertEqual(len(session.calls), 1)
        next(records)
        self.assertEqual(len(session.calls), 2)
        with self.assertRaises(StopIteration):
            next(records)

    def test_preserves_already_yielded_records_when_the_second_page_raises(self):
        """2페이지째에서 예외(BudgetExhausted)가 나도 1페이지째에서 이미 yield
        된 레코드는 호출자 손에 남아 있어야 한다 — list() 로 감싸지 않고 for
        문으로 직접 소비해서 확인한다."""
        responses = [
            _json_response(
                {"totalCount": 4, "studies": [STUDY, STUDY], "nextPageToken": "p2"}
            ),
            FakeResponse(402),  # BudgetExhausted — 재시도 없이 즉시 던져진다
        ]
        transport, session = self._transport(responses)
        records = clinicaltrials.iter_studies(transport, "sunscreen", limit=10)

        collected = []
        with self.assertRaises(BudgetExhausted):
            for record in records:
                collected.append(record)

        self.assertEqual(len(collected), 2)
        self.assertEqual(len(session.calls), 2)


class RegistryTest(unittest.TestCase):
    def test_registers_clinicaltrials_under_its_key(self):
        self.assertIs(SOURCES["clinicaltrials"], clinicaltrials.ClinicalTrials)


if __name__ == "__main__":
    unittest.main()
