"""host 별 일일 예산(rate-limit credit) 추적.

Task 1 의 papers/http.py 는 모듈 전역 dict(_budget_remaining, _budget_warned)
로 이를 구현했다 — 그쪽 테스트가 매 테스트마다 `_last_call.clear()` 등으로
수동 초기화해야 했다는 사실 자체가, 전역 상태는 테스트 간 오염을 만든다는
증거다. 여기서는 module-global 싱글턴을 두지 않고, Transport 인스턴스가
소유하는 값 객체로 만든다 — 인스턴스를 새로 만들면 상태도 새로 시작한다.
"""

from __future__ import annotations

from collections.abc import Mapping

from paper_radar.transport import warn

# 이 밑으로 처음 떨어질 때 host 당 1회 경고한다. 예산이 이미 소진된 뒤(0)가
# 아니라 소진되기 "전"에 남은 여유가 있다는 것을 운영자가 미리 알기 위함이다.
WARN_THRESHOLD = 100

# 실측된 OpenAlex 계량제 헤더들 (2026-02 개편). remaining 은 남은 크레딧,
# credits_used 는 이번 요청이 소모한 크레딧, limit 은 일일 총 한도다.
_HEADER_KEYS = {
    "remaining": "x-ratelimit-remaining",
    "credits_used": "x-ratelimit-credits-used",
    "limit": "x-ratelimit-limit",
}


def _lower_lookup(headers: Mapping[str, str]) -> dict[str, str]:
    """헤더 매핑을 소문자 키 dict 로 정규화한다. 대소문자 표기가 서버마다 다르다."""
    return {str(key).lower(): value for key, value in headers.items()}


class BudgetTracker:
    """host 하나짜리 프로세스가 아니라 여러 host 를 동시에 추적하는 값 객체.

    Transport 가 응답을 받을 때마다(오류 포함) observe() 를 호출해 최신 상태를
    갱신한다. remaining()/snapshot() 은 순수 조회다.
    """

    def __init__(self) -> None:
        self._state: dict[str, dict[str, int | None]] = {}
        self._warned: set[str] = set()  # 이미 저잔량 경고를 보낸 host

    def observe(self, host: str, headers: Mapping[str, str]) -> None:
        """헤더에서 예산 관련 값을 파싱해 host 상태를 갱신한다.

        관련 헤더가 하나도 없으면(예: 예산제가 없는 소스) 조용히 아무것도 하지
        않는다 — 모든 소스가 이 헤더들을 주는 것은 아니다.
        """
        lowered = _lower_lookup(headers)
        parsed = {}
        for field_name, header_name in _HEADER_KEYS.items():
            raw = lowered.get(header_name)
            if raw is None:
                continue
            try:
                parsed[field_name] = int(raw)
            except (TypeError, ValueError):
                continue  # 파싱 불가능한 값은 무시한다 — 크래시할 이유가 없다

        if not parsed:
            return

        state = self._state.setdefault(
            host, {"remaining": None, "credits_used": None, "limit": None}
        )
        state.update(parsed)

        remaining = state.get("remaining")
        if remaining is not None and remaining < WARN_THRESHOLD and host not in self._warned:
            self._warned.add(host)
            warn(f"{host}: 남은 예산 {remaining} (임계값 {WARN_THRESHOLD} 미만)")

    def remaining(self, host: str) -> int | None:
        """host 에서 가장 최근에 관측한 남은 예산. 관측 전이면 None."""
        state = self._state.get(host)
        if state is None:
            return None
        return state.get("remaining")

    def snapshot(self) -> dict:
        """수집 종료 시 기록용 전체 상태 사본 {host: {remaining, credits_used, limit}}."""
        return {host: dict(values) for host, values in self._state.items()}
