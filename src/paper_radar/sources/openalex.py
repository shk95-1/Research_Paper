"""OpenAlex — 메인 검색 소스.

papers/sources/openalex.py 를 paper_radar contract 위로 이식한 것이다
(T5a). 파싱(순수 함수: restore_abstract/bare_doi/to_record)과 네트워크
드라이버(search/trend, transport 를 받는다)를 분리한다 — 파싱 함수는
transport 를 몰라야 테스트가 네트워크 없이도 결정적이다.

검색 모드
    기본 search= 는 본문 전문을 뒤진다. 2026-08-19 실측에서 'cosmetic' 이
    496,202건이었고 1위 결과는 화장품이 스쳐 지나가는 노출 모델링 논문이었다.
    title_and_abstract.search 는 137,680건. 근거 수집이 목적이므로 후자를 쓴다.

초록
    abstract_inverted_index 는 {"단어": [위치, ...]} 역색인이다. 상위 50건 중
    36건(72%)만 이 필드를 갖고 있었다. 나머지는 Semantic Scholar 와 Europe PMC
    보강으로 채운다. 즉 보강은 선택이 아니라 초록 확보의 필수 경로다.

DOI
    OpenAlex 는 'https://doi.org/10.xxxx/...' 형태의 전체 URL 로 준다.
    Crossref 와 Semantic Scholar 는 bare DOI 를 받으므로 여기서 정규화한다.

인증/폴라이트 풀
    api_key(OPENALEX_API_KEY)는 소스 코드가 직접 붙이지 않는다 — SourcePolicy
    의 auth_kind="param"/auth_name="api_key" 선언만으로 Transport 가 자동
    주입한다(값이 있을 때만). mailto 는 2026-02-13부터 OpenAlex 가
    mailto/polite pool 자체를 폐지했으므로 여기서는 아예 보내지 않는다
    (User-Agent 의 mailto 는 Crossref 용으로 Transport 가 별도로 유지한다).

None 의 의미
    search()/trend() 는 실패를 None 이나 빈 결과로 흡수하지 않는다 — 그건
    "이 입력으로 조회 자체가 불가능하다"는 뜻이 아니라 전송이 실패했다는
    뜻이므로, transport 가 던지는 타입 있는 예외가 그대로 전파된다.

페이지네이션 중간 실패와 iter_search (T5a 리뷰 Important 대응)
    OpenAlex 는 요청당 과금(계량제)이다. search() 가 리스트를 통째로 모아
    반환하는 방식이면, 중간 페이지에서 예외(특히 BudgetExhausted)가 나는
    순간 그 이전 페이지들 — 이미 돈을 지불한 결과 — 이 예외와 함께 통째로
    사라진다. iter_search() 는 페이지를 받을 때마다 그 페이지의 레코드를
    즉시 yield 하므로, 호출자가 제너레이터를 직접 순회하면 예외가 나기
    전까지 넘어온 레코드는 이미 호출자 손에 남아 있다. 예외 객체 자체에
    부분 결과를 부착하지는 않는다(예: exc.partial) — transport.errors 의
    타입 계약을 흐리고 싶지 않기 때문이다. search() 는 이 제너레이터를
    list() 로 감싼 편의 함수일 뿐이라 여전히 중간 실패 시 통째로 예외만
    던진다 — 부분 결과가 필요하면 iter_search() 를 직접 쓰라.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from paper_radar.contract import SourcePolicy
from paper_radar.registry import register

BASE = "https://api.openalex.org/works"

# 쓰는 필드만 요청한다. 응답 meta 에 cost_usd 가 있어 OpenAlex 가 사용량을 계량한다.
SELECT = ",".join(
    [
        "id",
        "doi",
        "title",
        "publication_year",
        "abstract_inverted_index",
        "is_retracted",
        "cited_by_count",
        "open_access",
        "primary_location",
        "topics",
        "keywords",
        "authorships",
        "type",
        "language",
    ]
)

PER_PAGE_MAX = 200
# group_by 응답은 per-page 에 잘린다. 실측에서 per-page=1 이면 연도 1개만 왔다.
GROUP_PER_PAGE = 200

DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")


@register
class OpenAlex:
    """레지스트리 등록용 얇은 표지 — 정책 선언 외에 상태를 갖지 않는다."""

    key: ClassVar[str] = "openalex"
    policy: ClassVar[SourcePolicy] = SourcePolicy(
        host="api.openalex.org",
        min_interval_s=0.1,  # 구속 조건은 인터벌이 아니라 일일 크레딧(계량제)이다
        auth_env="OPENALEX_API_KEY",
        auth_kind="param",
        auth_name="api_key",
    )


def restore_abstract(inverted):
    """역색인을 일반 텍스트로 되돌린다. 복원 불가면 None."""
    if not isinstance(inverted, dict) or not inverted:
        return None
    pairs = []
    for word, positions in inverted.items():
        if not isinstance(positions, (list, tuple)):
            continue
        for position in positions:
            if isinstance(position, int) and not isinstance(position, bool):
                pairs.append((position, word))
    if not pairs:
        return None
    pairs.sort(key=lambda pair: pair[0])
    return " ".join(word for _, word in pairs)


def bare_doi(value):
    """'https://doi.org/10.1/a' -> '10.1/a'. 없으면 None."""
    if not value:
        return None
    doi = value.strip().lower()
    for prefix in DOI_PREFIXES:
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
            break
    return doi or None


def _display_names(items):
    if not isinstance(items, list):
        return []
    return [
        item.get("display_name")
        for item in items
        if isinstance(item, dict) and item.get("display_name")
    ]


def _authors(authorships):
    if not isinstance(authorships, list):
        return []
    names = []
    for entry in authorships:
        author = (entry or {}).get("author") or {}
        name = author.get("display_name")
        if name:
            names.append(name)
    return names


def to_record(work):
    """OpenAlex work -> 내부 작업용 레코드. 보강 단계가 빈 칸을 채운다."""
    doi = bare_doi(work.get("doi"))
    source = (work.get("primary_location") or {}).get("source") or {}
    return {
        "doi": doi,
        "openalex_id": work.get("id"),
        "title": work.get("title"),
        "authors": _authors(work.get("authorships")),
        "year": work.get("publication_year"),
        "journal": source.get("display_name"),
        "abstract": restore_abstract(work.get("abstract_inverted_index")),
        "tldr": None,  # Semantic Scholar 가 채운다
        "keywords": _display_names(work.get("keywords")),
        "topics": _display_names(work.get("topics")),
        "citation_count": work.get("cited_by_count"),
        "is_open_access": bool((work.get("open_access") or {}).get("is_oa")),
        "url": f"https://doi.org/{doi}" if doi else work.get("id"),
        "is_retracted": bool(work.get("is_retracted")),
    }


def _filter(query, year_from, year_to):
    return ",".join(
        [
            f"title_and_abstract.search:{query}",
            f"from_publication_date:{year_from}-01-01",
            f"to_publication_date:{year_to}-12-31",
        ]
    )


def iter_search(transport, query, year_from, year_to, limit) -> Iterator[dict]:
    """커서 페이지네이션으로 limit 건까지, 페이지를 받는 즉시 그 페이지의
    레코드를 하나씩 yield 한다.

    중간에 transport 예외(예: 몇 페이지를 이미 받아 과금된 뒤 BudgetExhausted)
    가 나면 그대로 전파한다 — 다만 그 전에 yield 된 레코드는 이미 호출자
    손에 있으므로 예외와 함께 사라지지 않는다. 부분 결과가 필요 없다면
    search() 를 쓰라.
    """
    yielded = 0
    cursor = "*"
    while yielded < limit and cursor:
        params = [
            ("filter", _filter(query, year_from, year_to)),
            ("select", SELECT),
            ("per-page", str(min(PER_PAGE_MAX, limit - yielded))),
            ("cursor", cursor),
        ]
        payload = transport.get_json(BASE, params=params, policy=OpenAlex.policy)
        results = payload.get("results") or []
        if not results:
            return
        for work in results:
            if yielded >= limit:
                return
            yield to_record(work)
            yielded += 1
        cursor = payload.get("meta", {}).get("next_cursor")


def search(transport, query, year_from, year_to, limit):
    """iter_search() 를 리스트로 모으는 편의 래퍼.

    중간 실패 시 부분 결과를 반환하지 않는다 — transport 의 타입 있는 예외가
    그대로 전파된다(기존 papers/sources/openalex.py 는 실패 시 그때까지 모은
    결과만 돌려줬지만, "오류는 타입이다"라는 새 계약 아래서는 그 판단을
    파이프라인에 맡긴다). 예외가 나기 전까지 받은 레코드를 잃고 싶지 않은
    호출자는 iter_search() 를 직접 순회해야 한다.
    """
    return list(iter_search(transport, query, year_from, year_to, limit))


def trend(transport, query, year_from, year_to):
    """[(연도, 논문수), ...] 오름차순. 논문 본문을 받지 않으므로 호출 1회."""
    params = [
        ("filter", _filter(query, year_from, year_to)),
        ("group_by", "publication_year"),
        ("per-page", str(GROUP_PER_PAGE)),
    ]
    payload = transport.get_json(BASE, params=params, policy=OpenAlex.policy)
    groups = payload.get("group_by") or []
    counts = []
    for group in groups:
        key = str((group or {}).get("key", ""))
        if key.isdigit():
            counts.append((int(key), group.get("count") or 0))
    return sorted(counts)
