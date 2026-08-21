"""papers.evidence 컬럼 추가 — 마이그레이션 0003.

소스별 검증 근거를 JSON 으로 담는다. 예:
    {"crossref": {"found": true, "title_similarity": 0.97}, "europepmc": {...}}

이 컬럼에 값을 채우는 것은 T5(verify 개편)의 몫이다. 이 마이그레이션은
컬럼만 만든다 — NULL 은 "아직 아무 근거도 기록되지 않았다"는 뜻이다.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE papers ADD COLUMN evidence TEXT")
