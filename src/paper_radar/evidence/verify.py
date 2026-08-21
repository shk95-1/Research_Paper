"""신뢰도 산출 — papers/verify.py 의 탈특권화 이식.

순수 함수다. 저장된 레코드와 소스별 evidence dict 만 보고 점수를 낸다.
네트워크도 DB 도 만지지 않는다. 그래서 나중에 배점을 바꾸고 싶어지면 DB 를
다시 훑어 재계산하는 스크립트를 붙이기만 하면 된다.

배점 (스펙 7절, papers/verify.py 와 상수 이름·값 전부 동일)
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

탈특권화: crossref -> evidence
    기존 build(record, crossref, found_in_sources) 는 Crossref 만 특별 취급하는
    명명 파라미터를 받았다. 이 모듈은 그 자리를 evidence: dict[str, dict] 로
    바꾼다 — {source_name: 그 소스가 반환한 원본 dict}. crossref_verified 는
    "crossref" in evidence 로 유도한다("crossref 응답을 받았다"가 검증의
    의미이지, 그 응답의 title 이 채워져 있는지는 상관없다 — 기존
    `crossref is not None` 과 동일한 의미다). crossref 항목이 있으면 그 안의
    title 로 기존과 동일한 제목 유사도 계산을 한다.

반환 스펙 확장: evidence 키 추가
    반환 dict 는 기존 6키(has_doi, crossref_verified, title_match,
    found_in_sources, is_retracted, confidence_score) 에 `evidence` 키가
    추가된 7키다. T4 의 repository.upsert() 가 verification["evidence"] 를
    evidence 컬럼에 저장한다 — 이 점수가 왜 나왔는지를 사후에 원본 응답으로
    추적할 수 있어야 하기 때문이다(예: crossref 제목이 실제로 뭐였길래
    title_match 가 False 였나). 기존 6키의 의미·타입은 완전히 그대로다.
"""

from __future__ import annotations

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


def build(record, found_in_sources=None, evidence=None):
    """수집 시점에 한 번 호출한다. 결과는 레코드와 함께 저장된다."""
    sources = list(found_in_sources or [PRIMARY_SOURCE])
    evidence = dict(evidence or {})
    crossref_verified = "crossref" in evidence
    crossref_evidence = evidence.get("crossref") or {}
    similarity = (
        title_similarity(record.get("title"), crossref_evidence.get("title"))
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
        "evidence": evidence,
    }
