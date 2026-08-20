"""논문 키워드 트렌드 모듈.

papers/ 와 목적이 다르다.
    papers/       = 근거 확보. 특정 주장을 뒷받침할 논문을 찾아 검증한다.
    papers_trend/ = 트렌드 관찰. 어떤 키워드가 떠오르고 어떤 것이 유지되는지 본다.

목적이 다르므로 모집단이 다르다. papers/ 는 표적 검색 결과라 모집단이 아니고,
이 모듈은 검색어에 걸리는 논문을 전수로 받는다.

단계는 파일을 읽어 파일을 쓴다. 수집은 한 번, 나머지는 반복 실행이 전제다.
사전과 불용어를 계속 키우게 되므로 2~5단계를 수십 번 다시 돌린다.

    collect_openalex.py  ->  raw/{query_id}/*.jsonl    네트워크. 여기만 느리다
    records.py           ->  필드 추출, 순수 변환
    normalize.py         ->  사전 적용
    weight.py            ->  순수 함수. aggregate 가 호출
    aggregate.py         ->  out/*.csv
    unmatched.py         ->  out/unmatched_terms.csv   사전 확장용
"""

import sys

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")
