"""8개 소스 모듈을 import 해 레지스트리에 등록하는 부작용을 낸다.

이 패키지를 import 하면 openalex/crossref/semantic_scholar/europepmc/
unpaywall/pubmed/clinicaltrials/pubchem 8개 모듈이 각자 @register 되어
paper_radar.registry.SOURCES 에 들어간다(T10 의 pubmed, T11 의
clinicaltrials, T12 의 pubchem 으로 5개에서 8개로 늘었다 — 리뷰 대응,
Finding 2: 이 파일이 그 뒤로 갱신되지 않아 3개가 빠져 있었다). 소스 자체를
쓰려면 각 서브모듈을 직접 import 해서 그 안의 search/trend/fetch 함수를
호출한다 — 이 파일은 등록 트리거일 뿐이다.

설계 사유 — 엔진/Lane 이 없는 이유
    trend-radar 교본은 엔진(스케줄러)이 여러 Lane(소스별 동시 실행 트랙) 위에
    소스를 얹어 동시에 여러 호스트를 두드리는 구조를 쓴다. 여기서는 그 계층을
    두지 않았다.

    이유: 동시성은 이 프로젝트의 비목표다(paper-radar 는 개인 연구 도구이고,
    배치 하나가 몇 분 더 걸려도 무방하다는 것이 T1~T3 에 걸쳐 확인된 전제다).
    소스도 8개뿐이라 순차 드라이버 — search/trend/fetch 가 Transport 를 직접
    받아 그 안에서 페이스·재시도·인증을 전부 처리 — 만으로 요구사항을 충분히
    충족한다. 엔진/Lane 이 주는 이점(동시 여러 호스트 스케줄링, 우선순위
    프론티어, 소스 간 공정성 분배)은 이 규모에서 비용만 더하는 과설계다
    (YAGNI) — 소스 수나 동시성 요구가 늘어나면 그때 다시 도입을 검토한다.
"""

from paper_radar.sources import (
    clinicaltrials,
    crossref,
    europepmc,
    openalex,
    pubchem,
    pubmed,
    semantic_scholar,
    unpaywall,
)

__all__ = [
    "clinicaltrials",
    "crossref",
    "europepmc",
    "openalex",
    "pubchem",
    "pubmed",
    "semantic_scholar",
    "unpaywall",
]
