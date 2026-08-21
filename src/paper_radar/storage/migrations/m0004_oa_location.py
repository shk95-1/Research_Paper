"""oa_location 테이블 추가 — 마이그레이션 0004.

Unpaywall(T8)이 채우는 테이블. DOI 하나당 "최신 관측" 스냅샷 1행만 둔다 —
papers 의 병합(COALESCE) upsert 와 달리, 여기는 repository.upsert_records()
의 "overwrite" 정책(자연키 충돌 시 전 컬럼을 새 값으로 무조건 갱신)을 쓴다.
이유: OA 상태는 시간에 따라 실제로 바뀐다(엠바고 해제, 출판사 정책 변경,
저장소 등록 등) — 과거 값과 병합하면 "예전엔 pdf_url 이 있었는데 지금은
사라졌다" 같은 정정을 반영하지 못하고 죽은 링크가 영원히 남는다. 이번
관측이 이전 관측보다 항상 더 진실에 가깝다.

PDF 파일 자체는 이 테이블에도, 어디에도 저장하지 않는다 — pdf_url 은
링크(URL 문자열)일 뿐이다.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE oa_location (
    doi         TEXT PRIMARY KEY,   -- bare DOI. papers.doi 와 같은 규약(소문자, 프리픽스 없음)
    is_oa       INTEGER NOT NULL,   -- 0/1
    oa_status   TEXT NOT NULL,      -- gold/green/hybrid/bronze/closed
    pdf_url     TEXT,               -- NULL = OA 아님/직링크 없음. 파일이 아니라 링크만 저장한다
    landing_url TEXT,
    host_type   TEXT,               -- publisher | repository
    license     TEXT,
    checked_at  TEXT NOT NULL       -- OA 상태는 시간에 따라 변한다 — 이 시점의 관측값
);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
