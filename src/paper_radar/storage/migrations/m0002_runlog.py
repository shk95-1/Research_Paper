"""수집 자기기록(runlog) 테이블 — 마이그레이션 0002.

trend-radar 교본의 "패턴 7"(수집 실행 자기기록) 을 이식한 것. 목적은 사후
디버깅이다: 이번 수집이 언제 시작해 언제 끝났는지, 소스별로 몇 건을
요청/수집/실패했는지, 예산이 왜 소진됐는지를 코드가 아니라 DB 를 보고
answered 할 수 있어야 한다.

run
    실행 한 번(예: `paper-radar evidence collect` 한 번의 CLI 호출) = 한 행.
    finished_at 이 NULL 이면 아직 진행 중이거나, 비정상 종료(프로세스가
    finish() 를 못 부르고 죽음)로 마무리를 기록하지 못한 것이다 — 둘을
    이 컬럼만으로는 구분 못 하지만, started_at 이 오래됐는데 여전히 NULL 이면
    후자로 의심할 수 있다.

run_source
    같은 실행 안에서 소스(openalex 등)별 집계. budget_remaining 이 NULL 이면
    응답 헤더에 x-ratelimit-remaining 자체가 없었다는 뜻(소스가 예산 헤더를
    안 주거나, 한 번도 요청을 못 보냈다) — 0 과는 다르다(0 은 "실제로 소진됐다").
    stopped_reason 이 NULL 이면 정상 완료다.

fetch_log
    요청 한 건 = 한 행. status 가 NULL 이면 응답 자체를 못 받았다(연결
    오류, timeout 등) — HTTP 상태 코드가 있는데 실패한 것과는 다른 종류의
    실패라서 구분한다. url 은 자격증명 파라미터(api_key 등)를 제거한
    뒤에만 기록해야 한다 — runlog.scrub_url() 이 그 책임을 진다.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE run (
    run_id            TEXT PRIMARY KEY,   -- uuid4 hex
    started_at        TEXT NOT NULL,      -- ISO UTC
    finished_at       TEXT,               -- NULL = 진행 중 또는 비정상 종료
    status            TEXT NOT NULL,      -- running / ok / partial / failed
    command           TEXT NOT NULL,      -- 어떤 CLI 명령이었나 (예: "evidence collect")
    args_json         TEXT NOT NULL,      -- 재현에 필요한 인자 (query, 연도, limit 등)
    collector_version TEXT NOT NULL,      -- 코드 버전 (pyproject version)
    schema_version    INTEGER NOT NULL    -- 실행 시점 user_version
);

CREATE TABLE run_source (
    run_id           TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    source           TEXT NOT NULL,       -- 소스 key ("openalex" 등)
    requests         INTEGER NOT NULL DEFAULT 0,
    records          INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0,
    budget_remaining INTEGER,             -- 종료 시점 x-ratelimit-remaining. NULL = 헤더 없음
    stopped_reason   TEXT,                -- NULL = 정상 완료 / budget_exhausted / transport_error
                                           -- / max_pages / max_months / error
    PRIMARY KEY (run_id, source)
);

CREATE TABLE fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    at         TEXT NOT NULL,
    source     TEXT NOT NULL,
    url        TEXT NOT NULL,             -- 자격증명 파라미터(api_key 등)는 기록 전에 제거
    status     INTEGER,                   -- NULL = 전송 실패 (연결 오류 등)
    attempt    INTEGER NOT NULL,
    elapsed_ms INTEGER,
    error      TEXT
);
CREATE INDEX ix_fetch_log_run ON fetch_log(run_id, at);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
