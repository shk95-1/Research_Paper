"""RunLog — 수집 실행 자기기록.

trend-radar 교본의 "패턴 7"을 이식한 것: 수집 파이프라인이 스스로 언제
시작해서 무엇을 얼마나 요청/수집/실패했는지 기록한다. 목적은 사후
디버깅이다 — "지난 실행에서 무슨 일이 있었나"를 로그 파일을 뒤지지 않고
DB 쿼리 한 줄로 답할 수 있어야 한다.

status(run.status, run_source.stopped_reason 등) 값을 ok/partial/failed
중 무엇으로 판정할지는 이 모듈의 책임이 아니다 — 그 판단은 실제 수집을
도는 호출자(T5)의 몫이고, 여기서는 호출자가 건네준 값을 그대로 저장할
뿐이다.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from paper_radar.storage.schema import schema_version

# pyproject.toml [project].name — importlib.metadata 는 설치된 배포판 이름으로
# 찾는다(파이썬 패키지 임포트 이름 "paper_radar" 와 다를 수 있다).
_DISTRIBUTION_NAME = "paper-radar"

# URL 쿼리에서 값을 지울 자격증명으로 보이는 파라미터 이름(대소문자 무관).
# DB 는 백업·디버깅 공유 등으로 사람 손을 타기도 한다 — fetch_log.url 에
# api_key 값이 그대로 남으면 "DB 를 공유한다"가 곧 "키를 유출한다"가 된다.
_CREDENTIAL_PARAM_NAMES = frozenset(
    {"api_key", "apikey", "key", "token", "access_token", "secret", "password"}
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _collector_version() -> str:
    """설치된 paper-radar 배포판 버전. 편집 가능 설치가 아니면 못 찾을 수 있어 그때는 'unknown'."""
    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "unknown"


def scrub_url(url: str) -> str:
    """URL 쿼리에서 자격증명으로 보이는 파라미터를 통째로 제거한다.

    이름 매치는 대소문자를 가리지 않는다. 다른 파라미터의 순서와 값은
    그대로 보존한다 — 지워야 할 것만 지운다.
    """
    parts = urlsplit(url)
    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name.lower() not in _CREDENTIAL_PARAM_NAMES
    ]
    return urlunsplit(parts._replace(query=urlencode(kept)))


class RunLog:
    """run → run_source(소스별 집계) → fetch_log(요청별 로그) 를 기록한다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self, command: str, args: dict) -> str:
        """새 실행을 기록한다. run_id(uuid4 hex) 를 돌려준다."""
        run_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO run"
            " (run_id, started_at, finished_at, status, command, args_json,"
            "  collector_version, schema_version)"
            " VALUES (:run_id, :started_at, NULL, 'running', :command, :args_json,"
            "         :collector_version, :schema_version)",
            {
                "run_id": run_id,
                "started_at": _now(),
                "command": command,
                "args_json": json.dumps(args, ensure_ascii=False),
                "collector_version": _collector_version(),
                "schema_version": schema_version(self._conn),
            },
        )
        self._conn.commit()
        return run_id

    def record_source(
        self,
        run_id: str,
        source: str,
        *,
        requests: int,
        records: int,
        errors: int,
        budget_remaining: int | None = None,
        stopped_reason: str | None = None,
    ) -> None:
        """소스별 집계를 기록(upsert)한다. 같은 run_id+source 재호출은 갱신이다."""
        self._conn.execute(
            "INSERT INTO run_source"
            " (run_id, source, requests, records, errors, budget_remaining, stopped_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(run_id, source) DO UPDATE SET"
            " requests = excluded.requests,"
            " records = excluded.records,"
            " errors = excluded.errors,"
            " budget_remaining = excluded.budget_remaining,"
            " stopped_reason = excluded.stopped_reason",
            (run_id, source, requests, records, errors, budget_remaining, stopped_reason),
        )
        self._conn.commit()

    def finish(self, run_id: str, status: str) -> None:
        """finished_at 을 지금 시각으로 스탬프하고 status 를 기록한다."""
        self._conn.execute(
            "UPDATE run SET finished_at = ?, status = ? WHERE run_id = ?",
            (_now(), status, run_id),
        )
        self._conn.commit()

    def log_fetch(
        self,
        run_id: str,
        *,
        source: str,
        url: str,
        status: int | None,
        attempt: int,
        elapsed_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        """요청 한 건을 기록하고 즉시 커밋한다.

        진행 중인 실행을 다른 연결(예: 모니터링 스크립트)에서 실시간으로
        관찰할 수 있어야 하므로, 다른 메서드들처럼 호출 끝에 커밋하되
        여기서는 배치하지 않고 매 호출마다 커밋한다.
        """
        self._conn.execute(
            "INSERT INTO fetch_log (run_id, at, source, url, status, attempt, elapsed_ms, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, _now(), source, scrub_url(url), status, attempt, elapsed_ms, error),
        )
        self._conn.commit()
