"""4개 논문 API 공용 HTTP 계층.

여기가 네트워크를 만지는 유일한 지점이다. 소스 모듈은 전부 이 함수를 경유한다.
테스트는 session() 하나만 대체하면 네트워크 없이 전체를 검증할 수 있다.

정책
  1. polite pool: OPENALEX_EMAIL 이 있으면 User-Agent 에 mailto 를 실어 보낸다.
     없으면 경고만 하고 그대로 진행한다. 이메일은 .env 에서만 읽는다.
     2026-02-13부터 OpenAlex 는 mailto/polite pool 자체를 폐지했다 — User-Agent 의
     이메일은 OpenAlex 에는 더 이상 아무 효과가 없다. 다만 Crossref 의 polite 풀에는
     여전히 유효하므로(단건 DOI 10req/s) 이 User-Agent 전송 코드는 그대로 둔다.
  2. 호스트별 최소 간격을 지킨다. 상대 서버가 우리를 차단할 이유를 만들지 않는다.
  3. 429/5xx 는 재시도한다. Retry-After 가 있으면 그 값이 지수 백오프를 이긴다.
     OpenAlex 는 실측에서 Retry-After: 39~40 을 준다. 1/2/4/8 초로는 복구되지 않는다.
  4. 어떤 실패도 예외로 밖에 내보내지 않는다. None 을 돌려주고 호출자가 이어간다.
     논문 한 건의 보강 실패가 100건 수집을 중단시켜서는 안 된다.
  5. OpenAlex 402/409 는 일일 예산(계량제, UTC 자정 초기화) 소진으로 본다.
     재시도해도 자정 전에는 회복되지 않으므로 즉시 포기한다.
"""

import os
import sys
import time
from urllib.parse import urlsplit

import requests

USER_AGENT_BASE = "cosmetics-papers/0.1"

# 호스트별 최소 요청 간격(초)
MIN_INTERVAL = {
    "api.openalex.org": 0.1,
    # 2025-12-01 정책: polite 풀(mailto 제공) 은 단건 DOI 10req/s·동시 3 상한.
    # 0.2s(5req/s) 로 여유를 둔다. 목록/필터 쿼리(현재는 안 씀)는 polite 라도
    # 3req/s 상한이라 0.34s 가 필요하다 — 목록 쿼리를 도입하면 이 값도 갈아야 한다.
    "api.crossref.org": 0.2,
    "www.ebi.ac.uk": 0.2,
    "api.semanticscholar.org": 1.2,  # 키 없을 때 기준. 키가 있으면 아래에서 낮춘다
}
DEFAULT_INTERVAL = 0.5
SEMANTIC_SCHOLAR_HOST = "api.semanticscholar.org"
SEMANTIC_SCHOLAR_KEYED_INTERVAL = 1.0  # 표준 무료 키는 1req/s 상한

MAX_RETRIES = 5
MAX_SLEEP = 60.0
BASE_BACKOFF = 2.0
RETRY_STATUS = {429, 500, 502, 503, 504}
# OpenAlex 2026-02-13부터 계량제. 일일 예산 소진 시 402/409 가 반환되고
# UTC 자정에나 초기화되므로, 이 상태 코드는 재시도해도 무의미하다 — 즉시 포기한다.
BUDGET_STATUS = {402, 409}
BUDGET_LOW_THRESHOLD = 100  # 이 아래로 처음 떨어지면 소진 임박을 한 번만 경고한다

_last_call = {}
_session = None
_budget_remaining = {}  # 호스트 -> x-ratelimit-remaining 최신값
_budget_warned = set()  # 저잔량 경고를 이미 보낸 호스트


def warn(message):
    print(f"[warn] {message}", file=sys.stderr)


def contact_email():
    return os.environ.get("OPENALEX_EMAIL", "").strip()


def user_agent():
    email = contact_email()
    if email:
        return f"{USER_AGENT_BASE} (mailto:{email})"
    return USER_AGENT_BASE


def session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = user_agent()
    return _session


def _interval(host):
    if host == SEMANTIC_SCHOLAR_HOST and os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
        return SEMANTIC_SCHOLAR_KEYED_INTERVAL
    return MIN_INTERVAL.get(host, DEFAULT_INTERVAL)


def _throttle(host):
    last = _last_call.get(host)
    if last is not None:
        wait = _interval(host) - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_call[host] = time.monotonic()


def budget_remaining(host):
    """host 에서 가장 최근에 관측한 x-ratelimit-remaining. 관측 전이면 None."""
    return _budget_remaining.get(host)


def _track_budget(host, response):
    """x-ratelimit-remaining 헤더를 저장하고, 저잔량 진입 시 1회만 경고한다."""
    header = response.headers.get("x-ratelimit-remaining") if response is not None else None
    if header is None:
        return
    try:
        remaining = int(header)
    except TypeError, ValueError:
        return
    _budget_remaining[host] = remaining
    if remaining < BUDGET_LOW_THRESHOLD and host not in _budget_warned:
        _budget_warned.add(host)
        warn(f"{host}: 남은 예산 {remaining} (임계값 {BUDGET_LOW_THRESHOLD} 미만)")


def retry_delay(response, attempt):
    """다음 재시도까지 기다릴 초. Retry-After 헤더가 지수 백오프보다 우선한다."""
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return min(float(header), MAX_SLEEP)
        except TypeError, ValueError:
            pass  # HTTP-date 형식. 지수 백오프로 넘어간다
    return min(BASE_BACKOFF * (2**attempt), MAX_SLEEP)


def get_json(url, params=None, headers=None, timeout=40):
    """성공하면 파싱된 JSON, 최종 실패나 미발견이면 None. 예외를 던지지 않는다."""
    host = urlsplit(url).netloc
    for attempt in range(MAX_RETRIES):
        _throttle(host)
        try:
            response = session().get(url, params=params, headers=headers or {}, timeout=timeout)
        except requests.RequestException as exc:
            warn(f"{host}: 요청 실패 ({exc})")
            time.sleep(retry_delay(None, attempt))
            continue

        _track_budget(host, response)

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                warn(f"{host}: JSON 파싱 실패")
                return None

        if response.status_code == 404:
            return None  # 미발견은 실패가 아니다. 재시도하지 않는다

        if response.status_code in BUDGET_STATUS:
            warn(f"{host}: {response.status_code} — 일일 예산 소진 추정 (UTC 자정 초기화)")
            return None  # 재시도해도 자정 전에는 회복되지 않는다

        if response.status_code in RETRY_STATUS:
            delay = retry_delay(response, attempt)
            warn(
                f"{host}: {response.status_code}, "
                f"{delay:.0f}초 후 재시도 ({attempt + 1}/{MAX_RETRIES})"
            )
            time.sleep(delay)
            continue

        warn(f"{host}: 예상치 못한 상태 {response.status_code}")
        return None

    warn(f"{host}: {MAX_RETRIES}회 재시도 실패, 건너뜁니다")
    return None
