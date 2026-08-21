"""papers_trend/ 를 이식한 논문 키워드 트렌드 파이프라인.

    collect -> records -> normalize -> weight -> aggregate -> unmatched

각 단계는 독립 모듈이다. collect 만 네트워크를 만진다(paper_radar.transport
+ paper_radar.storage.runlog 를 쓴다). 나머지는 raw JSONL/dict 를 입출력하는
순수 변환이며 DB 를 쓰지 않는다 — CSV 가 유일한 산출물이다.

계층 규칙(T7 가드 테스트가 강제): 이 패키지는 paper_radar.evidence 와
paper_radar.cli 를 import 하지 않는다. evidence 는 반대로 trend 를 모른다 —
두 파이프라인은 storage(RunLog)와 sources(OpenAlex 상수/정책)만 공유한다.
"""
