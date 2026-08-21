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


def _update_date(updated):
    """"updated": {"date-parts": [[Y, (M), (D)]]} 를 "YYYY[-MM[-DD]]" 로.

    실측에서 월/일이 없는(연도만 있는) date-parts 가 드물지 않다 — 있는
    만큼만 이어 붙인다. 파츠가 아예 없으면 None(날짜 미상).
    """
    parts = ((updated or {}).get("date-parts") or [None])[0]
    if not isinstance(parts, list) or not parts:
        return None
    numbers = [p for p in parts if isinstance(p, int)]
    if not numbers:
        return None
    widths = (4, 2, 2)  # 연-월-일 자리수(월/일은 zero-pad)
    return "-".join(f"{value:0{width}d}" for value, width in zip(numbers, widths, strict=False))


def parse_retractions(message: dict, doi: str) -> tuple[dict, ...]:
    """message 에서 철회 신호를 두 경로로 뽑는다. 순수 함수(네트워크·DB 없음).

    경로 1 (relation, role="retracted"): 철회'된' 논문 자신의 응답에 실린다 —
        relation["is-retracted-by"][].id 가 철회 공지의 DOI다. 우리 파이프라인이
        조회하는 DOI 는 거의 항상 이쪽(원 논문)이라 이게 주 경로다.
    경로 2 (update-to, role="notice"): 철회 공지 자신의 응답에 실린다 —
        update-to[].DOI 가 원 논문의 DOI 다. OpenAlex 가 공지를 독립 문서로
        색인해 우리 파이프라인이 공지 DOI 자체를 수집하는 경우가 드물지
        않아, 파싱만 하고 끝내지 않고 role 을 함께 태그한다(아래 참고).
        type 에 "retraction" 이 포함된 항목만 쓴다 — correction 등 다른
        갱신 종류는 이번 태스크(철회 추적)의 범위 밖이라 무시한다(정정 논문
        추적은 별도 태스크의 몫).

    role 태그 (리뷰 Important 대응): "이 message 가 기술하는 문서 자신이
        철회된 논문인가, 철회 공지인가"를 항목마다 구분해 실어 보낸다 —
        두 경로가 "retraction_doi" 자리에 넣는 DOI 의 실제 의미가 정반대이기
        때문이다(경로 1 은 상대편이 공지, 경로 2 는 상대편이 원 논문). role
        이 없으면 pipeline 이 두 경로를 구분 없이 doi=record 자신의 doi 로
        조립해버려, 공지 문서 자신을 수집한 경우 retraction 테이블 행의
        doi/retraction_doi 가 스키마 주석과 반대로 뒤집힌다(고쳐진 버그).
        verify.build() 의 is_retracted 판정도 이 role 을 봐야 한다 — 공지
        문서 자체는 철회'된' 논문이 아니라 철회를 알리는 문서이므로
        role="notice" 만 있는 레코드의 점수를 0 으로 만들면 안 된다.

    doi(이 message 의 주인 DOI, 즉 fetch() 호출에 쓰인 DOI)와 우연히 같은
    DOI 를 가리키는 항목은 자기 자신을 자기 철회 상대로 지목하는 모순이라
    방어적으로 걸러낸다(API 오응답 방어). DOI 필드 자체가 없거나 빈 항목도
    같은 이유로 건너뛴다(두 경로를 대칭으로 다룬다 — repository 의
    None -> '' 강제에 기대지 않고 여기서 이미 무의미한 항목을 버린다).
    반환 dict 자체는 "doi" 를 담지 않는다(그 값은 pipeline 이 role 에 따라
    record["doi"] 또는 상대편 DOI 로 채운다).
    """
    if not isinstance(message, dict):
        return ()
    own = (doi or "").strip().lower()
    results: list[dict] = []

    relation = message.get("relation")
    if isinstance(relation, dict):
        for item in relation.get("is-retracted-by") or []:
            if not isinstance(item, dict):
                continue
            candidate = item.get("id")
            if not candidate or candidate.strip().lower() == own:
                continue
            results.append(
                {
                    "role": "retracted",
                    "retraction_doi": candidate,
                    "update_type": "retraction",
                    "update_date": None,
                }
            )

    for item in message.get("update-to") or []:
        if not isinstance(item, dict):
            continue
        update_type = item.get("type") or ""
        if "retraction" not in update_type:
            continue
        candidate = item.get("DOI")
        if not candidate or candidate.strip().lower() == own:
            continue
        results.append(
            {
                "role": "notice",
                "retraction_doi": candidate,
                "update_type": update_type,
                "update_date": _update_date(item.get("updated")),
            }
        )

    return tuple(results)


def fetch(transport, doi=None, title=None):
    """DOI 로만 조회한다. DOI 가 없으면 None(조회 자체가 불가능하다는 뜻).

    404(NotFound)는 여기서 잡지 않고 그대로 전파한다 — 그 판단은 파이프라인
    몫이다. title 은 이 소스에서는 쓰이지 않는다(브리핑 시그니처와의 호환용).

    retractions(T9): parse_retractions() 로 뽑은 철회 신호 튜플. 비어 있는
    것(기본값)이 대다수다 — 철회는 극소수 논문에만 해당한다.
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
        "retractions": parse_retractions(message, doi),
    }
