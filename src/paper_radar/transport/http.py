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
from collections.abc import Callable, Iterable, Mapping
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

    observer: 매 시도(attempt)마다 정확히 1회 호출되는 관측 훅
    (fetch, status, attempt, elapsed_ms, error_str) — status=None 은 전송
    실패(연결 오류)를, error_str 은 그 경우의 예외 메시지를 나타낸다. T5b 가
    이 훅으로 fetch_log 를 기록한다. observer 자신이 던진 예외는 삼키고 warn
    한 줄만 남긴다 — 관측 코드의 버그가 수집 자체를 죽여서는 안 되기 때문이다.
    """

    def __init__(
        self,
        session=None,
        clock=time.monotonic,
        sleep=time.sleep,
        observer: Callable[[Fetch, int | None, int, int, str | None], None] | None = None,
    ):
        self._session = session if session is not None else requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._observer = observer
        self._last_call: dict[str, float] = {}  # host -> 마지막 호출 시각(clock 단위)
        self.budget = BudgetTracker()  # host 별 예산 추적. 이 인스턴스가 소유한다

    def set_observer(
        self, observer: Callable[[Fetch, int | None, int, int, str | None], None] | None
    ) -> Callable[[Fetch, int | None, int, int, str | None], None] | None:
        """observer 훅을 생성 후에 (재)설정하고, "이전" observer 를 돌려준다.

        T5b 의 collect() 는 RunLog.start() 로 run_id 를 발급받은 "뒤"에야 그
        run_id 를 캡처하는 observer 콜백을 만들 수 있는데, Transport 자체는
        보통 그보다 먼저(호출자가 collect() 를 부르기 전에) 만들어진다. 그래서
        생성자의 observer= 만으로는 이 순서를 맞출 수 없어, 나중에 설정할 수
        있는 통로를 열어 둔다.

        반환값이 이전 observer 인 이유: Transport 인스턴스가 재사용될 수
        있다(예: 같은 CLI 프로세스가 collect() 를 두 번 부르거나, 호출자가
        이미 자기 observer 를 걸어 둔 Transport 를 넘기는 경우). 호출자가
        되돌려 줄 값 없이 그냥 None 으로 밀어버리면, collect() 가 끝난 뒤에도
        원래 있던(또는 없던) observer 상태를 복원할 방법이 없어 다음
        요청부터 죽은 run_id 로 fetch_log 가 계속 쌓이거나, 바깥 호출자가
        걸어 둔 observer 가 조용히 사라진다. `previous =
        transport.set_observer(new); ...; transport.set_observer(previous)`
        패턴으로 안전하게 되돌릴 수 있어야 한다.
        """
        previous = self._observer
        self._observer = observer
        return previous

    def _notify(
        self, fetch: Fetch, status: int | None, attempt: int, elapsed_ms: int, error: str | None
    ) -> None:
        """observer 훅을 호출한다. observer 가 예외를 던져도 삼키고 warn 한 줄만
        남긴다 — 관측(로깅 등)의 버그 때문에 수집 전체가 죽으면 안 된다."""
        if self._observer is None:
            return
        try:
            self._observer(fetch, status, attempt, elapsed_ms, error)
        except Exception as exc:
            warn(f"observer 콜백 실패, 무시하고 계속한다: {exc}")

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
                elapsed_ms = int((self._clock() - start) * 1000)
                self._notify(fetch, None, attempt, elapsed_ms, str(exc))
                warn(f"{host}: 요청 실패 ({exc})")
                if attempt + 1 >= policy.max_attempts:
                    raise TransientError(f"{host}: 연결 실패, 재시도 소진 ({exc})") from exc
                self._sleep(_capped_backoff(attempt))
                continue

            elapsed_ms = int((self._clock() - start) * 1000)
            self.budget.observe(host, response.headers)
            status = response.status_code
            self._notify(fetch, status, attempt, elapsed_ms, None)

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

            if status >= 500:
                # RETRY_STATUS 에 없는 5xx (501/505/511 등). 재시도 목록에 없다고 해서
                # "요청 자체가 잘못됐다"(PermanentError)로 보내면 일시적 서버 장애를
                # 영구 실패로 오분류해 호출자가 향후 재시도 기회 자체를 잃는다 — 서버측
                # 오류라는 사실은 RETRY_STATUS 여부와 무관하다. 재시도는 하지 않되(이
                # 코드가 알려진 재시도 대상이 아니므로) 타입은 TransientError 로 던진다.
                raise TransientError(
                    f"{host}: {status} (재시도 대상 외 5xx, 즉시 포기)", status=status
                )

            # 404/BUDGET_STATUS/RETRY_STATUS/5xx 가 아닌 그 외(다른 4xx, 예상 밖 3xx 등).
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
