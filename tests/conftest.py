"""pytest 전역 설정 — 소켓 금지 가드.

계획의 제약: `uv run pytest` 기본 실행은 네트워크를 쓰지 않는다. 소스 테스트는
전부 FakeSession/모킹된 transport 로 결정적으로 돌아가야 하는데, 실수로 그
모킹을 빼먹으면(예: mock.patch 대상을 잘못 짚음) 테스트가 진짜 소켓을 열어
버릴 수 있다 — 로컬에서는 느려지거나 방화벽에 막혀 타임아웃으로만 보이고,
CI 에서는 network egress 자체가 없어 원인을 알기 어려운 hang 이 된다.

이 autouse fixture 는 모든 테스트에 대해 소켓 연결 시도 자체를 차단하고,
어떤 테스트가 시도했는지 즉시 알 수 있게 예외 메시지에 test id 를 담는다.
가드 자체가 실제로 막는지는 tests/paper_radar/test_guards.py::SocketGuardTest
가 검증한다.

tool/live_smoke.py 는 pytest 가 discover 하지 않는 별도 스크립트(testpaths=
tests/ 바깥, 파일명도 test*.py 아님)라 이 가드의 영향을 받지 않는다 — 그
스크립트를 직접 실행할 때만 진짜 네트워크를 쓴다.
"""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def _forbid_real_sockets(request, monkeypatch):
    """모든 테스트에서 socket.socket.connect/connect_ex, socket.create_connection 을
    막는다. 호출되면 어느 테스트였는지 담은 RuntimeError 를 던진다."""

    test_id = request.node.nodeid

    def _blocked(*_args, **_kwargs):
        raise RuntimeError(f"소켓 연결이 테스트 중 시도됐다 (금지): {test_id}")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield
