"""ClinicalTrials.gov v2 임상시험 레코드 파이프라인(T11).

    collect -- 유일한 단계. 검색 -> 저장 -> RunLog 자기기록.

evidence/·trend/ 와 대칭 구조(코드리뷰 대응: 이 러너가 원래 cli.py 안에
있었는데, cli.py 의 "인자 파싱과 출력만 담당한다"는 계약을 어기고
evidence.pipeline/trend.collect 에 이은 세 번째 사본을 CLI 계층에 만들었다는
지적으로 여기로 옮겼다). trials 는 papers 테이블도, trend 의 CSV 산출물도
아닌 완전히 별개의 저장 표면(trial 테이블)을 쓰는 레코드 종류라 evidence/
trend 그 어느 쪽에도 속하지 않는 독립 패키지다.

계층 규칙(T7 가드 테스트가 강제, 이 태스크가 확장): 이 패키지는
paper_radar.evidence·paper_radar.trend·paper_radar.cli 를 import 하지
않는다 — evidence/trend 가 서로를 모르는 것과 같은 이유로, 세 파이프라인은
storage(RunLog)와 sources(레지스트리)만 공유한다.
"""
