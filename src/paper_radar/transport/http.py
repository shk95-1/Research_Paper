"""소스 전용 통합 HTTP 계층 — 네트워크를 만지는 유일한 지점.

Task 1 이 확정한 papers/http.py 의 정책(페이스·재시도·인증·User-Agent)을
그대로 계승하되, 실패를 None 으로 뭉개던 것을 타입 있는 예외로 바꾼다.
transport 는 사이트(소스)를 모른다 — 알고 있는 것은 SourcePolicy 뿐이다.

동시성 없음: 순차 + 페이스(host 별 최소 간격) + 예산 상한이 전부다 — 사용자가
처리량을 비목표로 명시했다.

Task 1 이 확정한 소스별 정책값 (T5 에서 각 소스 모듈이 SourcePolicy 로 선언한다.
여기서는 이식 시 참고할 근거만 주석으로 남겨 둔다):
  - api.openalex.org: 0.1s (구속 조건은 인터벌이 아니라 일일 크레딧).
    2026-02 계량제 개편으로 mailto 파라미터는 더 이상 보내지 않는다.
    auth: param "api_key" ← OPENALEX_API_KEY.
  - api.crossref.org: 0.2s (2025-12 polite 풀 단건 DOI 10req/s 상한의 절반).
    mailto param ← OPENALEX_EMAIL 은 Crossref 에는 여전히 유효하다.
  - api.semanticscholar.org: 키 있으면 1.0s(표준 키 1req/s), 없으면 4.0s(익명
    풀 포화). auth: header "x-api-key" ← SEMANTIC_SCHOLAR_API_KEY.
  - www.ebi.ac.uk: 0.2s.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import requests

from paper_radar.contract import Fetch, Payload, SourcePolicy
from paper_radar.transport import warn
from paper_radar.transport.budget import BudgetTracker
from paper_radar.transport.errors import (
    BudgetExhausted,
    NotFound,
    PermanentError,
    RateLimited,
    TransientError,
)

USER_AGENT_BASE = "paper-radar/0.1"

# 429/5xx 는 재시도 대상. OpenAlex 실측 Retry-After 39~40초를 감안해 지수
# 백오프 상한(MAX_SLEEP)을 그보다 넉넉히 잡는다.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# OpenAlex 2026-02 계량제: 일일 예산 소진 시 402/409. UTC 자정에나 초기화되므로
# 재시도해도 무의미하다 — 즉시 BudgetExhausted 로 포기한다 (재시도 없음).
BUDGET_STATUS = frozenset({402, 409})

BASE_BACKOFF = 1.5  # 지수 백오프 밑변. 1.5*2**attempt: 1.5/3/6/12/24...초
MAX_SLEEP = 60.0  # 백오프·Retry-After 상한. 이보다 길게 기다리면 배치가 멎는다


def _utcnow() -> datetime:
    """실제 UTC 벽시계. Retry-After 의 HTTP-date 형을 해석할 때만 쓴다 — 페이스
    계산은 주입 가능한 clock(monotonic)을 쓰므로 테스트가 실제로 기다리지
    않는다. 이 함수만 mock.patch 로 갈아끼우면 HTTP-date 케이스도 결정적으로
    테스트할 수 있다.
    """
    return datetime.now(UTC)


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """대소문자 구분 없이 헤더 값을 찾는다. 서버마다 표기가 다르다."""
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return value
    return None


def _normalize_headers(headers: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """응답 헤더를 소문자 키 튜플 쌍으로 정규화한다 (Payload.header() 의 전제)."""
    return tuple((str(key).lower(), str(value)) for key, value in headers.items())


def _capped_backoff(attempt: int) -> float:
    return min(BASE_BACKOFF * (2**attempt), MAX_SLEEP)


def _parse_retry_after(value: str) -> float | None:
    """Retry-After 헤더 값을 초로 해석한다. 숫자형과 HTTP-date 형 둘 다 지원한다.

    기존 papers/http.py 는 HTTP-date 형을 파싱하지 못해 버렸다 — OpenAlex 는
    실측상 숫자형만 보내 문제가 드러나지 않았을 뿐, 표준(RFC 9110)은 두 형식을
    모두 허용하므로 언젠가 HTTP-date 를 보내는 소스를 만나면 조용히 백오프로
    새 버렸을 것이다. 여기서는 email.utils.parsedate_to_datetime 으로 처리한다.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max((parsed - _utcnow()).total_seconds(), 0.0)


def _retry_delay(response, attempt: int) -> float:
    """다음 재시도까지 기다릴 초. Retry-After 가 있으면 지수 백오프보다 우선한다."""
    header = _header_value(response.headers, "retry-after")
    if header:
        seconds = _parse_retry_after(header)
        if seconds is not None:
            return min(seconds, MAX_SLEEP)
    return _capped_backoff(attempt)


