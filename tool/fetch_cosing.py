"""CosIng 벌크 CSV 다운로드 — 일회성, 분기 갱신 시 사람이 다시 실행한다.

EU 화장품 성분 DB(CosIng)는 API 가 없고 European Commission
cosmetic-ingredient-database 페이지의 다운로드 링크(CSV/Excel 내보내기)뿐이다
— 계획의 "용량은 크지만 일회성이고 수가 매우 적은 경우는 다운로드" 조항의
적용 사례(Task 13 브리핑). 링크 주소와 파일 형식이 바뀌곤 하므로 URL 을
코드에 박지 않는다 — 실행 인자로 받고, 실제로 받은 것이 무엇인지(URL,
체크섬, 크기, 헤더 행, 받은 시각)를 _meta.json 에 기록해 재현 가능하게 한다.

이 스크립트는 `uv run pytest`(testpaths=tests/) 가 전혀 건드리지 않는다 —
tool/ 아래 있고 파일명도 test*.py 가 아니다(tool/live_smoke.py 와 같은
배치). 이번 태스크(T13)에서는 이 파일을 실행하지 않는다 — 코드 리뷰와
아래 순수 함수(sha256_of/header_row_of/build_meta)에 대한 단위 테스트로만
검증한다. 실제 다운로드는 사람이(또는 후속 태스크가) 직접 실행한다:

    uv run python tool/fetch_cosing.py --url <CosIng CSV 다운로드 URL>

이미 받아 둔 파일이 있으면(분기 갱신 재실행), 덮어쓰기 전에 기존 _meta.json
을 _meta.prev.json 으로 보존한다 — 무엇이 바뀌었는지(URL, 체크섬, 크기)
새 _meta.json 과 diff 로 비교할 수 있게 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import requests

# data/ 아래 — .gitignore 에 이미 있는 항목이라 커밋되지 않는다(수만 행, 수
# MB 라 브리핑이 gitignore 대상으로 지정).
DEFAULT_OUT_DIR = "data/reference/cosing"
CSV_FILENAME = "cosing.csv"
META_FILENAME = "_meta.json"  # src/paper_radar/ingredients/import_cosing.py 가 같은 이름을 찾는다
PREV_META_FILENAME = "_meta.prev.json"

# 다운로드 요청 타임아웃(초). CosIng 내보내기 파일은 수 MB 라 넉넉히 잡는다.
_TIMEOUT_S = 60


def sha256_of(path: Path) -> str:
    """파일 내용의 sha256 hex digest. 순수 함수(디스크 읽기만, 네트워크 없음).

    청크 단위로 읽는다 — 파일 전체를 한 번에 메모리에 올리지 않는다(수 MB
    라 지금은 문제 없지만, CosIng 배포 파일이 더 커져도 그대로 안전하다).
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def header_row_of(path: Path) -> str:
    """CSV 첫 줄(헤더)을 그대로 읽어 온다. 순수 함수(디스크 읽기만).

    utf-8-sig 로 연다 — 엑셀 내보내기가 흔히 UTF-8 BOM 을 붙이는데, BOM 이
    남으면 헤더 원문을 사람이 비교할 때 눈에 보이지 않는 문자가 섞여
    헷갈린다. 이 값은 reference/cosing.py 가 실제로 파싱에 쓰는 컬럼
    이름(_COL_INCI_NAME 등)과 실파일 헤더가 일치하는지 사람이 확인하는
    용도다 — 배포처가 컬럼명을 조용히 바꾸는 드리프트를 잡기 위함이다.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return handle.readline().rstrip("\r\n")


def build_meta(*, url: str, csv_path: Path, fetched_at: str) -> dict:
    """_meta.json 에 쓸 내용을 구성한다. 순수 함수 — sha256/size/header 계산만, 네트워크 없음.

    fetched_at 은 호출자(fetch())가 넘긴다 — 이 함수 자체는 "지금 몇 시인지"
    를 몰라야 테스트에서 시각을 고정해 결정론적으로 검증할 수 있다.
    """
    return {
        "url": url,
        "sha256": sha256_of(csv_path),
        "size_bytes": csv_path.stat().st_size,
        "fetched_at": fetched_at,
        "header_row": header_row_of(csv_path),
    }


def fetch(url: str, out_dir: str = DEFAULT_OUT_DIR) -> dict:
    """CosIng CSV 를 내려받아 저장하고 _meta.json 을 기록한다.

    네트워크 함수 — 이 태스크에서 테스트하지 않는다(브리핑 지시: "pytest
    미발견 경로"). 순수 부분(sha256_of/header_row_of/build_meta)만 단위
    테스트한다.

    이미 _meta.json 이 있으면(재실행 = 분기 갱신) 덮어쓰기 전에
    _meta.prev.json 으로 보존한다 — 이전 실행의 URL/체크섬/크기가 사라지지
    않아, 이번 갱신에서 무엇이 바뀌었는지 diff 로 볼 수 있다.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    csv_path = out_path / CSV_FILENAME
    meta_path = out_path / META_FILENAME
    prev_meta_path = out_path / PREV_META_FILENAME

    if meta_path.exists():
        shutil.copyfile(meta_path, prev_meta_path)

    response = requests.get(url, timeout=_TIMEOUT_S)
    response.raise_for_status()
    csv_path.write_bytes(response.content)

    fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = build_meta(url=url, csv_path=csv_path, fetched_at=fetched_at)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CosIng 벌크 CSV 를 내려받는다 (일회성, 분기 갱신 시 재실행)."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="CosIng CSV 내보내기 다운로드 URL (배포처가 바뀔 수 있어 인자로 받는다)",
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUT_DIR, help=f"저장 디렉터리 (기본값: {DEFAULT_OUT_DIR})"
    )
    args = parser.parse_args(argv)

    meta = fetch(args.url, args.out)
    print(f"저장 완료: {Path(args.out) / CSV_FILENAME}")
    print(f"  size_bytes: {meta['size_bytes']:,}")
    print(f"  sha256: {meta['sha256']}")
    print(f"  fetched_at: {meta['fetched_at']}")
    print(f"  header_row: {meta['header_row']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
