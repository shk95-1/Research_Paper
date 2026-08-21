"""Semantic Scholar — tldr 한 줄 요약 담당.

papers/sources/semantic_scholar.py 를 paper_radar contract 위로 이식한
것이다(T5a).

tldr 은 문자열이 아니라 {"model": ..., "text": ...} 객체다. text 를 꺼내야 한다.

abstract 와 tldr 은 서로 독립이다. 실측 사례에서 abstract 는 null 인데 tldr 은
있었다. 초록이 없다고 요약도 없다고 단정하면 안 된다.

citationCount 는 OpenAlex cited_by_count 와 다르다 (실측 1341 vs 1805).
어느 쪽이 맞다고 판단하지 않고, 파이프라인이 OpenAlex 값을 우선하고
비어 있을 때만 이 값으로 채운다.

인터벌 — current_policy()
    키가 없으면 익명 풀은 429 가 잦으므로 보수적으로 4.0초 간격을 쓴다. 키가
    있으면 표준 무료 키 상한(1req/s)에 맞춰 1.0초로 좁힌다. ClassVar policy 는
    "키가 있는지 알기 전"의 보수적 기본값(4.0초)만 담고 있다 — 호출자는 항상
    current_policy() 를 통해 그 시점의 실제 env 상태를 반영한 정책을 받아야
    한다. auth(x-api-key 헤더)는 SourcePolicy 선언만으로 Transport 가 자동
    주입하므로, 두 정책 모두 같은 auth_env/auth_kind/auth_name 을 쓴다.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import ClassVar
from urllib.parse import quote

from paper_radar.contract import SourcePolicy
from paper_radar.registry import register

BASE = "https://api.semanticscholar.org/graph/v1/paper/DOI:"
FIELDS = "title,abstract,tldr,citationCount,year,venue,isOpenAccess"

_ANON_INTERVAL_S = 4.0  # 키 없는 익명 풀은 429 가 잦다 — 보수적 기본값
_KEYED_INTERVAL_S = 1.0  # 표준 무료 키는 1req/s 상한


@register
class SemanticScholar:
    """레지스트리 등록용 얇은 표지. policy 는 "키 없음" 가정의 보수적 기본값 —
    실제 호출은 current_policy() 가 돌려주는 정책을 써야 한다."""

    key: ClassVar[str] = "semantic_scholar"
    policy: ClassVar[SourcePolicy] = SourcePolicy(
        host="api.semanticscholar.org",
        min_interval_s=_ANON_INTERVAL_S,
        auth_env="SEMANTIC_SCHOLAR_API_KEY",
        auth_kind="header",
        auth_name="x-api-key",
    )


def current_policy() -> SourcePolicy:
    """env 에 SEMANTIC_SCHOLAR_API_KEY 가 있으면 min_interval_s=1.0, 없으면 4.0.

    replace() 로 min_interval_s 하나만 바꾸는 이유: ClassVar policy 의 다른
    필드(auth_*, timeout_s, max_attempts 등)를 여기서 손으로 다시 나열하면
    나중에 그 필드가 바뀔 때 이 함수만 갱신을 놓쳐 조용히 어긋날 수 있다.
    """
    if os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip():
        return replace(SemanticScholar.policy, min_interval_s=_KEYED_INTERVAL_S)
    return SemanticScholar.policy


def _tldr_text(tldr):
    if isinstance(tldr, dict):
        return tldr.get("text") or None
    return tldr or None


def fetch(transport, doi=None, title=None):
    """DOI 로만 조회한다. DOI 가 없으면 None(조회 자체가 불가능하다는 뜻).

    404(NotFound)는 여기서 잡지 않고 그대로 전파한다. title 은 이 소스에서는
    쓰이지 않는다(브리핑 시그니처와의 호환용).
    """
    if not doi:
        return None
    payload = transport.get_json(
        BASE + quote(doi, safe="/"),
        params=[("fields", FIELDS)],
        policy=current_policy(),
    )
    if not isinstance(payload, dict) or not payload:
        return None
    return {
        "title": payload.get("title") or None,
        "abstract": payload.get("abstract") or None,
        "tldr": _tldr_text(payload.get("tldr")),
        "citation_count": payload.get("citationCount"),
        "journal": payload.get("venue") or None,
        "year": payload.get("year"),
        "is_open_access": payload.get("isOpenAccess"),
    }
