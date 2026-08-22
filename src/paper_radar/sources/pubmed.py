"""PubMed E-utilities — MeSH 용어 보강.

MeSH(Medical Subject Headings)는 사람이 직접 매기는 통제 어휘다. OpenAlex 의
토픽 체계가 2025-10 에 어휘 자체를 갈아치운 것 같은 단절이 MeSH 에는 없다 —
그래서 트렌드 분석의 제2 축이자, evidence 레코드를 보강하는 안정적인 소스가
된다. 이 모듈은 evidence 보강(fetch/parse_efetch_xml)과, 월별 트렌드 수집
(T15, paper_radar.trend.collect_pubmed)이 쓰는 배치 조회(search_pmids 의
retstart/mindate/maxdate 확장, parse_efetch_batch, fetch_batch) 둘 다 다룬다.

배치 efetch — 다건을 한 번에 (T15)
    efetch 는 `id` 파라미터에 쉼표로 여러 PMID 를 한 번에 받는다(E-utilities
    공식 기능). 월별 수집처럼 논문 수백 편을 받아야 할 때 한 편씩 요청하면
    페이스 제한 때문에 시간이 논문 수에 비례해 늘어난다 — fetch_batch() 로
    한 번에 최대 EFETCH_BATCH_MAX 개까지 배치 요청한다. parse_efetch_batch()
    는 응답 안의 모든 PubmedArticle 을 순회한다(parse_efetch_xml() 은 첫
    번째 하나만 본다 — DOI 단건 조회는 매칭이 많아야 하나이므로 그걸로
    충분하다).

날짜 필터는 term 문자열에 끼워넣지 않는다 (T15)
    esearch 의 `mindate`/`maxdate`/`datetype` 은 E-utilities 가 제공하는
    공식 파라미터다. `f"({query}) AND 2024/01/01:2024/01/31[pdat]"` 처럼
    term 문자열 안에 날짜 범위를 직접 이어붙이면 날짜 형식·연산자 우선순위를
    스스로 다뤄야 해 파싱 오류 여지가 있다 — 공식 파라미터를 쓰면 그 여지가
    아예 없다.

두 단계 조회 — esearch 로 PMID 를 찾고, efetch 로 본문을 가져온다
    PubMed 는 Semantic Scholar/Crossref 처럼 "DOI 하나 → 레코드 하나"를 한
    번의 요청으로 주지 않는다. esearch 로 `{doi}[DOI]` 검색을 해서 PMID 를
    얻고, 그 PMID 로 efetch 를 다시 불러야 실제 제목/초록/MeSH 를 받는다.
    그래서 fetch() 한 번이 실제로는 요청 두 건이고, 두 건 모두
    current_policy() 가 돌려주는 같은 페이스 정책의 적용을 받는다(순차
    호출이라 페이스 제한이 두 번 다 걸린다는 뜻이지, 병렬로 나가는 게
    아니다).

제목 검색을 하지 않는 이유
    Europe PMC 는 DOI 가 없는 논문을 제목으로도 찾아준다(그게 DOI 없는
    논문이 점수를 받는 유일한 통로다). PubMed 도 제목 검색 자체는
    가능하지만, 실측상 부분 제목·유사 제목까지 걸려 오탐(다른 논문을
    잘못 매칭)이 크다 — Europe PMC 의 `TITLE:"..."` 처럼 통제된 쿼리가
    아니라 PubMed 의 자유 텍스트 검색은 훨씬 느슨하다. 이 태스크의 범위는
    "evidence 보강 + 검색 기초"까지이므로, 오탐 위험을 감수하면서까지 제목
    검색을 넣지 않는다 — DOI 가 없으면 그냥 None.

stdlib xml.etree.ElementTree 만 쓴다 (lxml 금지)
    이 저장소는 stdlib 의존만 허용한다(브리핑 제약). ElementTree 는 PubMed
    가 주는 정도 규모의 문서(논문 한 편)를 파싱하는 데 부족함이 없다.

파싱 실패는 ValueError — ParseError(transport)와 다른 결에 있다
    transport.errors.ParseError 는 "200 인데 JSON 이 아니다"를 뜻하는
    transport 계층의 타입이다. efetch 의 응답은 애초에 JSON 이 아니라
    XML 이므로 transport.get_json()을 쓰지 않는다(대신 transport.request()
    로 원문을 받아 이 모듈이 직접 xml.etree 로 파싱한다) — 그래서 그
    파싱은 transport 밖, 이 소스 모듈 안에서 일어난다. 실패했을 때 무엇을
    던질지는 이 모듈의 몫이고, "XML 형태 자체가 깨졌다/예상 구조가
    아니다"는 명시적 ValueError 로 던진다. evidence/pipeline.py 의 오류
    매트릭스는 이 ValueError 를 TransientError/RateLimited/ParseError/
    PermanentError 와 같은 묶음(warn 한 줄 + 오류 카운트, 캐시하지 않음)
    으로 취급하도록 확장돼 있다 — "업스트림이 확정적으로 없다고 답했다"
    (NotFound)가 아니라 "이번 응답을 이해하지 못했다"는 뜻이므로, 영구
    부재로 캐시해 버리면 다음 실행이 재시도할 기회를 잃는다.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import replace
from typing import ClassVar

from paper_radar.contract import Fetch, SourcePolicy
from paper_radar.registry import register

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

_ANON_INTERVAL_S = 0.34  # 무키 상한 3req/s → 요청 간 최소 간격
_KEYED_INTERVAL_S = 0.1  # 무료 키(NCBI_API_KEY) 상한 10req/s

# efetch id= 파라미터에 한 번에 넣을 PMID 개수 상한. E-utilities 문서가 명시한
# 강제 한도는 아니지만, NCBI 가 실무적으로 권장하는 상한이 200이다(URL 길이·
# 서버 부하 양쪽을 고려한 값) — 이보다 크게 배치하면 서버가 거부하거나
# 응답이 비정상적으로 느려질 수 있다는 보고가 있다. fetch_batch() 자신은
# 이 상한을 강제하지 않는다(순수하게 주어진 pmids 로 배치 요청 하나를 만들
# 뿐이다) — 호출자(trend.collect_pubmed)가 이 상수로 청크를 나눠야 한다.
EFETCH_BATCH_MAX = 200


@register
class PubMed:
    """레지스트리 등록용 얇은 표지. policy 는 "키 없음" 가정의 보수적
    기본값 — 실제 호출은 current_policy() 가 돌려주는 정책을 써야 한다."""

    key: ClassVar[str] = "pubmed"
    policy: ClassVar[SourcePolicy] = SourcePolicy(
        host="eutils.ncbi.nlm.nih.gov",
        min_interval_s=_ANON_INTERVAL_S,
        auth_env="NCBI_API_KEY",
        auth_kind="param",
        auth_name="api_key",
        # budget_is_daily 를 선언하지 않는다(기본값 False) — 리뷰 Finding2
        # 실측(2026-08-22): NCBI E-utilities 도 X-Ratelimit-Remaining 헤더를
        # 보내지만(X-Ratelimit-Limit: 3, X-Ratelimit-Remaining: 2, 무키
        # 3req/s 상한 기준), 이 값은 "초당" 레이트리밋 잔량이라 매 요청
        # 사이에 다시 찬다 — OpenAlex 식 일일 크레딧과 뜻이 다르다. True 로
        # 잘못 선언하면 매 PubMed 요청마다 "남은 예산 2" 오탐 경고가 난다.
    )


def current_policy() -> SourcePolicy:
    """env 에 NCBI_API_KEY 가 있으면 min_interval_s=0.1(10req/s), 없으면 0.34(3req/s).

    semantic_scholar.current_policy() 와 같은 패턴이다: replace() 로
    min_interval_s 하나만 바꿔서, ClassVar policy 의 다른 필드(auth_*,
    timeout_s 등)가 나중에 바뀌어도 이 함수가 그 값을 다시 나열하다 갱신을
    놓치는 일이 없게 한다.
    """
    if os.environ.get("NCBI_API_KEY", "").strip():
        return replace(PubMed.policy, min_interval_s=_KEYED_INTERVAL_S)
    return PubMed.policy


def search_pmids(
    transport, term, retmax=20, retstart=0, *, mindate=None, maxdate=None, datetype="pdat"
):
    """esearch 로 term 을 검색해 (PMID 목록, 총건수) 를 돌려준다.

    총건수(count)를 함께 돌려주는 이유(T15): 월별 트렌드 수집은 한 달의 결과가
    retmax 를 넘을 수 있어 retstart 로 여러 페이지를 이어 받아야 하고, "이
    달을 전수로 받았는지"(census 판정)도 count 와 실제로 받은 개수를 비교해야
    알 수 있다 — 둘 다 이 함수 밖에서는 얻을 수 없는 값이다. 기존 호출자
    (fetch())는 튜플의 [0](PMID 목록)만 쓰도록 조정했다.

    mindate/maxdate/datetype 을 함께 주면 esearch 가 그 날짜 범위로 결과를
    좁힌다(공식 파라미터를 쓰는 이유는 모듈 docstring 참고). 기본값 None 이면
    날짜 필터 없이 term 전체를 검색한다(기존 단건 DOI 조회 fetch() 의 동작과
    동일 — 날짜 파라미터를 아예 보내지 않는다).
    """
    params = [
        ("db", "pubmed"),
        ("term", term),
        ("retmax", str(retmax)),
        ("retstart", str(retstart)),
        ("retmode", "json"),
    ]
    if mindate is not None:
        params.append(("mindate", mindate))
    if maxdate is not None:
        params.append(("maxdate", maxdate))
    if mindate is not None or maxdate is not None:
        params.append(("datetype", datetype))
    payload = transport.get_json(BASE, params=params, policy=current_policy())
    esearchresult = (payload or {}).get("esearchresult") or {}
    idlist = esearchresult.get("idlist") or []
    pmids = [pmid for pmid in idlist if isinstance(pmid, str)]
    try:
        count = int(esearchresult.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    return pmids, count


def _text(element):
    """element 가 있으면 element 자신 + 모든 자손의 텍스트를 문서 순서대로
    이어 붙여 돌려준다. 없거나 텍스트가 비어 있으면 None.

    element.text 만 읽으면 중첩 마크업(<i>, <b>, <sub>, <sup> 등) 안쪽과
    그 뒤(tail)의 텍스트가 조용히 사라진다 — 예를 들어
    "<ArticleTitle>Effects of <i>Retinol</i> on skin.</ArticleTitle>" 에서
    element.text 는 "Effects of " 뿐이고 "Retinol on skin." 이 통째로
    없어진다. 화장품/피부과 코퍼스에서 <i>학명·성분명</i>, <sub>/<sup>
    화학식·유전자 표기는 실제로 흔하므로(가설적 케이스가 아니다) 이런
    손실은 논문 제목·MeSH 용어·초록 어디서든 발생할 수 있다.
    element.itertext() 는 element 자신의 text 부터 각 자식의 text/tail 까지
    문서 순서대로 순회하므로, 태그를 걷어낸 전체 텍스트를 얻을 수 있다.
    XML 이 들여쓰기·개행으로 예쁘게 포맷돼 있으면 그 공백도 그대로
    섞여 들어오므로, 마지막에 공백을 전부 하나로 접어 정규화한다.
    """
    if element is None:
        return None
    joined = "".join(element.itertext())
    normalized = " ".join(joined.split())
    return normalized or None


def _abstract_text(article_el):
    """Article/Abstract/AbstractText 를 합친다.

    AbstractText 가 여러 개(구조화 초록 — Background/Methods/Results 같은
    Label 속성이 붙은 섹션들)일 수 있다. Label 은 무시하고 본문 텍스트만
    등장 순서대로 공백 하나로 이어붙인다 — 이어붙인 결과가 검색·근거
    표시에 쓸 초록이지, 섹션 구조 자체를 보존할 필요는 없다. 섹션 하나하나의
    텍스트 추출은 _text() 를 재사용한다 — 섹션 안에 <i>/<sub> 같은 중첩
    마크업이 있어도(예: "...effects of <i>Retinol</i> on...") 놓치지
    않는다(모듈 docstring "_text()" 참고).
    """
    abstract_el = article_el.find("Abstract")
    if abstract_el is None:
        return None
    sections = abstract_el.findall("AbstractText")
    parts = [text for text in (_text(section) for section in sections) if text]
    return " ".join(parts) if parts else None


def _mesh_terms(citation_el):
    """MeshHeadingList/MeshHeading/DescriptorName 텍스트를 튜플로 모은다."""
    return tuple(
        _text(descriptor)
        for descriptor in citation_el.findall("MeshHeadingList/MeshHeading/DescriptorName")
        if _text(descriptor)
    )


def _doi_from_article_ids(article_el):
    """PubmedData/ArticleIdList/ArticleId[@IdType="doi"] 에서 DOI 를 찾는다.

    없으면 None(정상 — 모든 PubMed 문서가 DOI 를 갖고 있지는 않다). fetch()
    는 이미 doi 를 알고 있으므로 이 값을 쓰지 않지만(입력 doi 로 조회했으니
    같은 값이 되돌아올 뿐이다), parse_efetch_xml() 은 순수 파싱 함수로서
    응답에 있는 모든 관심 경로를 그대로 노출한다 — 장차 PMID 만으로 수집을
    시작하는 경로(T15 의 월별 트렌드 수집처럼 DOI 없이 PMID 부터 얻는 흐름)
    가 이 역참조로 DOI 를 되찾을 수 있어야 하기 때문이다.
    """
    pubmed_data = article_el.find("PubmedData")
    if pubmed_data is None:
        return None
    for article_id in pubmed_data.findall("ArticleIdList/ArticleId"):
        if article_id.get("IdType") == "doi":
            return _text(article_id)
    return None


def _parse_article(article_el):
    """PubmedArticle 엘리먼트 하나 -> {"pmid","title","abstract","journal",
    "mesh_terms","doi"} 딕셔너리, 또는 MedlineCitation 이 없으면 None.

    parse_efetch_xml()(첫 PubmedArticle 하나만)과 parse_efetch_batch()(모든
    PubmedArticle 순회, T15)가 공유하는 순수 파싱 조각이다 — 두 함수가 이
    로직을 각자 베끼면 한쪽만 고치고 잊는 회귀가 생긴다.
    """
    citation_el = article_el.find("MedlineCitation")
    if citation_el is None:
        return None

    pmid = _text(citation_el.find("PMID"))
    article = citation_el.find("Article")
    title = _text(article.find("ArticleTitle")) if article is not None else None
    abstract = _abstract_text(article) if article is not None else None
    journal = _text(article.find("Journal/Title")) if article is not None else None

    return {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "journal": journal,
        "mesh_terms": _mesh_terms(citation_el),
        "doi": _doi_from_article_ids(article_el),
    }


def parse_efetch_xml(xml_text):
    """efetch(retmode=xml) 응답 본문을 파싱한다. 순수 함수(네트워크 없음).

    반환: {"pmid", "title", "abstract", "journal", "mesh_terms", "doi"} 또는
    PubmedArticle 이 하나도 없으면(빈 결과 집합) None. mesh_terms 는 항상
    tuple(없으면 빈 튜플) — fetch() 가 "빈 튜플도 항상 기록한다"는 규칙을
    구현할 수 있으려면 "MeSH 가 없었다"와 "아직 안 물어봤다"를 구분할 값이
    있어야 하기 때문이다(NULL 정규화는 repository 쪽에서 일어난다).

    XML 자체가 깨졌으면(태그가 안 닫혔거나 인코딩이 망가진 경우) ET.ParseError
    를 잡아 명시적 ValueError 로 다시 던진다 — 모듈 docstring 의 "파싱 실패는
    ValueError" 설명 참고.

    PubmedArticleSet 안 **첫 번째** PubmedArticle 만 읽는다 — DOI 단건 조회는
    매칭되는 PMID 가 많아야 하나이므로 그걸로 충분하다. 여러 건을 다 읽어야
    하면 parse_efetch_batch() 를 쓴다(T15, fetch_batch() 의 배치 응답용).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"PubMed efetch XML 파싱 실패: {exc}") from exc

    article_el = root.find("PubmedArticle")
    if article_el is None:
        return None
    return _parse_article(article_el)


