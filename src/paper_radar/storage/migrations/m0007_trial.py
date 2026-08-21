"""trial 테이블 추가 — 마이그레이션 0007.

ClinicalTrials.gov v2(T11)가 채우는 테이블. repository.upsert_records() 의
"overwrite" 정책(TABLE_FOR)을 쓴다 — 시험 상태(overallStatus, phase, 결과
게시 여부 등)는 시간에 따라 실제로 바뀌므로(모집 중 -> 완료, 결과 미게시
-> 게시 등) oa_location(T8)과 같은 이유로 과거 값과 병합하지 않고 최신
관측으로 무조건 덮어쓴다.

nct_id 는 ClinicalTrials.gov 가 부여하는 고유 식별자라 그대로 PK 로 쓴다
(oa_location.doi 와 같은 결의 자연키 — retraction.retraction_doi 처럼
"미상"을 표현할 필요가 없는 컬럼이라 NOT NULL DEFAULT '' 특례가 없다).
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE trial (
    nct_id         TEXT PRIMARY KEY,   -- ClinicalTrials.gov 식별자 (예: NCT01234567)
    title          TEXT NOT NULL,      -- briefTitle. 응답에 없으면 빈 문자열
    status         TEXT NOT NULL,      -- overallStatus (COMPLETED/RECRUITING/...). 없으면 빈 문자열
    phase          TEXT,               -- phases[0]. NULL = phase 미기재(관찰 연구 등)
    sponsor_class  TEXT,               -- leadSponsor.class (INDUSTRY/OTHER/NIH/...). NULL = 미기재
    enrollment     INTEGER,            -- enrollmentInfo.count. NULL = 미공개
    conditions     TEXT,               -- JSON 배열 문자열. NULL = 조건 미기재
    interventions  TEXT,               -- JSON 배열 문자열(중재명만). NULL = 중재 미기재
    outcomes_json  TEXT NOT NULL,      -- outcomesModule 원본 보존(sort_keys 직렬화)
    first_posted   TEXT,               -- studyFirstPostDateStruct.date (YYYY-MM-DD). NULL = 미기재
    results_posted INTEGER NOT NULL,   -- hasResults. 0/1
    url            TEXT NOT NULL,      -- https://clinicaltrials.gov/study/{nctId}
    matched_query  TEXT NOT NULL,      -- 어떤 검색어로 걸렸는지 — 수집 맥락 없이는 해석 불능
    captured_at    TEXT NOT NULL       -- 이 관측 시점(ISO UTC)
);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
