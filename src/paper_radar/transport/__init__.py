"""통합 HTTP 계층. 소스를 모른다 — Transport/BudgetTracker/오류 타입만 안다.

warn() 은 이 계층 전체(http.py, budget.py)가 공유하는 단 하나의 출력 통로다.
절대 print 하지 않는다 — stdout 은 파이프라인 산출물(csv/json 등)을 위해
비워 둔다. 경고는 stderr 한 줄로만 흘려보낸다 (기존 papers/http.py 관례 계승).
"""

from __future__ import annotations

import sys


def warn(message: str) -> None:
    print(f"[warn] {message}", file=sys.stderr)
