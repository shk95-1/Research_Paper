"""4개 논문 API 공용 HTTP 계층.

여기가 네트워크를 만지는 유일한 지점이다. 소스 모듈은 전부 이 함수를 경유한다.
테스트는 session() 하나만 대체하면 네트워크 없이 전체를 검증할 수 있다.

정책
  1. polite pool: OPENALEX_EMAIL 이 있으면 User-Agent 에 mailto 를 실어 보낸다.
     없으면 경고만 하고 그대로 진행한다. 이메일은 .env 에서만 읽는다.
  2. 호스트별 최소 간격을 지킨다. 상대 서버가 우리를 차단할 이유를 만들지 않는다.
  3. 429/5xx 는 재시도한다. Retry-After 가 있으면 그 값이 지수 백오프를 이긴다.
     OpenAlex 는 실측에서 Retry-After: 39~40 을 준다. 1/2/4/8 초로는 복구되지 않는다.
  4. 어떤 실패도 예외로 밖에 내보내지 않는다. None 을 돌려주고 호출자가 이어간다.
     논문 한 건의 보강 실패가 100건 수집을 중단시켜서는 안 된다.
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
    "api.crossref.org": 0.1,
    "www.ebi.ac.uk": 0.2,
    "api.semanticscholar.org": 1.2,  # 키 없을 때 기준. 키가 있으면 아래에서 낮춘다
}
DEFAULT_INTERVAL = 0.5
SEMANTIC_SCHOLAR_HOST = "api.semanticscholar.org"
SEMANTIC_SCHOLAR_KEYED_INTERVAL = 0.2

MAX_RETRIES = 5
MAX_SLEEP = 60.0
BASE_BACKOFF = 2.0
RETRY_STATUS = {429, 500, 502, 503, 504}

_last_call = {}
_session = None


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


def retry_delay(response, attempt):
    """다음 재시도까지 기다릴 초. Retry-After 헤더가 지수 백오프보다 우선한다."""
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return min(float(header), MAX_SLEEP)
        except (TypeError, ValueError):
            pass  # HTTP-date 형식. 지수 백오프로 넘어간다
    return min(BASE_BACKOFF * (2 ** attempt), MAX_SLEEP)


def get_json(url, params=None, headers=None, timeout=40):
    """성공하면 파싱된 JSON, 최종 실패나 미발견이면 None. 예외를 던지지 않는다."""
    host = urlsplit(url).netloc
    for attempt in range(MAX_RETRIES):
        _throttle(host)
        try:
            response = session().get(
                url, params=params, headers=headers or {}, timeout=timeout
            )
        except requests.RequestException as exc:
            warn(f"{host}: 요청 실패 ({exc})")
            time.sleep(retry_delay(None, attempt))
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                warn(f"{host}: JSON 파싱 실패")
                return None

        if response.status_code == 404:
            return None  # 미발견은 실패가 아니다. 재시도하지 않는다

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
