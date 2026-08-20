"""PubMed E-utilities — 월별 논문 수.

세 번째 무료 계수 소스다. OpenAlex 가 2026-08 기준 요청당 $0.001 을 물리고
무료 예산이 0 이라, 무료로 같은 질문에 답하는 곳이 필요했다.

PubMed 를 고른 이유는 MeSH 다. esearch 는 검색어를 통제어휘로 확장한다.
"cosmetic" 한 단어가 `"cosmetics"[MeSH Terms] OR "cosmetical"[All Fields] OR
"cosmetics"[Pharmacological Action] ...` 로 펼쳐지므로, 그 단어를 쓰지 않은
논문도 주제가 맞으면 잡힌다. 문자열 매칭보다 주제 매칭에 가깝다.

세 소스의 수치는 호환되지 않는다. 2019-01 실측으로 PubMed 1,205,
Europe PMC 843, Crossref 208 이다. 6배 차이는 논문 수의 차이가 아니라
무엇을 세는지의 차이다. 한 시계열에 이어붙이면 안 된다.

키 없이 초당 3회가 상한이다. http.MIN_INTERVAL 이 그것을 지킨다.
"""

import calendar

from .. import http

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def monthly_trend(query, year_from, year_to):
    """[("YYYY-MM", 논문수), ...] 오름차순. 월마다 요청 1회.

    [dp] 는 date of publication 이다. 논문 본문도 ID 목록도 받지 않고
    esearchresult.count 만 읽는다.

    못 물어본 달은 0 이 아니라 빠진다. 0 은 '논문이 없던 달'이라는 뜻이고,
    그 둘을 나중에 시계열에서 구별할 방법이 없다.
    """
    counts = []
    for year in range(year_from, year_to + 1):
        for month in range(1, 13):
            last = calendar.monthrange(year, month)[1]
            window = f"{year}/{month:02d}/01:{year}/{month:02d}/{last:02d}[dp]"
            payload = http.get_json(BASE, params={
                "db": "pubmed",
                "term": f"{query} AND {window}",
                "retmax": 0,
                "retmode": "json",
            })
            if not payload:
                continue
            raw = (payload.get("esearchresult") or {}).get("count")
            if raw is None:
                continue
            try:
                counts.append((f"{year}-{month:02d}", int(raw)))
            except (TypeError, ValueError):
                # count 가 숫자가 아니면 그 달을 지어내지 않고 버린다.
                http.warn(f"pubmed: {year}-{month:02d} count 가 숫자가 아님 ({raw!r})")
    return counts