def parse_efetch_batch(xml_text):
    """efetch(retmode=xml) 배치 응답(PubmedArticle 여러 건)을 전부 파싱한다.

    parse_efetch_xml() 과 달리 PubmedArticleSet 안의 **모든** PubmedArticle
    을 `root.findall("PubmedArticle")` 로 순회한다(fetch_batch() 의 배치
    호출 결과, T15). 반환은 list[dict] — 결과 집합이 비었으면(PubmedArticle
    이 하나도 없음) 빈 리스트(None 아님 — 배치 호출은 "무언가는 있었다"가
    전제라 단건 조회의 "그 PMID 는 없다" 의미의 None 과는 성격이 다르다).
    MedlineCitation 이 없는 개별 article(비정상 항목)은 조용히 걸러진다 —
    한 항목의 결손이 배치 전체를 실패시키지 않는다.

    XML 자체가 깨졌으면 parse_efetch_xml() 과 동일하게 ValueError.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"PubMed efetch XML 파싱 실패: {exc}") from exc

    parsed = (_parse_article(article_el) for article_el in root.findall("PubmedArticle"))
    return [article for article in parsed if article is not None]


def fetch(transport, doi=None, title=None):
    """DOI 로만 조회한다. DOI 가 없으면 None(제목 검색은 하지 않는다 — 모듈
    docstring "제목 검색을 하지 않는 이유" 참고. title 인자는 다른 소스와
    같은 fetch(transport, doi=, title=) 시그니처를 맞추기 위한 것일 뿐 이
    소스에서는 쓰이지 않는다).

    esearch `{doi}[DOI]` 로 PMID 를 찾는다 — 0건이면 None(그 DOI 를 PubMed
    가 색인하지 않았다는 정상적인 부재이지, 오류가 아니다). PMID 를 찾으면
    efetch 로 본문을 가져와 parse_efetch_xml() 로 파싱한다. DOI 는 유일 식별자
    이므로 매칭되는 PMID 는 많아야 하나다 — retmax=1 로 충분하다.

    반환 키는 title/abstract/journal/mesh_terms/pmid 다섯 개로 고정한다(다른
    보강 소스와 마찬가지로 evidence dict 에 그대로 실린다) — doi 는 이미
    호출자가 아는 값이라 되돌려주지 않는다(parse_efetch_xml() 은 순수
    파싱 함수라 doi 도 노출하지만, fetch() 계약은 여기서 그 값을 뺀다).

    404(NotFound)는 다른 소스들과 마찬가지로 여기서 잡지 않고 그대로
    전파한다. XML 파싱 실패(ValueError)도 잡지 않고 전파한다 — 캐시 여부
    판단은 evidence/pipeline.py 의 오류 매트릭스가 한다.
    """
    if not doi:
        return None
    pmids, _count = search_pmids(transport, f"{doi}[DOI]", retmax=1)
    if not pmids:
        return None

    payload = transport.request(
        Fetch(
            url=EFETCH_URL,
            params=(("db", "pubmed"), ("id", pmids[0]), ("retmode", "xml")),
        ),
        current_policy(),
    )
    parsed = parse_efetch_xml(payload.text())
    if parsed is None:
        return None
    return {
        "title": parsed["title"],
        "abstract": parsed["abstract"],
        "journal": parsed["journal"],
        "mesh_terms": parsed["mesh_terms"],
        "pmid": parsed["pmid"],
    }


def fetch_batch(transport, pmids):
    """efetch 로 여러 PMID 를 한 번에 받아 parse_efetch_batch() 로 파싱한다(T15).

    `id` 파라미터에 PMID 를 쉼표로 이어붙여 한 번의 요청으로 pmids 전부를
    요청한다 — pmids 개수에 EFETCH_BATCH_MAX 상한을 이 함수 자신은 강제하지
    않는다(모듈 docstring 참고: 호출자가 청크를 나눈다). pmids 가 비어 있으면
    요청 자체를 보내지 않고 빈 리스트를 돌려준다(빈 id= 로 나가는 요청은
    NCBI 쪽에서 어떻게 응답할지 실측하지 않았고, 애초에 보낼 이유가 없다).

    404/BudgetExhausted/재시도 소진 등 transport 오류는 fetch() 와 마찬가지로
    잡지 않고 그대로 전파한다 — 이 함수는 순수 조회이고, 실패했을 때 무엇을
    할지(건너뛴다/중단한다)는 호출자(trend.collect_pubmed)의 몫이다.
    """
    if not pmids:
        return []
    payload = transport.request(
        Fetch(
            url=EFETCH_URL,
            params=(("db", "pubmed"), ("id", ",".join(pmids)), ("retmode", "xml")),
        ),
        current_policy(),
    )
    return parse_efetch_batch(payload.text())
