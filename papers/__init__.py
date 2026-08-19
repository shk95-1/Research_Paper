"""논문 근거 수집기.

Windows 콘솔 기본 코드페이지(cp949)에서 한글 로그가 깨지므로 여기서 한 번만 맞춘다.
기존 루트 스크립트들과 같은 방식이다.
"""

import sys

for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
