"""조회 결과 캐시 — papers/cache.py 의 이식 (동일 의미론).

'없음'도 캐시한다. Crossref 에 없는 DOI 를 매번 다시 물으면 재실행마다
같은 404 를 반복해서 받는다. 그래서 반환값 None 과 '캐시에 없음'을 구분해야
하고, 그 구분이 MISS 센티널의 존재 이유다.

키가 빈 문자열이면 캐시하지 않는다. DOI 없는 논문들이 한 칸을 공유하면
서로의 결과를 덮어쓴다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

MISS = object()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(conn: sqlite3.Connection, source: str, key: str):
    """캐시된 값. 저장된 적 없으면 MISS. 저장된 '없음'은 None."""
    if not key:
        return MISS
    row = conn.execute(
        "SELECT response FROM cache WHERE source = ? AND key = ?", (source, key)
    ).fetchone()
    if row is None:
        return MISS
    try:
        return json.loads(row["response"])
    except (TypeError, ValueError):
        return MISS


def put(conn: sqlite3.Connection, source: str, key: str, response: Any) -> None:
    if not key:
        return
    conn.execute(
        "INSERT INTO cache (source, key, response, fetched_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(source, key) DO UPDATE SET"
        " response = excluded.response, fetched_at = excluded.fetched_at",
        (source, key, json.dumps(response, ensure_ascii=False), _now()),
    )
    conn.commit()


def fetch(conn: sqlite3.Connection, source: str, key: str, loader):
    """캐시에 있으면 그것을, 없으면 loader() 를 부르고 결과를 캐시한다."""
    cached = get(conn, source, key)
    if cached is not MISS:
        return cached
    result = loader()
    put(conn, source, key, result)
    return result
