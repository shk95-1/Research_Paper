"""소스 레지스트리 — trend-radar 교본의 레지스트리를 축소 이식한 것.

소스 모듈은 @register 를 붙이는 것만으로 등록된다. import 시점에 계약
위반(key 비어있음/중복, policy 가 SourcePolicy 가 아님)을 즉시 raise 해서,
잘못 정의된 소스가 수집 실행 중간에야 발견되는 일을 막는다 — "fail fast".
"""

from __future__ import annotations

from paper_radar.contract import SourcePolicy

SOURCES: dict[str, type] = {}


def register(source_cls: type) -> type:
    """소스 클래스를 SOURCES 에 등록한다. key/policy 계약을 여기서 검증한다."""
    key = getattr(source_cls, "key", None)
    if not isinstance(key, str) or not key:
        # 빈/누락된 key 는 registry 도 CLI 도 이 소스를 가리킬 방법이 없다
        raise ValueError(f"{source_cls!r}: key 는 비어있지 않은 str 이어야 한다 (받은 값: {key!r})")
    if key in SOURCES:
        # 조용히 덮어쓰면 나중에 등록된 쪽이 이긴다 — 어느 소스가 실제로 동작
        # 중인지 알 수 없게 된다
        raise ValueError(f"key 중복: {key!r} 는 이미 {SOURCES[key]!r} 로 등록되어 있다")

    policy = getattr(source_cls, "policy", None)
    if not isinstance(policy, SourcePolicy):
        raise ValueError(
            f"{source_cls!r} (key={key!r}): policy 는 SourcePolicy 인스턴스여야 한다 "
            f"(받은 값: {policy!r})"
        )

    SOURCES[key] = source_cls
    return source_cls


def get_source(key: str) -> type:
    """key 로 등록된 소스 클래스를 찾는다. 모르는 key 면 등록된 키 목록을 담아 KeyError."""
    try:
        return SOURCES[key]
    except KeyError:
        known = ", ".join(sorted(SOURCES)) or "(없음)"
        raise KeyError(f"알 수 없는 소스 key: {key!r} (등록된 키: {known})") from None
