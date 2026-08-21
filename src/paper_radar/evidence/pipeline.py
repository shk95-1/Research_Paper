"""evidence 수집 파이프라인 조립 — papers/pipeline.py 의 데이터 주도 재조립.

OpenAlex 로 찾고, 나머지 세 소스로 빈 칸을 메우고, 점수를 매겨 저장한다.
papers/pipeline.py 는 이 조립을 하드코딩된 순서로 했다(semantic_scholar ->
europepmc -> crossref 를 함수 본문에 직접 나열). 이 모듈은 그 순서를
ENRICHERS: tuple[Enricher, ...] 라는 데이터로 뽑아낸다 — 순서·채움 규칙·
캐시 키가 전부 한 곳에 선언되어 있어, "소스를 하나 더 추가하려면 어디를
고쳐야 하나"라는 질문에 "ENRICHERS 에 항목 하나"로 답할 수 있다.

빈 칸 채우기 원칙 (papers/pipeline.py 와 동일)
    이미 값이 있으면 덮지 않는다. OpenAlex 를 1차 출처로 보고 나머지는
    비어 있는 자리만 채운다. citation_count 는 예외다: 0 도 유효한 인용수라서
    "값이 있으면 덮지 않는다"는 falsy 판정으로는 실제 0을 "없다"고 오판할 수
    있다 — 그래서 이 필드만 "record 쪽이 None 인가"로 판정한다(실측에서
    OpenAlex 1805 vs S2 1341 처럼 소스마다 인용수가 달랐으므로 OpenAlex 를
    우선한다).

캐시와 오류 타입 (None 과적 해소의 완성)
    papers/cache.py 시절에는 어떤 실패든(404 든 5xx 든 파싱 오류든) 전부
    None 으로 캐시했다 — "이 DOI 는 없다"와 "지금 서버가 잠깐 죽었다"를
    구분하지 못하고 둘 다 영구 부재로 캐시에 굳혀 버렸다. 이 모듈은 오류
    타입으로 캐시 여부를 가른다:
      - NotFound(404)      -> 부재 확정. None 을 캐시한다(재실행 시 재호출 안 함).
      - TransientError/RateLimited/ParseError/PermanentError
                            -> warn 한 줄 + 오류 카운트. 캐시하지 않는다
                               (다음 실행이 재시도할 수 있어야 한다).
      - BudgetExhausted     -> 그 소스가 오늘 예산을 다 썼다는 뜻. 재시도해도
                               무의미하므로 캐시하지 않고 그대로 collect() 까지
                               전파해 보강 루프 전체를 중단시킨다.
    캐시는 storage.cache.get/put 을 직접 쓴다 — storage.cache.fetch() 는
    loader() 의 예외를 구분하지 않고 그대로 흘려보내는 얇은 헬퍼라, "성공은
    캐시하고 어떤 실패는 캐시하지 않는다"는 이 정책을 표현할 수 없다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from paper_radar import registry
from paper_radar.evidence import verify
from paper_radar.sources import crossref, europepmc, openalex, semantic_scholar
from paper_radar.storage import cache, repository
from paper_radar.storage.runlog import RunLog
from paper_radar.transport import warn
from paper_radar.transport.errors import (
    BudgetExhausted,
    NotFound,
    ParseError,
    PermanentError,
    RateLimited,
    TransientError,
)
from paper_radar.transport.http import Transport

# 캐시·error_counts 에서 예상치 못한 KeyError 를 피하려고 warn+카운트 대상으로
# 묶는 오류 타입들. NotFound 와 BudgetExhausted 는 각각 별도 분기라 여기 없다.
_WARN_AND_COUNT = (TransientError, RateLimited, ParseError, PermanentError)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Enricher:
    """보강 소스 하나의 데이터 주도 표지.

    cache_key 가 빈 문자열을 돌려주면 호출도 캐시 조회도 모두 건너뛴다 —
    예를 들어 semantic_scholar/crossref 는 DOI 가 없으면 조회 자체가
    불가능하므로 cache_key 가 ""를 돌려주게 만들면 그 규칙이 그대로 구현된다.
    """

    name: str
    cache_key: Callable[[dict], str]
    call: Callable[[Transport, dict], dict | None]
    fills: tuple[tuple[str, str], ...]  # (enricher 반환 dict 키, 레코드 필드)


def _fill(record, field, value):
    """값이 있고 record 필드가 비어 있으면 채운다. 이미 값이 있으면 덮지 않는다."""
    if value and not record.get(field):
        record[field] = value


def _apply_fills(record, result, fills):
    for source_field, record_field in fills:
        value = result.get(source_field)
        if record_field == "citation_count":
            # 0 도 유효한 인용수다 — falsy 기준(_fill)이 아니라 "record 에
            # 아직 값이 없다(None)"로만 판정해야 실제 0을 지키면서 채운다.
            if record.get("citation_count") is None:
                record["citation_count"] = value
        else:
            _fill(record, record_field, value)


ENRICHERS: tuple[Enricher, ...] = (
    Enricher(
        name="semantic_scholar",
        cache_key=lambda record: record.get("doi") or "",
        call=lambda transport, record: semantic_scholar.fetch(transport, doi=record.get("doi")),
        fills=(
            ("tldr", "tldr"),
            ("abstract", "abstract"),
            ("journal", "journal"),
            ("citation_count", "citation_count"),
        ),
    ),
    Enricher(
        name="europepmc",
        # DOI 가 없으면 제목(200자 절단, 소문자)으로 캐시 키를 만든다 — DOI 가
        # 없는 논문도 Europe PMC 는 제목으로 찾을 수 있는 유일한 보강 소스다.
        cache_key=lambda record: record.get("doi")
        or (record.get("title") or "")[:200].strip().lower(),
        call=lambda transport, record: europepmc.fetch(
            transport, doi=record.get("doi"), title=record.get("title")
        ),
        fills=(
            ("abstract", "abstract"),
            ("journal", "journal"),
            ("keywords", "keywords"),
        ),
    ),
    Enricher(
        name="crossref",
        cache_key=lambda record: record.get("doi") or "",
        call=lambda transport, record: crossref.fetch(transport, doi=record.get("doi")),
        # 레코드 필드로는 journal 만 채운다 — Crossref 의 진짜 역할(제목 검증)은
        # 반환 dict 전체를 evidence["crossref"] 로 verify.build() 에 넘기는
        # 쪽에서 이뤄진다(아래 enrich() 참고).
        fills=(("journal", "journal"),),
    ),
)


def enrich(conn, transport, record, error_counts=None):
    """OpenAlex 레코드 하나를 보강하고 verification·evidence 를 붙인다.

    error_counts 는 소스이름 -> 누적 오류 건수 dict 로, 주어지면 이 함수가
    in-place 로 늘린다(collect() 가 run_source.errors 집계에 쓴다). 생략하면
    이 호출 안에서만 쓰고 버리는 빈 dict 를 새로 만든다 — 테스트에서
    enrich() 하나만 부를 때 이 인자를 신경 쓰지 않아도 되게 하기 위함이다.

    BudgetExhausted 는 여기서 삼키지 않는다 — 어느 소스에서 났는지를
    exc.source 에 실어(원래 예외에는 없는 속성이라 여기서 붙인다) 그대로
    collect() 까지 전파한다. collect() 는 이 신호로 보강 루프 전체를
    중단해야 하기 때문이다.
    """
    if error_counts is None:
        error_counts = {}
    record = dict(record)
    evidence: dict[str, dict] = {}
    sources = [verify.PRIMARY_SOURCE]

    for enricher in ENRICHERS:
        key = enricher.cache_key(record)
        if not key:
            continue

        cached = cache.get(conn, enricher.name, key)
        if cached is not cache.MISS:
            result = cached
        else:
            try:
                result = enricher.call(transport, record)
            except NotFound:
                cache.put(conn, enricher.name, key, None)
                result = None
            except BudgetExhausted as exc:
                exc.source = enricher.name
                raise
            except _WARN_AND_COUNT as exc:
                warn(f"{enricher.name}: {key!r} 처리 중 오류 ({exc})")
                error_counts[enricher.name] = error_counts.get(enricher.name, 0) + 1
                result = None
            else:
                cache.put(conn, enricher.name, key, result)

        if result:
            evidence[enricher.name] = result
            _apply_fills(record, result, enricher.fills)
            if enricher.name != "crossref":
                sources.append(enricher.name)

    record["verification"] = verify.build(record, found_in_sources=sources, evidence=evidence)
    record["collected_at"] = _now()
    return record


@dataclass(frozen=True)
class CollectReport:
    """collect() 실행 결과 요약. CLI 가 이 값으로 exit code 를 정한다.

    status 는 run.finish() 에 기록된 값과 같다("ok" | "partial") — "failed" 는
    collect() 가 예외로 끝나는 경우라 이 필드로는 절대 관측되지 않는다(그
    경로에서는 CollectReport 자체가 만들어지지 않고 예외가 호출자까지 전파된다).
    """

    run_id: str
    records: list[dict]
    errors_by_source: dict[str, int]
    stopped_reason: dict[str, str]
    status: str


def collect(
    conn, transport, query, year_from, year_to, limit, *, json_path=None, on_progress=None
) -> CollectReport:
    """검색 -> 보강 -> 검증 -> 저장, 그리고 실행 자체를 RunLog 에 기록한다.

    흐름
        RunLog.start() 로 run_id 를 발급받고 -> openalex.iter_search() 를
        점진 소비(제너레이터를 직접 순회 — search() 의 list() 래퍼를 쓰면
        중간 BudgetExhausted 시 이미 받은 레코드까지 통째로 사라진다. OpenAlex
        는 요청당 과금이라 이미 지불한 페이지를 버리는 것은 실제 손실이다)
        -> 레코드마다 enrich -> verify(는 enrich 안에서) -> repository.upsert
        -> dump_json -> record_source(소스별 requests/records/errors/
        budget_remaining/stopped_reason) -> finish(status).

    status 규칙
        모든 소스 정상 = "ok". BudgetExhausted(검색 또는 보강 단계) 나 소스
        오류(TransientError 등, warn 으로 넘어간 것)가 하나라도 있으면
        "partial". 이 함수 자체가 처리하지 못한 예외로 끝나면 "failed" —
        finally 블록이 보장한다(그래야 죽은 run 이 DB 에 "running" 으로
        영원히 남지 않는다).

    requests 카운트는 transport 에 매 시도(attempt)마다 불리는 observer 를
    설치해서 얻는다 — Transport(observer=...) 는 생성 시점에만 관측 훅을
    받을 수 있는데, RunLog 의 run_id 는 run.start() 를 부른 "뒤"에야 나오므로
    이미 만들어진 transport 에 set_observer() 로 나중에 붙인다. host -> source
    매핑은 registry.SOURCES 의 policy.host 로 만든다(Semantic Scholar 는
    current_policy() 로 인터벌만 바꾸고 host 는 ClassVar policy 와 항상
    같으므로 이 매핑에 영향이 없다). finally 에서 이 observer 를 다시 떼어내고
    이전 observer 로 되돌린다 — 안 그러면 Transport 인스턴스가 collect() 이후
    재사용될 때 끝난 run_id 로 fetch_log 가 계속 쌓이거나(원래 collect() 를
    다시 부르는 경우), 호출자가 미리 걸어 둔 observer 가 사라진다.
    """
    run = RunLog(conn)
    run_id = run.start(
        "evidence collect",
        {"query": query, "year_from": year_from, "year_to": year_to, "limit": limit},
    )

    host_to_source = {source_cls.policy.host: key for key, source_cls in registry.SOURCES.items()}
    request_counts: dict[str, int] = dict.fromkeys(registry.SOURCES, 0)
    error_counts: dict[str, int] = dict.fromkeys(registry.SOURCES, 0)
    stopped_reason: dict[str, str] = {}

    def observer(fetch, status, attempt, elapsed_ms, error):
        source = host_to_source.get(urlsplit(fetch.url).netloc)
        if source is None:
            return  # 등록되지 않은 host — 집계 대상 밖(발생하면 registry 등록 누락)
        request_counts[source] += 1
        run.log_fetch(
            run_id,
            source=source,
            url=fetch.url,
            status=status,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    # 이전 observer 를 기억해 뒀다가 finally 에서 되돌린다 — Transport 인스턴스가
    # collect() 종료 후에도 재사용될 수 있는데(같은 프로세스가 collect() 를 다시
    # 부르거나, 호출자가 자기 observer 를 걸어 둔 transport 를 넘기는 경우), 이걸
    # 안 하면 (a) 끝난 run_id 를 클로저로 문 이 콜백이 다음 요청에도 계속 불려
    # fetch_log 에 죽은 run 의 행이 계속 쌓이거나 (b) 바깥 호출자가 원래 걸어
    # 뒀던 observer 가 조용히 사라진다.
    previous_observer = transport.set_observer(observer)

    # finally 가 이 값을 그대로 기록한다 — 아래에서 "ok"/"partial" 로 갱신하지
    # 못하고 예외가 새면 "failed" 가 남는다(죽은 run 이 running 으로 안 남게).
    status = "failed"
    try:
        works = []
        try:
            for work in openalex.iter_search(transport, query, year_from, year_to, limit):
                works.append(work)
        except BudgetExhausted:
            stopped_reason["openalex"] = "budget_exhausted"

        stored = []
        for index, work in enumerate(works, start=1):
            if not repository.record_key(work):
                warn(f"식별자(DOI/openalex_id)가 없어 건너뜁니다: {work.get('title')!r}")
                continue
            try:
                enriched = enrich(conn, transport, work, error_counts)
            except BudgetExhausted as exc:
                stopped_reason[getattr(exc, "source", "unknown")] = "budget_exhausted"
                break
            verification = enriched.pop("verification")
            repository.upsert(conn, enriched, verification)
            public = repository.public_record(dict(enriched, verification=verification))
            stored.append(public)
            if on_progress:
                on_progress(index, len(works), public)

        if json_path:
            repository.dump_json(conn, json_path)

        source_records: dict[str, int] = dict.fromkeys(registry.SOURCES, 0)
        source_records["openalex"] = len(works)
        for public in stored:
            for name in (public.get("verification") or {}).get("evidence") or {}:
                source_records[name] = source_records.get(name, 0) + 1

        for key, source_cls in registry.SOURCES.items():
            run.record_source(
                run_id,
                key,
                requests=request_counts.get(key, 0),
                records=source_records.get(key, 0),
                errors=error_counts.get(key, 0),
                budget_remaining=transport.budget.remaining(source_cls.policy.host),
                stopped_reason=stopped_reason.get(key),
            )

        status = "partial" if stopped_reason or any(error_counts.values()) else "ok"
        return CollectReport(
            run_id=run_id,
            records=stored,
            errors_by_source=dict(error_counts),
            stopped_reason=dict(stopped_reason),
            status=status,
        )
    finally:
        run.finish(run_id, status)
        transport.set_observer(previous_observer)
