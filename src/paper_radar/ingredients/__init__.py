"""PubChem/CosIng 성분 실체 해소 파이프라인(T12).

    resolve -- 유일한 단계(T12 기준). 이름 -> PubChem 조회 -> 저장 -> RunLog 자기기록.

evidence/·trend/·trials/ 와 대칭 구조(코드리뷰 선례: trials.collect 가 원래
cli.py 안에 있다가 "cli.py 는 인자 파싱과 출력만 담당한다"는 계약을 어겨
옮겨진 사례를 이 태스크부터 그대로 따른다 — 처음부터 cli.py 밖에 둔다).
ingredient 는 papers 테이블도, trial 테이블도 아닌 완전히 별개의 저장 표면
(ingredient 테이블)을 쓰는 레코드 종류라 evidence/trend/trials 그 어느
쪽에도 속하지 않는 독립 패키지다.

계층 규칙(T7 가드 테스트가 강제, 이 태스크가 확장): 이 패키지는
paper_radar.evidence·paper_radar.trend·paper_radar.trials·paper_radar.cli 를
import 하지 않는다 — 다른 파이프라인들이 서로를 모르는 것과 같은 이유로,
네 파이프라인은 storage(RunLog)와 sources(레지스트리)만 공유한다.
"""
