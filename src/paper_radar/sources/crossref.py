"""Crossref — DOI 검증 담당.

papers/sources/crossref.py 를 paper_radar contract 위로 이식한 것이다(T5a).

이 소스의 역할은 초록 보강이 아니다. '이 DOI 가 실제로 등록된 논문인가'와
'등록된 제목이 OpenAlex 제목과 같은가'에 답하는 것이다. 그 두 답이
confidence_score 의 55점(30 + 25)을 결정한다.

없는 DOI 는 404 를 준다. 기존 papers/http.py 는 이를 None 으로 흡수했지만,
여기서는 transport 가 던지는 NotFound 를 그대로 전파한다 — "DOI 없음(입력
자체가 불가)"과 "DOI 는 있지만 조회가 실패함(전송 오류)"을 호출자가 구분할
수 있어야 하기 때문이다. fetch() 안에서 이 예외를 잡아 삼키지 않는다.

응답의 title 과 container-title 은 문자열이 아니라 리스트다.

polite pool(mailto)
    2025-12 polite 풀은 mailto 파라미터로 식별한다. 소스 코드가 직접 붙이지
    않고 SourcePolicy 의 auth_kind="param"/auth_name="mailto" 선언만으로
    Transport 가 OPENALEX_EMAIL 값이 있을 때만 자동 주입한다 — OpenAlex 의
    api_key 주입과 같은 auth 훅을 재사용한 것이다.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import quote

from paper_radar.contract import SourcePolicy
from paper_radar.registry import register

BASE = "https://api.crossref.org/works/"


@register
class Crossref:
    """레지스트리 등록용 얇은 표지 — 정책 선언 외에 상태를 갖지 않는다."""

    key: ClassVar[str] = "crossref"
    policy: ClassVar[SourcePolicy] = SourcePolicy(
        host="api.crossref.org",
        min_interval_s=0.2,  # 2025-12 polite 풀(단건 DOI 10req/s) 상한의 절반
        auth_env="OPENALEX_EMAIL",
        auth_kind="param",
        auth_name="mailto",  # polite 풀 식별자. OpenAlex 의 auth 훅을 재사용
    )


def _first(value):
    """Crossref 의 리스트 필드에서 첫 값을 꺼낸다."""
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


def _year(issued):
    parts = (issued or {}).get("date-parts") or []
    if not parts or not isinstance(parts[0], list) or not parts[0]:
        return None
    year = parts[0][0]
    return year if isinstance(year, int) else None


def fetch(transport, doi=None, title=None):
    """DOI 로만 조회한다. DOI 가 없으면 None(조회 자체가 불가능하다는 뜻).

    404(NotFound)는 여기서 잡지 않고 그대로 전파한다 — 그 판단은 파이프라인
    몫이다. title 은 이 소스에서는 쓰이지 않는다(브리핑 시그니처와의 호환용).
    """
    if not doi:
        return None
    payload = transport.get_json(BASE + quote(doi, safe="/"), policy=Crossref.policy)
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return None
    return {
        "title": _first(message.get("title")),
        "journal": _first(message.get("container-title")),
        "publisher": message.get("publisher") or None,
        "type": message.get("type") or None,
        "year": _year(message.get("issued")),
    }
