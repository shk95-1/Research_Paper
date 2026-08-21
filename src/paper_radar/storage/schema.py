"""스키마 버전 관리 — `PRAGMA user_version` 기반 마이그레이션 러너.

버전 0 은 "마이그레이션을 하나도 겪지 않은 DB"를 뜻하고, 두 가지 경우를
가릴 수 없다: (1) 방금 만든 빈 파일, (2) 구 papers/store.py 가 만든
DB(스키마는 있지만 버전 개념이 없었다). 0001 마이그레이션이 구 스키마와
문자 그대로 동일한 `CREATE TABLE IF NOT EXISTS` 를 실행하기 때문에 두 경우
모두 무해하게 통과해 버전 1 로 스탬프된다 — 이것이 "입양(adoption)"이다.

트랜잭션 제어에 sqlite3.Connection.autocommit(Python 3.12+)을 쓴다. 기본
(legacy) 모드에서는 `executescript()` 가 실행 전에 대기 중인 트랜잭션을
암묵적으로 커밋해버려서, 우리가 감싼 BEGIN IMMEDIATE 트랜잭션이 마이그레이션
중간에 끊기고 실패해도 롤백이 무력화된다(실측으로 확인). autocommit=True
로 두면 이 암묵적 커밋이 사라지고, 트랜잭션 경계를 우리가 낸 SQL
(BEGIN/COMMIT/ROLLBACK)로만 통제할 수 있다 — 그래서 이 모드에서는
Connection.commit()/rollback() 파이썬 메서드가 아니라 SQL 문 자체를 쓴다
(그 메서드들은 autocommit=True 에서 아무 효과가 없다). migrate() 를 벗어나면
호출자가 원래 쓰던 모드(레거시 커밋 방식)로 되돌려 놓는다 — repository.py 의
conn.commit() 호출 관례를 그대로 유지하기 위함이다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable

from paper_radar.storage.migrations import MIGRATIONS as _DEFAULT_MIGRATIONS

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def schema_version(conn: sqlite3.Connection) -> int:
    """현재 DB 의 PRAGMA user_version."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(
    conn: sqlite3.Connection,
    migrations: Iterable[Migration] | None = None,
) -> list[int]:
    """미적용 마이그레이션을 번호 순서대로 적용하고, 적용된 번호 목록을 돌려준다.

    각 마이그레이션은 개별 트랜잭션(BEGIN IMMEDIATE)으로 감싼다. 성공하면
    그 안에서 PRAGMA user_version 을 그 번호로 올린 뒤 커밋하고, 실패하면
    롤백하고 예외를 그대로 전파한다 — 절반 적용된 스키마가 남지 않는다.

    `migrations` 는 테스트에서 가짜(일부러 깨지는) 마이그레이션 목록을
    주입하기 위한 훅이다. 생략하면 실제 MIGRATIONS 를 쓴다.
    """
    ordered = tuple(migrations) if migrations is not None else _DEFAULT_MIGRATIONS
    previous_autocommit = conn.autocommit
    conn.autocommit = True  # BEGIN/COMMIT/ROLLBACK 을 이 함수가 직접 통제하기 위함
    try:
        applied: list[int] = []
        current = schema_version(conn)
        for number, _name, apply_fn in ordered:
            if number <= current:
                continue  # 이미 적용됨 — 재실행해도 무해해야 하므로(멱등) 건너뛴다
            conn.execute("BEGIN IMMEDIATE")
            try:
                apply_fn(conn)
                # PRAGMA 는 파라미터 바인딩(?)을 지원하지 않는다. number 는 이 모듈이
                # 만든 int 뿐이라 문자열 조립에 인젝션 경로가 없다.
                conn.execute(f"PRAGMA user_version = {int(number)}")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
            applied.append(number)
            current = number
        return applied
    finally:
        conn.autocommit = previous_autocommit
