"""신뢰도 산출.

순수 함수다. 저장된 레코드와 Crossref 응답만 보고 점수를 낸다. 네트워크도
DB도 만지지 않는다. 그래서 나중에 배점을 바꾸고 싶어지면 DB 를 다시 훑어
재계산하는 스크립트를 붙이기만 하면 된다.

배점 (스펙 7절)
    기본 5          OpenAlex 에 존재
    Crossref 30     DOI 가 실제로 등록되어 있다
    제목일치 25     등록된 제목이 OpenAlex 제목과 같다
    추가소스 15/개  Semantic Scholar, Europe PMC. 최대 30
    초록 10         근거로 인용할 본문이 있다
    철회면 총점 0   점수를 깎는 게 아니라 0 으로 만든다

    스펙 예시(2개 소스 + Crossref + 제목일치 + 초록)가 정확히 85점이 된다.

found_in_sources 에 crossref 는 넣지 않는다. Crossref 는 '발견'이 아니라
'검증'이고 이미 crossref_verified 로 30점을 따로 받는다. 양쪽에서 세면
이중 계산이다.
"""

import re
from difflib import SequenceMatcher

TITLE_MATCH_THRESHOLD = 0.85

SCORE_BASE = 5
SCORE_CROSSREF = 30
SCORE_TITLE_MATCH = 25
SCORE_PER_EXTRA_SOURCE = 15
SCORE_EXTRA_SOURCE_CAP = 30
SCORE_ABSTRACT = 10
SCORE_MAX = 100

PRIMARY_SOURCE = "openalex"
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_title(title):
    """비교용 정규화. 소문자, 구두점 제거, 공백 하나로."""
    if not title:
        return ""
    return " ".join(_PUNCTUATION.sub(" ", title.lower()).split())


def title_similarity(left, right):
    """0.0 ~ 1.0. 정확 일치를 요구하지 않는다. 어느 쪽이든 비면 0.0."""
    normalized_left = normalize_title(left)
    normalized_right = normalize_title(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def build(record, crossref=None, found_in_sources=None):
    """수집 시점에 한 번 호출한다. 결과는 레코드와 함께 저장된다."""
    sources = list(found_in_sources or [PRIMARY_SOURCE])
    crossref_verified = crossref is not None
    similarity = (
        title_similarity(record.get("title"), crossref.get("title"))
        if crossref_verified
        else 0.0
    )
    title_match = similarity >= TITLE_MATCH_THRESHOLD
    is_retracted = bool(record.get("is_retracted"))

    extra_sources = [name for name in sources if name != PRIMARY_SOURCE]
    score = SCORE_BASE
    if crossref_verified:
        score += SCORE_CROSSREF
    if title_match:
        score += SCORE_TITLE_MATCH
    score += min(len(extra_sources) * SCORE_PER_EXTRA_SOURCE, SCORE_EXTRA_SOURCE_CAP)
    if record.get("abstract"):
        score += SCORE_ABSTRACT
    score = min(score, SCORE_MAX)
    if is_retracted:
        score = 0

    return {
        "crossref_verified": crossref_verified,
        "title_match": title_match,
        "found_in_sources": sources,
        "is_retracted": is_retracted,
        "has_doi": bool(record.get("doi")),
        "confidence_score": score,
    }
