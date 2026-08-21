"""2단계: 원본 JSONL 에서 집계에 쓸 필드만 뽑는다.

papers_trend/records.py 를 이식했다. 순수 변환이다. 네트워크도 DB 도 만지지
않는다. 입력은 dict, 출력은 dict.

month_bucket 이 모든 집계의 시간 단위다. publication_date 를 월 단위로 절삭한다.
발행일이 연도까지만 있는 레코드는 버리지 않고 month_bucket=None,
date_precision="year_only" 로 남긴 뒤 월별 집계에서만 빠진다. 제외 건수는
summarize() 가 센다.

provider 차원 (T6)
    to_record() 가 레코드 dict 에 provider 를 붙인다. CSV 컬럼에는 넣지 않는다
    — provider 분리는 파일 시스템 수준(raw 경로, T15 의 mesh_monthly.csv 같은
    별도 파일)에서 이뤄진다. CSV 표면(컬럼/값/인코딩)은 과거 산출물과의 조인이
    걸린 공개 계약이라 손대지 않는다.

raw 경로 (T6)
    새 수집은 data/raw/{provider}/{query_id}/ 에 쓰인다(trend.collect 가
    쓴다). raw_dir()/raw_meta()/iter_raw() 는 이 새 경로를 기본으로 하되,
    없고 provider 가 openalex 면 구 경로(papers_trend/raw/{query_id}/)를
    fallback 으로 읽는다 — provider 차원이 생기기 전에 이미 모아 둔 수집분을
    재수집 없이 계속 쓰기 위해서다(이 raw 는 303MB 라 재수집 비용이 크다).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# src/paper_radar/trend -> parents[0]=paper_radar, [1]=src, [2]=저장소 루트.
# cli.py 의 DEFAULT_DB 계산(parents[2], cli.py 는 paper_radar 바로 아래)과
# 같은 규칙이되, 이 파일은 한 단계 더 깊은 trend/ 아래 있어 parents[2]가 된다.
REPO_ROOT = HERE.parents[2]

NEW_RAW_ROOT = REPO_ROOT / "data" / "raw"
LEGACY_RAW_ROOT = REPO_ROOT / "papers_trend" / "raw"
DEFAULT_PROVIDER = "openalex"

FIELDS = (
    "openalex_id",
    "doi",
    "title",
    "publication_date",
    "month_bucket",
    "date_precision",
    "year",
    "journal",
    "keywords",
    "topics",
    "concepts",
    "citation_count",
    "collected_at",
    "query_id",
    "provider",
)


def display_names(items):
    """list[dict] 에서 display_name 만. 원본 표기를 그대로 둔다 (정규화는 normalize.py)."""
    if not isinstance(items, list):
        return []
    return [
        item.get("display_name")
        for item in items
        if isinstance(item, dict) and item.get("display_name")
    ]


def parse_date(raw, fallback_year=None):
    """(month_bucket, date_precision, year) 를 돌려준다.

    OpenAlex 는 대체로 YYYY-MM-DD 를 주지만 YYYY 만 오는 레코드가 존재한다.
    그런 레코드는 버리지 않는다. 월별 집계에서만 빠진다.
    """
    text = (raw or "").strip()
    if len(text) >= 10:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            return parsed.strftime("%Y-%m"), "day", parsed.year
        except ValueError:
            pass
    if len(text) >= 7:
        try:
            parsed = datetime.strptime(text[:7], "%Y-%m")
            return parsed.strftime("%Y-%m"), "month", parsed.year
        except ValueError:
            pass
    if len(text) >= 4 and text[:4].isdigit():
        return None, "year_only", int(text[:4])
    if fallback_year:
        return None, "year_only", int(fallback_year)
    return None, "unknown", None


def bare_doi(value):
    if not value:
        return None
    doi = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
            break
    return doi or None


def to_record(work, query_id, collected_at=None, provider=DEFAULT_PROVIDER):
    """OpenAlex work 원본 -> 집계용 레코드. 실패하지 않는다."""
    month_bucket, precision, year = parse_date(
        work.get("publication_date"), work.get("publication_year")
    )
    source = (work.get("primary_location") or {}).get("source") or {}
    return {
        "openalex_id": work.get("id"),
        "doi": bare_doi(work.get("doi")),
        "title": work.get("title"),
        "publication_date": work.get("publication_date"),
        "month_bucket": month_bucket,
        "date_precision": precision,
        "year": year,
        "journal": source.get("display_name"),
        "keywords": display_names(work.get("keywords")),
        "topics": display_names(work.get("topics")),
        "concepts": display_names(work.get("concepts")),
        "citation_count": work.get("cited_by_count") or 0,
        "collected_at": collected_at,
        "query_id": query_id,
        "provider": provider,
    }


def new_raw_dir(query_id, provider=DEFAULT_PROVIDER):
    """이번 수집이 쓰는(그리고 읽어야 할 첫 번째) raw 디렉터리."""
    return NEW_RAW_ROOT / provider / query_id


def raw_dir(query_id, provider=DEFAULT_PROVIDER):
    """읽기용 raw 디렉터리를 고른다. 새 경로 우선, 없으면(openalex 한정) 구 경로.

    구 경로(papers_trend/raw/{query_id})는 provider 차원이 생기기 전 레이아웃
    이라 provider 가 openalex 일 때만 fallback 대상이다.
    """
    new = new_raw_dir(query_id, provider)
    if new.exists():
        return new
    if provider == DEFAULT_PROVIDER:
        legacy = LEGACY_RAW_ROOT / query_id
        if legacy.exists():
            return legacy
    return new


def raw_meta(query_id, provider=DEFAULT_PROVIDER):
    path = raw_dir(query_id, provider) / "_meta.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError, ValueError:
        return {}


def iter_raw(query_id, provider=DEFAULT_PROVIDER):
    """raw/*.jsonl 전체를 openalex_id(work id) 기준으로 중복 없이 흘려준다."""
    directory = raw_dir(query_id, provider)
    if not directory.exists():
        raise FileNotFoundError(f"{directory} 가 없습니다. 먼저 trend collect 를 실행하세요.")
    seen = set()
    for path in sorted(directory.glob("*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    work = json.loads(line)
                except ValueError:
                    continue
                identifier = work.get("id")
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                yield work


def load_records(query_id, provider=DEFAULT_PROVIDER):
    collected_at = raw_meta(query_id, provider).get("collected_at")
    return [
        to_record(work, query_id, collected_at, provider) for work in iter_raw(query_id, provider)
    ]


def summarize(records):
    precision = Counter(record["date_precision"] for record in records)
    months = Counter(record["month_bucket"] for record in records if record["month_bucket"])
    return {
        "total": len(records),
        "date_precision": dict(precision),
        "excluded_from_monthly": sum(1 for record in records if not record["month_bucket"]),
        "months": dict(sorted(months.items())),
        "unique_keywords": len({k for r in records for k in r["keywords"]}),
        "unique_topics": len({t for r in records for t in r["topics"]}),
        "unique_concepts": len({c for r in records for c in r["concepts"]}),
        "with_doi": sum(1 for record in records if record["doi"]),
    }
