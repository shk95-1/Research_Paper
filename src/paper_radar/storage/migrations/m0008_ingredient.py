"""ingredient 테이블 추가 — 마이그레이션 0008.

PubChem(T12)이 채우고 CosIng(T13)이 조인하는 "성분 실체 해소" 테이블 —
"niacinamide / nicotinamide / vitamin B3 / CAS 98-92-0 / CID 936" 을 한 행으로
묶는다. repository.upsert_records() 의 "merge" 정책(TABLE_FOR)을 쓴다 —
PubChem 이 먼저 이름→CID/CAS/동의어를 채우고 CosIng 이 나중에 INCI 공식명을
보태는 두 소스 합류 지점이라, oa_location/trial 처럼 "최신 관측이 곧 진실"인
레코드가 아니다: 나중에 들어온 소스가 먼저 채워진 값을 지우면 안 된다(자세한
이유는 repository.py 의 TABLE_FOR/upsert_records() docstring 참고).

name_key 는 PubChem/CosIng 어느 쪽에서 왔든 같은 정규화 규칙(strip+casefold+
공백 축약, sources.pubchem.name_key() 참고)으로 만든 키라 PK 로 쓴다 —
oa_location.doi 와 같은 결의 자연키.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE ingredient (
    -- 정규화된 이름(strip+casefold+공백 축약). sources.pubchem.name_key() 참고
    name_key    TEXT PRIMARY KEY,
    -- CosIng 공식 INCI 명. NULL = CosIng 미조인(T13 이전, 또는 매치 실패)
    inci_name   TEXT,
    cid         INTEGER,            -- PubChem CID. NULL = PubChem 미해소
    cas         TEXT,               -- CAS 등록번호. NULL = 동의어 목록에서 찾지 못함
    synonyms    TEXT,               -- JSON 배열 문자열(동의어, 앞 50개 절단). NULL = 아직 없음
    -- JSON 배열 문자열("pubchem"/"cosing" 등). merge 정책이 합집합으로 누적
    sources     TEXT,
    fetched_at  TEXT NOT NULL       -- 이 행이 마지막으로 갱신된 시각(ISO UTC)
);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
