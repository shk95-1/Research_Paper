"""구 papers/store.py 스키마의 스냅샷 — 마이그레이션 0001.

이 SQL 은 papers/store.py 의 SCHEMA 상수를 문자 그대로 복사한 것이다.
전부 `IF NOT EXISTS` 라서 이미 이 스키마로 만들어진 구 DB(마이그레이션을
한 번도 겪지 않아 user_version=0 인 DB) 위에서 실행해도 아무것도 바꾸지
않고 무해하게 지나간다 — 그 뒤 러너가 user_version 을 1 로 스탬프하는 것이
"입양(adoption)"이다.

컬럼 정의가 papers/store.py 의 SCHEMA 와 한 글자라도 다르면 입양이 조용히
틀어진다(구 DB 위에서 CREATE TABLE 이 실패하거나, 다른 컬럼 집합으로 다시
만들어져 구 데이터와 어긋난다). 그래서 이 파일은 재작성이 아니라 사본이다.
"""

from __future__ import annotations

import sqlite3

# papers/store.py 의 SCHEMA 상수와 문자 그대로 동일해야 한다.
SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    key               TEXT PRIMARY KEY,
    doi               TEXT,
    openalex_id       TEXT,
    title             TEXT,
    authors           TEXT,
    year              INTEGER,
    journal           TEXT,
    abstract          TEXT,
    tldr              TEXT,
    keywords          TEXT,
    topics            TEXT,
    citation_count    INTEGER,
    is_open_access    INTEGER,
    url               TEXT,
    crossref_verified INTEGER,
    title_match       INTEGER,
    found_in_sources  TEXT,
    is_retracted      INTEGER,
    has_doi           INTEGER,
    confidence_score  INTEGER,
    collected_at      TEXT,
    raw               TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_confidence ON papers(confidence_score DESC);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);

CREATE TABLE IF NOT EXISTS cache (
    source     TEXT,
    key        TEXT,
    response   TEXT,
    fetched_at TEXT,
    PRIMARY KEY (source, key)
);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
