"""Europe PMC — 생명과학 도메인 보강.

papers/sources/europepmc.py 를 paper_radar contract 위로 이식한 것이다(T5a).

여기서 찾히면 그 논문은 생명과학 문헌으로 색인된 것이다. 화장품 연구는
피부과·독성학과 겹치므로 이 소스의 적중률이 높고, OpenAlex 가 초록을 주지
않은 28%를 메우는 주된 경로가 된다.

resultType=core 를 빼면 초록이 오지 않는다. keywordList.keyword 는 리스트다.

세 보강 소스 중 유일하게 DOI 없이도 조회한다. DOI 가 없는 논문이 점수를
받을 수 있는 단 하나의 통로다.
"""

from __future__ import annotations

from typing import ClassVar

from paper_radar.contract import SourcePolicy
from paper_radar.registry import register

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


@register
class EuropePmc:
    """레지스트리 등록용 얇은 표지 — 정책 선언 외에 상태를 갖지 않는다. 인증
    없음(공개 API)."""

    key: ClassVar[str] = "europepmc"
    policy: ClassVar[SourcePolicy] = SourcePolicy(host="www.ebi.ac.uk", min_interval_s=0.2)


def _query(doi, title):
    if doi:
        return f'DOI:"{doi}"'
    if title:
        # 제목 안의 따옴표가 쿼리 문법을 깨뜨린다
        cleaned = " ".join(title.replace('"', " ").split())
        if cleaned:
            return f'TITLE:"{cleaned}"'
    return None


def _keywords(hit):
    raw = (hit.get("keywordList") or {}).get("keyword") or []
    if not isinstance(raw, list):
        return []
    return [word for word in raw if isinstance(word, str)]


def fetch(transport, doi=None, title=None):
    """DOI 를 먼저 쓰고 없으면 제목으로 검색한다.

    둘 다 없으면 None(조회 자체가 불가능하다는 뜻). 검색 결과가 0건이어도
    None(이 경우는 정상 응답 안에서의 빈 결과이지 전송 실패가 아니다) —
    전송 실패는 transport 의 타입 있는 예외로 그대로 전파된다.
    """
    query = _query(doi, title)
    if not query:
        return None
    payload = transport.get_json(
        BASE,
        params=[
            ("query", query),
            ("format", "json"),
            ("resultType", "core"),
            ("pageSize", "1"),
        ],
        policy=EuropePmc.policy,
    )
    hits = ((payload or {}).get("resultList") or {}).get("result") or []
    if not hits or not isinstance(hits[0], dict):
        return None
    hit = hits[0]
    journal = ((hit.get("journalInfo") or {}).get("journal") or {}).get("title")
    return {
        "title": hit.get("title") or None,
        "abstract": hit.get("abstractText") or None,
        "journal": journal or None,
        "keywords": _keywords(hit),
        "europepmc_id": hit.get("id") or None,
        "is_life_science": True,
    }
