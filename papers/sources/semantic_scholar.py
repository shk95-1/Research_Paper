"""Semantic Scholar — tldr 한 줄 요약 담당.

tldr 은 문자열이 아니라 {"model": ..., "text": ...} 객체다. text 를 꺼내야 한다.

abstract 와 tldr 은 서로 독립이다. 실측 사례에서 abstract 는 null 인데 tldr 은
있었다. 초록이 없다고 요약도 없다고 단정하면 안 된다.

citationCount 는 OpenAlex cited_by_count 와 다르다 (실측 1341 vs 1805).
어느 쪽이 맞다고 판단하지 않고, 파이프라인이 OpenAlex 값을 우선하고
비어 있을 때만 이 값으로 채운다.

키가 없으면 공용 풀을 쓴다. 429 가 잦으므로 http 계층이 이 호스트만
1.2초 간격으로 조른다.
"""

import os
from urllib.parse import quote

from .. import http

BASE = "https://api.semanticscholar.org/graph/v1/paper/DOI:"
FIELDS = "title,abstract,tldr,citationCount,year,venue,isOpenAccess"


def _headers():
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    return {"x-api-key": api_key} if api_key else {}


def _tldr_text(tldr):
    if isinstance(tldr, dict):
        return tldr.get("text") or None
    return tldr or None


def fetch(doi=None, title=None):
    """DOI 로만 조회한다. 미발견이나 실패는 None."""
    if not doi:
        return None
    payload = http.get_json(
        BASE + quote(doi, safe="/"), params={"fields": FIELDS}, headers=_headers()
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
