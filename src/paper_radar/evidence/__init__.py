"""evidence 수집 파이프라인 — verify(신뢰도 산출)와 pipeline(조립·저장·자기기록).

papers/pipeline.py + papers/verify.py 를 T3(contract/transport)·T4(storage)·
T5a(소스 4개) 위로 이식한 것(T5b). 하드코딩된 보강 순서를 데이터 주도
Enricher 목록으로, Crossref 특권 파라미터를 소스별 evidence dict 로 바꾸되
점수 배분과 verification 6키 표면은 그대로 둔다.
"""
