"""OpenAlex — 메인 검색 소스.

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
"""

import os

import calendar

from .. import http

BASE = "https://api.openalex.org/works"

# 쓰는 필드만 요청한다. 응답 meta 에 cost_usd 가 있어 OpenAlex 가 사용량을 계량한다.
SELECT = ",".join([
    "id", "doi", "title", "publication_year", "publication_date",
    "abstract_inverted_index",
    "is_retracted", "cited_by_count", "open_access", "primary_location",
    "topics", "keywords", "authorships", "type", "language",
])

PER_PAGE_MAX = 200
# group_by 응답은 per-page 에 잘린다. 실측에서 per-page=1 이면 연도 1개만 왔다.
GROUP_PER_PAGE = 200

DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")


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
            doi = doi[len(prefix):]
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
        # 연도만으로는 월 단위 지표와 붙지 않는다. OpenAlex 는 YYYY-MM-DD 를 준다.
        "date": work.get("publication_date"),
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
    return _date_filter(query, f"{year_from}-01-01", f"{year_to}-12-31")


def _date_filter(query, date_from, date_to):
    """OpenAlex filter 문자열. 절(clause) 구분자가 쉼표라는 점이 중요하다.

    query 에 쉼표가 있으면 서버는 그것을 새 절의 시작으로 읽는다. 결과는 대개
    4xx 이고 get_json 은 그것을 falsy 로 흡수하므로, 호출자는 '검색 결과 없음'과
    구별하지 못한 채 빈 목록을 받는다. 조용히 틀리느니 시끄럽게 거절한다.
    """
    if "," in query:
        raise ValueError(
            f"검색어에 쉼표를 쓸 수 없습니다: {query!r}. "
            "OpenAlex 는 쉼표를 filter 절 구분자로 읽습니다. 공백으로 나누세요."
        )
    return ",".join([
        f"title_and_abstract.search:{query}",
        f"from_publication_date:{date_from}",
        f"to_publication_date:{date_to}",
    ])


def monthly_trend(query, year_from, year_to):
    """[("YYYY-MM", 논문수), ...] 오름차순. 월마다 요청 1회.

    OpenAlex 는 publication_date 로 group_by 하지 못한다. 월 경계를 filter 에 넣고
    meta.count 만 읽는 편이, 논문을 전부 받아서 세는 것보다 훨씬 싸다. 8년치가
    96 요청이고 본문은 한 건도 받지 않는다.

    한 달이라도 물어보지 못했으면 그 달은 결과에서 빠진다. 0 으로 채우면 '논문이
    없었던 달'과 '못 물어본 달'이 같아지고, 그 둘을 시계열에서 되살릴 방법은 없다.
    """
    counts = []
    for year in range(year_from, year_to + 1):
        for month in range(1, 13):
            last = calendar.monthrange(year, month)[1]
            params = _polite({
                "filter": _date_filter(
                    query, f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last:02d}"
                ),
                "per-page": 1,
            })
            payload = http.get_json(BASE, params=params)
            if not payload:
                continue
            count = (payload.get("meta") or {}).get("count")
            if count is not None:
                counts.append((f"{year}-{month:02d}", count))
    return counts


def _polite(params):
    email = http.contact_email()
    if email:
        params["mailto"] = email
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    return params


def search(query, year_from, year_to, limit):
    """커서 페이지네이션으로 limit 건까지 모은다. 실패하면 모은 것만 돌려준다."""
    records = []
    cursor = "*"
    while len(records) < limit and cursor:
        params = _polite({
            "filter": _filter(query, year_from, year_to),
            "select": SELECT,
            "per-page": min(PER_PAGE_MAX, limit - len(records)),
            "cursor": cursor,
        })
        payload = http.get_json(BASE, params=params)
        if not payload:
            break
        results = payload.get("results") or []
        if not results:
            break
        records.extend(to_record(work) for work in results)
        cursor = (payload.get("meta") or {}).get("next_cursor")
    return records[:limit]


def trend(query, year_from, year_to):
    """[(연도, 논문수), ...] 오름차순. 논문 본문을 받지 않으므로 호출 1회."""
    params = _polite({
        "filter": _filter(query, year_from, year_to),
        "group_by": "publication_year",
        "per-page": GROUP_PER_PAGE,
    })
    payload = http.get_json(BASE, params=params)
    groups = (payload or {}).get("group_by") or []
    counts = []
    for group in groups:
        key = str((group or {}).get("key", ""))
        if key.isdigit():
            counts.append((int(key), group.get("count") or 0))
    return sorted(counts)
