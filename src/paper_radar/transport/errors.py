"""transport 오류 타입 — 오류는 타입이다. None 과적 금지.

기존 papers/http.py 는 모든 실패를 None 으로 뭉갰다 (그것도 "논문 한 건의
실패가 전체 수집을 막아서는 안 된다"는 정당한 이유가 있었다). 하지만 그러면
"이 DOI 는 그냥 없다"와 "예산이 다 떨어져서 오늘은 더 못 부른다"를 호출자가
구분할 수 없다. 404 와 재시도 소진과 예산 소진은 서로 다른 예외로 던지고,
호출자가 무엇을 해야 하는지는 예외 종류로 결정한다.
"""

from __future__ import annotations


class TransportError(Exception):
    """모든 transport 오류의 기반 클래스.

    호출자 조치: 구체 서브클래스로 잡아 처리하고, 알 수 없는 실패는 이 기반
    클래스로 잡아 수집 항목 하나를 건너뛰는 안전망으로 쓴다.
    """

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class NotFound(TransportError):
    """404. 호출자 조치: 그 레코드만 정상적으로 건너뛴다 — "없다"는 결과의 하나다."""


class TransientError(TransportError):
    """5xx 재시도 소진, 연결 실패, 타임아웃. 호출자 조치: 이 요청만 실패로 기록하고
    나머지 수집은 계속한다 — 다음 실행에서 재시도될 수 있다.
    """


class RateLimited(TransportError):
    """429 재시도 소진 (재시도 자체는 transport 내부에서 이미 수행했다).
    호출자 조치: 이 요청은 포기하고, 필요하면 이 host 의 페이스를 늘리는 것을 고려한다.
    """


class BudgetExhausted(TransportError):
    """402/409 — 일일 예산 소진 (예: OpenAlex 계량제, UTC 자정 초기화).
    호출자 조치: 재시도는 무의미하다 — 이 host 를 쓰는 수집 전체를 깨끗하게 중단한다.
    """


class PermanentError(TransportError):
    """404/402/409 를 제외한 그 외 4xx. 요청 자체가 잘못됐다는 뜻.
    호출자 조치: 재시도하지 말고, 요청을 만든 소스 코드의 버그로 취급해 조사한다.
    """


class ParseError(TransportError):
    """200 인데 JSON 이 아님 — 업스트림 응답 형태가 바뀌었다는 신호.
    호출자 조치: 이 페이로드는 버리고, 반복되면 소스 파서를 업스트림 변경에 맞춰 갱신한다.
    """
