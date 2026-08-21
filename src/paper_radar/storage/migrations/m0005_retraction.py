"""retraction 테이블 추가 — 마이그레이션 0005.

Crossref(T9)가 채우는 테이블. 논문 하나가 여러 개의 갱신 신호(예: 철회 공지
DOI 가 나중에 바뀌는 경우)를 가질 수 있어 PK 를 doi 단일 컬럼이 아니라
(doi, retraction_doi) 복합키로 둔다 — repository.upsert_records() 의
"overwrite" 정책과 맞물려, 같은 (doi, retraction_doi) 쌍은 최신 관측으로
덮어쓰고 다른 쌍은 별도 행으로 쌓인다.

retraction_doi 는 NOT NULL DEFAULT '' 다 — SQLite 는 PK 를 구성하는 컬럼에도
역사적으로 NULL 을 허용하고(표준 SQL 의 PK NOT NULL 암묵 규칙과 다르다),
NULL 은 자기 자신과도 "다르다"고 비교되므로 UNIQUE/PK 제약이 NULL 값의
중복을 걸러내지 못한다 — retraction_doi=None(공지 DOI 미상)인 행을 여러 번
upsert 하면 조용히 여러 행이 쌓이는 버그가 생긴다. repository.py 가 저장
전에 None -> '' 로 강제해 이 특례를 피해간다(자세한 이유는 repository.py
의 upsert_records() 참고).
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE retraction (
    doi            TEXT NOT NULL,            -- 철회된 논문의 bare DOI
    retraction_doi TEXT NOT NULL DEFAULT '', -- 철회 공지의 DOI. '' = 공지 DOI 미상
    update_type    TEXT NOT NULL,
    update_date    TEXT,                     -- NULL = 날짜 미상
    source         TEXT NOT NULL,            -- "crossref"
    PRIMARY KEY (doi, retraction_doi)
);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
