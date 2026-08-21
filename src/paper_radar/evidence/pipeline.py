"""evidence 수집 파이프라인 조립 — papers/pipeline.py 의 데이터 주도 재조립.

OpenAlex 로 찾고, 나머지 소스들로 빈 칸을 메우고, 점수를 매겨 저장한다.
papers/pipeline.py 는 이 조립을 하드코딩된 순서로 했다(semantic_scholar ->
europepmc -> crossref 를 함수 본문에 직접 나열, 세 소스뿐이었다). 이 모듈은
그 순서를 ENRICHERS: tuple[Enricher, ...] 라는 데이터로 뽑아낸다 — 순서·
채움 규칙·캐시 키가 전부 한 곳에 선언되어 있어, "소스를 하나 더 추가하려면
어디를 고쳐야 하나"라는 질문에 "ENRICHERS 에 항목 하나"로 답할 수 있다.
T10 이 이 형태 그대로 pubmed 를 europepmc 와 crossref 사이에 추가했다 —
지금은 semantic_scholar -> europepmc -> pubmed -> crossref 네 소스다.

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

OA 위치 해소 단계 (T8, Unpaywall) — enrich() 와 분리한 이유
    Unpaywall 은 ENRICHERS 에 넣지 않는다. ENRICHERS 는 papers 레코드의
    빈 칸을 채우고 verify.build() 의 evidence/found_in_sources 에 들어가
    confidence_score 에 영향을 준다 — 하지만 Unpaywall 은 "이 논문이
    맞는가"를 검증하는 서지 소스가 아니라 "합법적으로 어디서 읽을 수
    있는가"를 알려주는 부가 정보다. found_in_sources 에 넣으면 추가 소스
    +15점이 부당하게 붙는다(이 논문이 더 신뢰할 만해지는 게 아니라 그저
    OA 링크가 있을 뿐이다). 그래서 OA 해소는 enrich() 가 verify.build() 를
    이미 호출해 record["verification"] 을 확정한 "이후" 별도 단계로 두고,
    OaLocationRecord 를 repository.upsert_records() 로 oa_location 테이블에
    저장할 뿐 papers 레코드에는 손대지 않는다 — confidence_score 는
    unpaywall 유무와 완전히 무관해야 한다(회귀 방지 테스트로 고정).
    캐시·오류 처리는 다른 enricher 와 동일한 매트릭스를 따른다(cache
    source="unpaywall", key=doi).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from paper_radar import registry
from paper_radar.evidence import verify
from paper_radar.models import OaLocationRecord, RetractionRecord
from paper_radar.sources import crossref, europepmc, openalex, pubmed, semantic_scholar, unpaywall
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
# ValueError(T10): pubmed.parse_efetch_xml() 이 깨진/예상 밖 XML 을 만나면
# transport.errors 의 타입이 아니라 명시적 ValueError 를 던진다(efetch 응답은
# JSON 이 아니라 XML 이라 transport.get_json() 의 ParseError 경로를 타지
# 않기 때문 — pubmed.py 모듈 docstring 참고). "업스트림이 확정적으로 없다고
# 답했다"(NotFound)가 아니라 "이번 응답을 이해하지 못했다"는 뜻이므로 다른
# TransientError 류와 똑같이 warn+카운트하고 캐시하지 않는다 — 영구 부재로
# 캐시해 버리면 다음 실행이 재시도할 기회를 잃는다.
_WARN_AND_COUNT = (TransientError, RateLimited, ParseError, PermanentError, ValueError)

# oa_location 캐시 조회에 쓰는 source 이름. cache 테이블의 (source, key) 는
# ENRICHERS 의 이름들과 겹치지 않아야 한다 — "unpaywall" 은 ENRICHERS 에
# 없는 이름이라 자연히 분리된다.
_UNPAYWALL_CACHE_SOURCE = "unpaywall"


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


def _retraction_record(item, own_doi):
    """crossref.parse_retractions() 항목 하나를 RetractionRecord 로 조립한다.

    role 에 따라 doi/retraction_doi 자리가 뒤바뀐다(리뷰 Important 대응) —
    role="retracted"(relation 경로, 기본값)면 이 레코드 자신이 철회된
    논문이라 doi=own_doi, retraction_doi=item 이 가리키는 상대(공지) DOI.
    role="notice"(update-to 경로)면 이 레코드 자신이 철회 공지라 그 반대다:
    doi=item 이 가리키는 상대(원 논문) DOI, retraction_doi=own_doi(공지
    자신의 DOI). role 이 없는 항목(하위 호환)은 "retracted" 로 간주한다 —
    parse_retractions()/verify.build() 와 동일한 기본값이다.
    """
    if item.get("role", "retracted") == "notice":
        return RetractionRecord(
            doi=item.get("retraction_doi") or "",
            retraction_doi=own_doi,
            update_type=item.get("update_type") or "",
            update_date=item.get("update_date"),
            source="crossref",
        )
    return RetractionRecord(
        doi=own_doi,
        retraction_doi=item.get("retraction_doi"),
        update_type=item.get("update_type") or "",
        update_date=item.get("update_date"),
        source="crossref",
    )


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
        name="pubmed",
        # DOI 로만 조회한다(semantic_scholar/crossref 와 동일한 이유 —
        # pubmed.py 는 제목 검색을 지원하지 않는다). mesh_terms 는 fills
        # 규칙(비었을 때만 채움)으로 표현할 수 없어(다른 소스가 절대 채우지
        # 못하는 필드라 "비었으면 채운다"와 "항상 기록한다"가 관측상 같은
        # 결과를 내지만, "항상"이 의도임을 명시하려고) fills 에 넣지 않고
        # enrich() 안에서 별도로 처리한다(crossref 의 retractions 처리와
        # 같은 결의 예외).
        cache_key=lambda record: record.get("doi") or "",
        call=lambda transport, record: pubmed.fetch(transport, doi=record.get("doi")),
        fills=(
            ("abstract", "abstract"),
            ("journal", "journal"),
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

    철회 추적(T9): crossref 응답에 실려 온 retractions(evidence["crossref"]
    ["retractions"])가 비어 있지 않으면 _retraction_record() 로 RetractionRecord
    로 변환해 repository.upsert_records() 로 retraction 테이블에 저장한다.
    이건 Unpaywall(T8)의 OA 해소처럼 "완전히 별개 테이블"이면서
    confidence_score 와 무관한 부가 정보가 아니다 — 오히려 verify.build() 의
    is_retracted 입력(교차 검증)에 이미 반영되는 신호라서, 그 신호를 만드는
    evidence 가 이미 갖춰진 이 자리(enrich() 안, verify.build() 호출 직전)에서
    함께 저장하는 편이 "어디서 왔는지"와 "왜 저장했는지"가 한곳에 있어
    자연스럽다.

    수집 대상 자신이 철회 공지 문서일 수도 있다(OpenAlex 가 공지를 독립
    문서로 색인하는 경우) — 그때는 retractions 항목이 role="notice" 로
    태그돼 있고, _retraction_record() 가 doi/retraction_doi 자리를 뒤바꿔
    조립한다(자세한 이유는 그 함수 docstring 참고). verify.build() 도 같은
    role 을 봐서, role="notice" 뿐인 레코드(공지 문서 자신)는 철회'된' 논문이
    아니므로 점수를 0 으로 만들지 않는다.
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
            if enricher.name == "pubmed":
                # mesh_terms 는 다른 소스가 채울 수 없는 필드라 "비었을 때만
                # 채운다"는 _fill()/_apply_fills() 규칙과 무관하게 항상
                # 기록한다 — PubMed 가 성공적으로 응답했다는 사실 자체가
                # 최신 관측이고, 그 논문에 정말로 MeSH 가 없는 경우(빈 튜플)
                # 와 "아직 PubMed 를 조회하지 않음"(레코드에 이 키 자체가
                # 없음)을 구분하려면 빈 결과도 그대로 써야 한다 — 빈 튜플을
                # NULL 로 접는 정규화는 repository._norm_list_json() 이
                # 담당한다(캐시를 거친 result 는 JSON 왕복으로 tuple 이
                # list 가 되어 있을 수 있어 여기서 다시 tuple() 로 통일한다).
                record["mesh_terms"] = tuple(result.get("mesh_terms") or ())
            if enricher.name != "crossref":
                sources.append(enricher.name)

    retractions = (evidence.get("crossref") or {}).get("retractions") or ()
    if retractions:
        own_doi = record.get("doi") or ""
        repository.upsert_records(
            conn, [_retraction_record(item, own_doi) for item in retractions]
        )

    record["verification"] = verify.build(record, found_in_sources=sources, evidence=evidence)
    record["collected_at"] = _now()
    return record


def resolve_oa_location(conn, transport, doi, error_counts=None) -> OaLocationRecord | None:
    """DOI 하나에 대한 Unpaywall OA 위치를 캐시 경유로 얻는다.

    enrich() 의 오류×캐시 매트릭스와 동일하게 동작한다: NotFound 는 부재
    확정으로 캐시(None), TransientError 류는 캐시하지 않고 warn+카운트,
    BudgetExhausted 는 캐시하지 않고 exc.source="unpaywall" 을 붙여 그대로
    전파한다(collect() 가 이 신호로 보강 루프 전체를 중단한다).

    doi 가 없으면(빈 값) 호출도 캐시 조회도 하지 않고 None — DOI 없는
    논문은 애초에 조회 대상이 아니다.

    캐시에는 dataclass 를 그대로 넣을 수 없어(storage.cache 는 json.dumps
    로 직렬화한다) dataclasses.asdict() 로 평평한 dict 를 저장하고, 캐시
    히트 시 OaLocationRecord(**cached) 로 복원한다 — OaLocationRecord 의
    필드가 전부 JSON 원시 타입(str/bool/None)이라 이 왕복이 손실 없이 된다.
    """
    if error_counts is None:
        error_counts = {}
    if not doi:
        return None

    cached = cache.get(conn, _UNPAYWALL_CACHE_SOURCE, doi)
    if cached is not cache.MISS:
        return OaLocationRecord(**cached) if cached else None

    try:
        record = unpaywall.fetch(transport, doi)
    except NotFound:
        cache.put(conn, _UNPAYWALL_CACHE_SOURCE, doi, None)
        return None
    except BudgetExhausted as exc:
        exc.source = _UNPAYWALL_CACHE_SOURCE
        raise
    except _WARN_AND_COUNT as exc:
        warn(f"unpaywall: {doi!r} 처리 중 오류 ({exc})")
        error_counts[_UNPAYWALL_CACHE_SOURCE] = error_counts.get(_UNPAYWALL_CACHE_SOURCE, 0) + 1
        return None

    cache.put(
        conn, _UNPAYWALL_CACHE_SOURCE, doi, dataclasses.asdict(record) if record else None
    )
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
        -> (DOI 가 있으면) resolve_oa_location -> repository.upsert_records
        -> dump_json -> record_source(소스별 requests/records/errors/
        budget_remaining/stopped_reason) -> finish(status).

        OA 해소는 papers 레코드가 이미 저장된 "뒤"에 별도로 실행하고,
        그 결과(OaLocationRecord)는 papers 가 아니라 oa_location 테이블에만
        간다 — enriched 레코드도, verification 도 건드리지 않는다
        (evidence/pipeline.py 모듈 docstring의 "OA 위치 해소 단계" 참고).
        그래서 이 레코드의 confidence_score/found_in_sources 는 unpaywall
        조회 성공 여부와 완전히 무관하다.

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
        oa_saved = 0
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

            # OA 해소는 papers 저장이 끝난 뒤의 별도 단계다 — 이 레코드는
            # 이미 완전히 처리·저장됐으므로, 여기서 BudgetExhausted 가 나도
            # stored 에서 빼지 않는다(paper 자체는 성공했다는 사실이 바뀌지
            # 않는다). 루프는 여기서 멈춰 이후 레코드는 시도하지 않는다.
            try:
                oa_record = resolve_oa_location(conn, transport, public.get("doi"), error_counts)
            except BudgetExhausted as exc:
                stopped_reason[getattr(exc, "source", "unknown")] = "budget_exhausted"
                break
            if oa_record is not None:
                oa_saved += repository.upsert_records(conn, [oa_record])

        if json_path:
            repository.dump_json(conn, json_path)

        source_records: dict[str, int] = dict.fromkeys(registry.SOURCES, 0)
        source_records["openalex"] = len(works)
        source_records["unpaywall"] = oa_saved
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
