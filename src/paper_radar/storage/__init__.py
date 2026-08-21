"""SQLite 저장소 계층 — 스키마 마이그레이션, 병합 upsert, 캐시, 수집 자기기록.

stdlib(sqlite3) 만 의존한다. T3 의 contract/models/transport 는 import 하지
않는다 — 저장소는 "무엇을 어떻게 수집했나"를 몰라도 동작해야 하고, 첫
소비자(T5b)가 실제로 무엇을 필요로 하는지 보기 전에 레코드 dataclass
디스패치 같은 걸 미리 지어두면 쓰는 사람 없는 기계가 된다.
"""
