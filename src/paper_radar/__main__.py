"""`python -m paper_radar` 진입점.

[project.scripts] 로 설치되는 `paper-radar` 콘솔 스크립트와 완전히 동일하게
cli.main() 을 부른다 — 콘솔 스크립트가 PATH 에 없는 환경(예: uv run 없이
PYTHONPATH 로만 얹은 경우)에서도 `python -m paper_radar ...` 로 똑같이 쓸 수
있게 하는 표준 관례다.

이 모듈만 paper_radar.cli 를 import 할 수 있다 — 나머지 모든 모듈은 cli 를
몰라야 한다(계층 가드, tests/paper_radar/test_guards.py 참고). cli 는 여러
계층(evidence/trend/storage)을 한데 묶는 조립 지점이라, 다른 계층이 거꾸로
cli 를 알면 순환 의존이 생긴다.
"""

from __future__ import annotations

import sys

from paper_radar.cli import main

if __name__ == "__main__":
    sys.exit(main())