class Transport:
    """소스가 네트워크를 만지지 않고도 데이터를 받도록 하는 유일한 통로.

    session/clock/sleep 을 전부 주입 가능하게 열어 둔 이유는 테스트가 실제
    네트워크와 실제 대기 없이 페이스·재시도·백오프를 검증할 수 있어야 하기
    때문이다 (FakeSession + fake clock/sleep).
    """

    def __init__(self, session=None, clock=time.monotonic, sleep=time.sleep):
        self._session = session if session is not None else requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._last_call: dict[str, float] = {}  # host -> 마지막 호출 시각(clock 단위)
        self.budget = BudgetTracker()  # host 별 예산 추적. 이 인스턴스가 소유한다

    def _user_agent(self) -> str:
        email = os.environ.get("OPENALEX_EMAIL", "").strip()
        if email:
            return f"{USER_AGENT_BASE} (mailto:{email})"
        return USER_AGENT_BASE

    def _throttle(self, host: str, policy: SourcePolicy) -> None:
        last = self._last_call.get(host)
        if last is not None:
            wait = policy.min_interval_s - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_call[host] = self._clock()

    def _inject_auth(
        self, policy: SourcePolicy, headers: dict[str, str], params: dict[str, str]
    ) -> None:
        if policy.auth_kind is None:
            return
        value = os.environ.get(policy.auth_env, "").strip()
        if not value:
            return  # 키 없이도 동작해야 한다 — 조용히 생략
        if policy.auth_kind == "param":
            params[policy.auth_name] = value
        elif policy.auth_kind == "header":
            headers[policy.auth_name] = value

    def request(self, fetch: Fetch, policy: SourcePolicy) -> Payload:
        """fetch 를 policy 에 따라 (페이스·인증·재시도 적용해) 실행하고 Payload 를 돌려준다.

        실패는 전부 transport.errors 의 타입 있는 예외로 던진다 — None 을
        돌려주지 않는다.
        """
        host = policy.host
        for attempt in range(policy.max_attempts):
            self._throttle(host, policy)

            headers = dict(fetch.headers)
            params = dict(fetch.params)
            self._inject_auth(policy, headers, params)
            headers.setdefault("User-Agent", self._user_agent())

            start = self._clock()
            try:
                response = self._session.request(
                    fetch.method,
                    fetch.url,
                    params=params,
                    headers=headers,
                    data=fetch.json_body,
                    timeout=policy.timeout_s,
                )
            except requests.RequestException as exc:
                warn(f"{host}: 요청 실패 ({exc})")
                if attempt + 1 >= policy.max_attempts:
                    raise TransientError(f"{host}: 연결 실패, 재시도 소진 ({exc})") from exc
                self._sleep(_capped_backoff(attempt))
                continue

            elapsed_ms = int((self._clock() - start) * 1000)
            self.budget.observe(host, response.headers)
            status = response.status_code

            if status == 200:
                return Payload(
                    fetch=fetch,
                    status=200,
                    body=response.content,
                    headers=_normalize_headers(response.headers),
                    elapsed_ms=elapsed_ms,
                    captured_at=_utcnow().isoformat(),
                )

            if status == 404:
                raise NotFound(f"{host}: 404 Not Found — 그 레코드는 없다", status=404)

            if status in BUDGET_STATUS:
                # 재시도 없이 즉시 포기한다 — 일일 예산은 재시도로 회복되지 않는다
                raise BudgetExhausted(
                    f"{host}: {status} — 일일 예산 소진 추정 (UTC 자정 초기화)", status=status
                )

            if status in RETRY_STATUS:
                if attempt + 1 >= policy.max_attempts:
                    if status == 429:
                        raise RateLimited(f"{host}: 429 재시도 소진", status=status)
                    raise TransientError(f"{host}: {status} 재시도 소진", status=status)
                delay = _retry_delay(response, attempt)
                warn(
                    f"{host}: {status}, {delay:.0f}초 후 재시도 "
                    f"({attempt + 1}/{policy.max_attempts})"
                )
                self._sleep(delay)
                continue

            # 404/BUDGET_STATUS/RETRY_STATUS 가 아닌 그 외 전부(다른 4xx 포함).
            # 요청 자체가 잘못됐다는 뜻이므로 재시도하지 않는다.
            raise PermanentError(f"{host}: 예상치 못한 상태 {status}", status=status)

        # 위 루프의 매 반복은 return/raise/continue 로 끝나고, 마지막 반복에서는
        # continue 가 나오지 않도록 되어 있어 실제로는 도달하지 않는다. 방어용.
        raise TransientError(f"{host}: 재시도 소진")

    def get_json(self, url: str, params: Iterable[tuple[str, str]] = (), *, policy: SourcePolicy):
        """request() + json_data() 편의 메서드. dict 나 list 를 돌려준다."""
        fetch = Fetch(url=url, method="GET", params=tuple(params))
        payload = self.request(fetch, policy)
        return payload.json_data()
