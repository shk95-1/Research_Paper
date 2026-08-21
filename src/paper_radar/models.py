"""신규 소스가 만드는 레코드 타입.

이 태스크에서는 신규 레코드 타입만 정의한다 — 기존 papers 파이프라인이
만드는 레코드(OpenAlex/Crossref/Semantic Scholar/EuropePMC)는 T5 에서
이식되더라도 dict 그대로 유지한다. 이미 나가고 있는 산출물 표면을 바꾸는
것은 이번 이식 범위 밖의 위험이라는 것이 원장(ledger)의 판단이다.

각 클래스는 NATURAL_KEY(ClassVar[tuple[str, ...]]) 를 선언한다 — 저장 계층
(T4)이 upsert 시 사용할 자연키다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class OaLocationRecord:
    """Unpaywall 이 만든다. T8(오픈액세스 링크 보강)이 소비한다."""

    NATURAL_KEY: ClassVar[tuple[str, ...]] = ("doi",)

    doi: str
    is_oa: bool
    oa_status: str  # gold/green/hybrid/bronze/closed
    pdf_url: str | None  # 대용량 정책: 링크만 저장한다. 파일 자체는 받지 않는다
    landing_url: str | None
    host_type: str | None  # publisher | repository
    license: str | None
    checked_at: str  # ISO UTC


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """ClinicalTrials.gov v2 가 만든다. T11(임상시험 연계)이 소비한다."""

    NATURAL_KEY: ClassVar[tuple[str, ...]] = ("nct_id",)

    nct_id: str
    title: str
    status: str
    phase: str | None
    sponsor_class: str | None  # INDUSTRY / OTHER / NIH / ...
    enrollment: int | None
    conditions: tuple[str, ...]
    interventions: tuple[str, ...]
    outcomes_json: str  # 구조 보존용 직렬화 JSON
    first_posted: str | None  # YYYY-MM-DD
    results_posted: bool
    url: str
    matched_query: str  # 어떤 검색어로 걸렸는지 — 수집 맥락 없이는 해석 불능
    captured_at: str


@dataclass(frozen=True, slots=True)
class RetractionRecord:
    """Crossref update-type 이 만든다. T9(철회 논문 추적)이 소비한다."""

    NATURAL_KEY: ClassVar[tuple[str, ...]] = ("doi", "retraction_doi")

    doi: str  # 철회된 논문 자신의 DOI
    retraction_doi: str | None  # 철회 공지 자체의 DOI
    update_type: str  # retraction / correction / ...
    update_date: str | None
    source: str  # "crossref"


@dataclass(frozen=True, slots=True)
class IngredientRecord:
    """PubChem/CosIng 이 만든다. T12·T13(성분 정규화)이 소비한다."""

    NATURAL_KEY: ClassVar[tuple[str, ...]] = ("name_key",)

    name_key: str  # 정규화된 소문자 이름
    inci_name: str | None
    cid: int | None  # PubChem CID
    cas: str | None
    synonyms: tuple[str, ...]
    sources: tuple[str, ...]  # 이 정보가 어디서 왔나 ("pubchem", "cosing")
    fetched_at: str
