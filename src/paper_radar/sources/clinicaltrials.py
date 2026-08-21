"""ClinicalTrials.gov v2 — 임상시험 레코드 소스 (Phase 2, T11).

"논문이 니아신아마이드를 언급한다"와 "그 주장 뒤에 대조시험이 있다"의 차이를
만드는 소스다. papers 파이프라인이 만드는 서지 레코드(dict)와 달리, 여기는
models.TrialRecord(T3 가 이미 예정해 둔 신규 레코드 타입)를 만든다 —
openalex.py 의 순수 파싱 함수(to_record)/네트워크 드라이버(iter_search)
분리를 그대로 따른다.

API 사실 (2026-08 조사 — 실측: sunscreen 386건, niacinamide 1,600건)
    - GET https://clinicaltrials.gov/api/v2/studies
      ?query.term={q}&pageSize={n}&pageToken={t}&countTotal=true
    - 응답: {"totalCount": 386, "studies": [...], "nextPageToken": "..."}
      (마지막 페이지엔 nextPageToken 이 없다)
    - study 구조(관심 경로만, brief 의 구성 예시 — 실제 형태 검증은 코드가
      아니라 tool/live_smoke.py 항목으로 한다):
        protocolSection.identificationModule.{nctId, briefTitle}
        protocolSection.statusModule.{overallStatus, studyFirstPostDateStruct.date}
        protocolSection.sponsorCollaboratorsModule.leadSponsor.{name, class}
        protocolSection.designModule.{phases, enrollmentInfo.count}
        protocolSection.conditionsModule.conditions
        protocolSection.armsInterventionsModule.interventions[].{type, name}
        protocolSection.outcomesModule
        hasResults
    - 한도: 문서화된 상한 없음. 무키. min_interval_s=0.5 는 상한을 지키기
      위한 값이 아니라 순수한 예의(polite pacing)다.

페이지네이션과 부분 결과 보존
    iter_studies() 는 openalex.iter_search() 와 같은 패턴이다 — 페이지를
    받는 즉시 그 페이지의 레코드를 yield 하므로, 호출자가 제너레이터를
    직접 순회하면 중간 페이지에서 예외가 나도 이미 넘어온 레코드는 호출자
    손에 남는다. 예외는 여기서 흡수하지 않고 그대로 전파한다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import ClassVar

from paper_radar.contract import Fetch, SourcePolicy
from paper_radar.models import TrialRecord
from paper_radar.registry import register

BASE = "https://clinicaltrials.gov/api/v2/studies"

# v2 API 는 pageSize 상한을 문서화하지 않았지만 실측상 100 안팎에서 안정적으로
# 응답했다 — 과도하게 큰 pageSize 로 매 요청을 무겁게 만들지 않기 위한 상한.
PAGE_SIZE_MAX = 100


@register
class ClinicalTrials:
    """레지스트리 등록용 얇은 표지 — 정책 선언 외에 상태를 갖지 않는다."""

    key: ClassVar[str] = "clinicaltrials"
    policy: ClassVar[SourcePolicy] = SourcePolicy(
        host="clinicaltrials.gov",
        min_interval_s=0.5,  # 문서화된 한도 없음 — 예의상 최소 간격일 뿐
    )


def _names(interventions) -> tuple[str, ...]:
    if not isinstance(interventions, list):
        return ()
    return tuple(
        item.get("name")
        for item in interventions
        if isinstance(item, dict) and item.get("name")
    )


def _conditions(values) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(v for v in values if isinstance(v, str) and v)


def to_record(study: dict, query: str, captured_at: str) -> TrialRecord:
    """study(protocolSection 을 감싼 dict) -> TrialRecord. 네트워크 없음.

    결손 경로는 전부 .get() 사슬로 흡수한다 — v2 응답은 모듈이 하나만
    비어도(예: 관찰 연구는 phases 가 없다) 나머지는 정상으로 온다.
    outcomes_json 은 outcomesModule 전체를 sort_keys=True 로 직렬화한다 —
    같은 입력이 항상 같은 문자열이 되어야 재수집이 diff 를 만들지 않는다.
    """
    protocol = study.get("protocolSection") or {}
    identification = protocol.get("identificationModule") or {}
    status_module = protocol.get("statusModule") or {}
    sponsor_module = protocol.get("sponsorCollaboratorsModule") or {}
    design_module = protocol.get("designModule") or {}
    conditions_module = protocol.get("conditionsModule") or {}
    arms_module = protocol.get("armsInterventionsModule") or {}
    outcomes_module = protocol.get("outcomesModule") or {}

    nct_id = identification.get("nctId") or ""
    phases = design_module.get("phases") or []
    phase = phases[0] if phases else None
    lead_sponsor = sponsor_module.get("leadSponsor") or {}
    enrollment_info = design_module.get("enrollmentInfo") or {}
    first_posted = (status_module.get("studyFirstPostDateStruct") or {}).get("date")

    return TrialRecord(
        nct_id=nct_id,
        title=identification.get("briefTitle") or "",
        status=status_module.get("overallStatus") or "",
        phase=phase,
        sponsor_class=lead_sponsor.get("class"),
        enrollment=enrollment_info.get("count"),
        conditions=_conditions(conditions_module.get("conditions")),
        interventions=_names(arms_module.get("interventions")),
        outcomes_json=json.dumps(outcomes_module, ensure_ascii=False, sort_keys=True),
        first_posted=first_posted,
        results_posted=bool(study.get("hasResults")),
        url=f"https://clinicaltrials.gov/study/{nct_id}",
        matched_query=query,
        captured_at=captured_at,
    )


def iter_studies(transport, query: str, limit: int) -> Iterator[TrialRecord]:
    """pageToken 페이지네이션으로 limit 건까지, 페이지를 받는 즉시 그 페이지의
    레코드를 하나씩 yield 한다(openalex.iter_search() 와 같은 패턴).

    중간에 transport 예외(예: 몇 페이지를 이미 받은 뒤 TransientError/
    BudgetExhausted)가 나면 그대로 전파한다 — 다만 그 전에 yield 된 레코드는
    이미 호출자 손에 있으므로 예외와 함께 사라지지 않는다.
    """
    yielded = 0
    page_token: str | None = None
    while yielded < limit:
        params = [
            ("query.term", query),
            ("pageSize", str(min(PAGE_SIZE_MAX, limit - yielded))),
            ("countTotal", "true"),
        ]
        if page_token:
            params.append(("pageToken", page_token))
        payload = transport.request(Fetch(url=BASE, params=tuple(params)), ClinicalTrials.policy)
        data = payload.json_data()
        studies = data.get("studies") or []
        if not studies:
            return
        for study in studies:
            if yielded >= limit:
                return
            yield to_record(study, query, payload.captured_at)
            yielded += 1
        page_token = data.get("nextPageToken")
        if not page_token:
            return
