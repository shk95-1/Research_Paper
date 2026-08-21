"""collect 파이프라인 조립.

OpenAlex 로 찾고, 나머지 셋으로 빈 칸을 메우고, 점수를 매겨 저장한다.

빈 칸 채우기 원칙
    이미 값이 있으면 덮지 않는다. OpenAlex 를 1차 출처로 보고 나머지는
    비어 있는 자리만 채운다. 인용수는 실측에서 소스마다 달랐으므로
    (OpenAlex 1805 vs S2 1341) OpenAlex 값을 우선한다.

실패 흡수
    소스 하나가 예외를 던져도 나머지가 계속 간다. http 계층이 이미 대부분을
    None 으로 흡수하지만, 응답 형태가 예상과 다를 때 소스 모듈 안에서 터질
    여지가 남아 있다. 논문 100건 수집이 한 건 때문에 멈춰서는 안 된다.

캐시
    소스별로 DOI 를 키로 캐시한다. 재실행하면 보강 호출이 0 이 되므로
    Semantic Scholar 의 1.2초 간격을 다시 물지 않는다.
"""

from datetime import UTC, datetime

from . import cache, store, verify
from .sources import crossref, europepmc, openalex, semantic_scholar


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(source_name, key, loader, conn):
    """캐시를 거쳐 loader 를 부른다. 무엇이 터져도 None 으로 끝난다."""

    def guarded():
        try:
            return loader()
        except Exception as exc:  # 소스 하나의 사고가 배치를 죽이지 않는다
            from . import http

            http.warn(f"{source_name}: {key or '(키 없음)'} 처리 중 오류 ({exc})")
            return None

    try:
        return cache.fetch(conn, source_name, key, guarded)
    except Exception as exc:
        from . import http

        http.warn(f"{source_name}: 캐시 오류 ({exc})")
        return None


def _fill(record, field, value):
    if value and not record.get(field):
        record[field] = value


def enrich(conn, record):
    """OpenAlex 레코드 하나를 보강하고 verification 을 붙인다."""
    doi = record.get("doi")
    sources = [verify.PRIMARY_SOURCE]

    s2 = (
        _safe("semantic_scholar", doi, lambda: semantic_scholar.fetch(doi=doi), conn)
        if doi
        else None
    )
    if s2:
        sources.append("semantic_scholar")
        _fill(record, "tldr", s2.get("tldr"))
        _fill(record, "abstract", s2.get("abstract"))
        _fill(record, "journal", s2.get("journal"))
        if record.get("citation_count") is None:
            record["citation_count"] = s2.get("citation_count")

    # Europe PMC 는 DOI 가 없으면 제목으로 찾는다. 캐시 키도 그에 맞춘다.
    epmc_key = doi or (record.get("title") or "")[:200].strip().lower()
    epmc = _safe(
        "europepmc", epmc_key, lambda: europepmc.fetch(doi=doi, title=record.get("title")), conn
    )
    if epmc:
        sources.append("europepmc")
        _fill(record, "abstract", epmc.get("abstract"))
        _fill(record, "journal", epmc.get("journal"))
        if not record.get("keywords"):
            record["keywords"] = epmc.get("keywords") or []

    crossref_data = _safe("crossref", doi, lambda: crossref.fetch(doi=doi), conn) if doi else None
    if crossref_data:
        _fill(record, "journal", crossref_data.get("journal"))

    record["verification"] = verify.build(record, crossref=crossref_data, found_in_sources=sources)
    record["collected_at"] = _now()
    return record


def collect(conn, query, year_from, year_to, limit, json_path=None, on_progress=None):
    """검색 -> 보강 -> 저장. 저장된 레코드 목록을 돌려준다."""
    works = openalex.search(query, year_from, year_to, limit)
    records = []
    for index, work in enumerate(works, start=1):
        if not store.record_key(work):
            from . import http

            http.warn(f"식별자(DOI/openalex_id)가 없어 건너뜁니다: {work.get('title')!r}")
            continue
        record = enrich(conn, work)
        store.upsert(conn, record)
        records.append(store.public_record(record))
        if on_progress:
            on_progress(index, len(works), record)
    if json_path:
        store.dump_json(conn, json_path)
    return records
