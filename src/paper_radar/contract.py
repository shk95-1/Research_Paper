"""소스 플러그인 계약(contract) — 소스가 알아야 할 전부가 여기 있다.

trend-radar 교본의 원칙을 그대로 따른다: transport 는 사이트(소스)를 모르고,
소스는 네트워크를 모른다. 이 파일은 그 경계에 앉아 있는 값 타입들의 집합이며,
전부 stdlib 만 사용한다 — 소스 모듈이 이 계약만 보고 구현할 수 있어야 한다.

- SourcePolicy: 소스가 선언하는 페이스/재시도/인증 정책.
- Fetch: transport 에 보낼 요청 명세. 해시 가능해야 한다 — frontier(수집 대상
  큐)에서 set 으로 중복 제거하는 것이 전제이기 때문이다.
- Payload: transport 가 돌려주는 원시 응답.
- Yield: 소스의 parse() 가 돌려주는 결과 — 레코드와 후속 요청.
- Source: 소스가 구현해야 하는 최소 표면. seeds/parse 시그니처는 T5(소스 이식)
  에서 확정하고, 이 태스크에서는 key/policy 존재만 계약으로 요구한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from paper_radar.transport.errors import ParseError


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """소스 하나가 지켜야 할 페이스·재시도·인증 정책.

    host 가 페이스와 예산 추적의 키다 — URL 을 파싱해서 얻지 않고 정책이
    명시적으로 선언한다 (같은 호스트라도 소스마다 다른 페이스를 줄 수 있게).
    """

    host: str
    min_interval_s: float
    timeout_s: float = 40.0
    # 429/5xx 재시도 상한. Task 1 실측: OpenAlex 는 Retry-After 39~40초를 주므로
    # 상한이 너무 낮으면 복구 전에 포기하게 된다.
    max_attempts: int = 5
    auth_env: str | None = None  # 자격증명을 담은 환경변수 "이름" (값 자체가 아님)
    auth_kind: str | None = None  # "param" | "header" | None
    auth_name: str | None = None  # 파라미터/헤더 이름. 예: "api_key", "x-api-key"

    def __post_init__(self) -> None:
        if self.min_interval_s < 0:
            # 음수 간격은 "과거로 sleep" 이 되어 페이스 제한이 무의미해진다
            raise ValueError(f"min_interval_s 는 0 이상이어야 한다: {self.min_interval_s}")
        if self.max_attempts < 1:
            # 0 이면 한 번도 시도하지 않아 항상 실패로 끝난다
            raise ValueError(f"max_attempts 는 1 이상이어야 한다: {self.max_attempts}")
        if self.auth_kind is not None and (self.auth_env is None or self.auth_name is None):
            # auth_kind 만 있고 env/name 이 없으면 transport 가 어디서 값을 읽어
            # 어디에 넣을지 알 수 없다 — 셋은 하나의 세트로만 의미가 있다
            raise ValueError(
                "auth_kind 를 지정했으면 auth_env 와 auth_name 도 함께 지정해야 한다"
            )


@dataclass(frozen=True, slots=True)
class Fetch:
    """transport 에 보낼 요청 한 건의 명세.

    dict 대신 튜플 쌍(tuple[tuple[str, str], ...])을 쓰는 이유는 해시 가능성이다
    — frontier 가 이미 보낸 Fetch 를 set 으로 중복 제거하려면 dict 필드가 있는
    순간 불가능해진다.
    """

    url: str
    method: str = "GET"  # "GET" | "POST"
    params: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    json_body: str | None = None  # POST 용. 직렬화된 JSON 문자열 (dict 는 해시 불가)
    context: tuple[tuple[str, str], ...] = ()  # 소스가 parse 때 되찾을 자유 필드

    def ctx(self, name: str, default: str | None = None) -> str | None:
        """context 튜플에서 name 에 해당하는 값을 찾는다. 없으면 default."""
        for key, value in self.context:
            if key == name:
                return value
        return default


@dataclass(frozen=True, slots=True)
class Payload:
    """transport 가 돌려주는 원시 응답. 파싱은 소스가 아니라 여기서 편의로 제공한다."""

    fetch: Fetch
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...]  # 응답 헤더 (소문자 키로 정규화됨)
    elapsed_ms: int
    captured_at: str  # ISO UTC. transport 가 스탬프한다 (소스가 만들지 않는다)

    def text(self) -> str:
        """body 를 UTF-8 문자열로. 깨진 바이트는 버리지 않고 치환한다."""
        return self.body.decode("utf-8", errors="replace")

    def json_data(self):
        """body 를 JSON 으로 파싱. 실패하면 None 이 아니라 ParseError 를 던진다 —
        200 인데 JSON 이 아니라는 것은 업스트림 형태가 바뀌었다는 신호이지,
        조용히 넘길 정상 상태가 아니다.
        """
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ParseError(f"JSON 파싱 실패: {exc}", status=self.status) from exc

    def header(self, name: str, default: str | None = None) -> str | None:
        """응답 헤더 조회. name 은 대소문자 무관 — 저장 시 소문자로 정규화되어 있다."""
        lowered = name.lower()
        for key, value in self.headers:
            if key == lowered:
                return value
        return default


@dataclass(frozen=True, slots=True)
class Yield:
    """소스의 parse() 가 돌려주는 결과 — 레코드와 후속 요청(커서 페이지네이션 등)."""

    records: tuple
    follow: tuple[Fetch, ...] = ()


@runtime_checkable
class Source(Protocol):
    """소스 모듈이 구현해야 하는 최소 표면.

    seeds()/parse() 의 정확한 시그니처는 T5(기존 소스 이식)에서 실제 이식과
    함께 확정한다 — 지금 추측해서 못박으면 이식할 때 다시 뜯어야 할 위험이
    크다. 이 태스크에서는 registry 가 검증할 수 있는 최소치, key 와 policy
    존재만 계약으로 못박는다.
    """

    key: ClassVar[str]
    policy: ClassVar[SourcePolicy]
