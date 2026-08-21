"""Unpaywall — DOI 로 합법 오픈 액세스(OA) 위치를 조회하는 소스(Phase 2 첫 신규 소스).

"PDF 등 대용량은 하이퍼링크로 대체" 요구의 정답인 소스다. DOI 를 넣으면 합법
OA 위치(있다면 PDF 직링크 포함)를 돌려준다. **이 모듈은 PDF 파일을 절대
다운로드하지 않는다 — 링크(URL 문자열)만 파싱해 돌려준다.**

API 사실 (2026-08 조사 — 아래 근거로 이 모듈을 작성했다. 응답 형태의 실측
검증은 코드가 아니라 tool/live_smoke.py 항목으로 한다):
    - 엔드포인트: GET https://api.unpaywall.org/v2/{doi}?email={email}
      email 파라미터가 필수다. SourcePolicy 의 auth_kind="param" 선언만으로
      Transport 가 OPENALEX_EMAIL 값을 자동으로 email 파라미터에 채운다
      (crossref 의 mailto 와 같은 auth 훅 재사용).
    - 한도: 100,000건/일, 인증 키 불요. 예산 헤더(BUDGET_STATUS 402/409 를
      유발하는 x-ratelimit 류)가 없다 — 있다 해도 transport.budget 이
      알아서 관측만 하고 지나간다.
    - 404 = 그 DOI 를 모른다는 뜻(정상적인 결과의 하나). "이 논문은 OA 가
      아니다"(is_oa: false)와는 다른 신호다 — 후자는 200 으로 온다.
    - is_oa: false 면 best_oa_location 이 null 이다. 이 경우 pdf_url/
      landing_url/host_type/license 는 전부 None 으로 둔다. oa_status 는
      응답값("closed" 등)을 그대로 보존한다 — 부재를 "closed"로 뭉개지 않는다.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import quote

from paper_radar.contract import Fetch, SourcePolicy
from paper_radar.models import OaLocationRecord
from paper_radar.registry import register

BASE = "https://api.unpaywall.org/v2/"


@register
class Unpaywall:
    """레지스트리 등록용 얇은 표지 — 정책 선언 외에 상태를 갖지 않는다."""

    key: ClassVar[str] = "unpaywall"
    policy: ClassVar[SourcePolicy] = SourcePolicy(
        host="api.unpaywall.org",
        # 하루 10만 건 한도라 페이스가 병목이 될 일이 거의 없다 — 이 값은
        # 한도를 지키기 위한 하한이 아니라 상대 API 에 대한 예의(과도하게
        # 몰아치지 않는다)로 두는 최소 지연이다.
        min_interval_s=0.1,
        auth_env="OPENALEX_EMAIL",
        auth_kind="param",
        auth_name="email",
    )


def to_record(data: dict, checked_at: str) -> OaLocationRecord:
    """Unpaywall 응답 JSON(순수 dict) -> OaLocationRecord. 네트워크 없음.

    is_oa 가 false 면 best_oa_location 이 null 이므로 pdf_url/landing_url/
    host_type/license 를 전부 None 으로 둔다. oa_status 는 항상 응답값을
    그대로 쓴다("gold"/"green"/"hybrid"/"bronze"/"closed" 등) — 이 함수가
    임의로 "closed"를 기본값으로 밀어 넣지 않는다(응답에 그 키가 없는
    비정상 케이스에 한해서만 방어적으로 "closed"를 쓴다).
    """
    is_oa = bool(data.get("is_oa"))
    best_oa_location = data.get("best_oa_location") if is_oa else None
    best = best_oa_location or {}
    doi = (data.get("doi") or "").strip().lower()
    return OaLocationRecord(
        doi=doi,
        is_oa=is_oa,
        oa_status=data.get("oa_status") or "closed",
        pdf_url=best.get("url_for_pdf"),
        landing_url=best.get("url"),
        host_type=best.get("host_type"),
        license=best.get("license"),
        checked_at=checked_at,
    )


def fetch(transport, doi: str | None) -> OaLocationRecord | None:
    """DOI 로 OA 위치를 조회한다. DOI 가 없으면 None(조회 자체가 불가능하다는 뜻).

    404(NotFound)는 여기서 잡지 않고 그대로 전파한다 — "그 DOI 를 모른다"와
    "이 논문은 OA 가 아니다"(정상 200 + is_oa: false)를 호출자가 구분해야
    하기 때문이다. checked_at 은 transport 가 응답에 스탬프한
    Payload.captured_at(ISO UTC)을 그대로 쓴다 — OA 상태는 시간에 따라
    변하므로 "언제 관측했는가"가 소스가 아니라 transport 의 책임으로
    일관되게 남아야 한다.
    """
    if not doi:
        return None
    # email 파라미터는 여기서 손으로 붙이지 않는다 — SourcePolicy 의
    # auth_kind="param"/auth_name="email" 선언만으로 Transport._inject_auth()
    # 가 OPENALEX_EMAIL 값이 있을 때만 자동으로 채운다(crossref 의 mailto 와
    # 같은 auth 훅 재사용).
    payload = transport.request(Fetch(url=BASE + quote(doi, safe="/")), Unpaywall.policy)
    data = payload.json_data()
    return to_record(data, payload.captured_at)
