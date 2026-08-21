"""tests/fixtures/trend_golden/ 재생성 스크립트 — 구 papers_trend 코드로 돌린다.

Task 6 의 순서 제약: 골든은 papers_trend/ 가 아직 코드로 살아 있는 동안,
git mv(config.json/keyword_lexicon.json/stopwords.json 을 src/paper_radar/trend/
로 옮기기) **이전에** 이 스크립트로 한 번 생성해서 커밋해 둔다. 이후
tests/paper_radar/test_trend_golden.py 가 신 파이프라인(paper_radar.trend)을
같은 합성 픽스처(tests/fixtures/trend_synthetic/)에 돌려 이 골든과 바이트
동일한지 비교한다 — 골든 자체는 다시 실행해서 만들지 않는다(구 코드가 사라진
뒤에는 이 스크립트를 실행할 수 없다).

재생성이 필요한 경우(합성 픽스처 자체를 바꿀 때만, CSV 표면을 바꿀 때는
아니다 — CSV 컬럼/값/인코딩은 공개 표면이라 절대 바뀌면 안 된다):
    이 스크립트는 papers_trend/ 가 삭제된 뒤(T7)에는 동작하지 않는다. 그
    시점에 픽스처를 바꿔야 한다면, 신 코드(paper_radar.trend)가 이미 골든과
    동일함이 증명된 상태이므로 신 코드로 대신 돌려 골든을 새로 만들고, 그
    직후 test_trend_golden.py 가 신 코드 자기 자신과 비교하는 형태가 되어
    회귀 감지력을 잃는다는 점을 리뷰에서 반드시 짚을 것 — 이 상황 자체를
    피하는 것이 최선이다(픽스처는 잘 안 바뀌게 설계했다).

    python -m papers_trend 가 아직 동작하는 워크트리에서:
        uv run python tests/fixtures/make_trend_golden.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from papers_trend import aggregate, normalize, records, unmatched  # noqa: E402

FIXTURE_ROOT = Path(__file__).resolve().parent / "trend_synthetic"
GOLDEN_DIR = Path(__file__).resolve().parent / "trend_golden"
QUERY_ID = "synthetic"

# tests/fixtures/trend_synthetic/config.json 과 값을 반드시 맞춘다(리터럴로
# 여기 박아 두는 이유: 구 aggregate.run() 은 config dict 를 인자로 받을 수
# 있어 CONFIG_PATH 를 건드리지 않고도 합성 설정을 주입할 수 있다 — 신 코드
# 테스트(test_trend_golden.py)도 같은 파일을 json.load 로 읽어 쓴다).
CONFIG = {
    "window": {"from": "2023-09-01", "to": "2026-08-31"},
    "limit": None,
    "per_page": 200,
    "low_sample_threshold": 5,
    "provisional_months": 1,
}


def main() -> None:
    # 구 코드는 provider 개념이 없어 RAW_DIR/{query_id} 를 직접 읽는다. RAW_DIR 을
    # raw/openalex 로 겨냥하면 raw/openalex/synthetic/ 을 그대로 읽게 되는데, 이
    # 경로는 신 코드의 data/raw/{provider}/{query_id}/ 레이아웃과 한 단계 우연히
    # 맞아떨어진다 — 픽스처 파일을 양쪽이 그대로 공유해서 읽을 수 있는 이유다.
    records.RAW_DIR = FIXTURE_ROOT / "raw" / "openalex"

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    result = aggregate.run(QUERY_ID, config=CONFIG, out_dir=GOLDEN_DIR, allow_sample=False)
    for name, count in result["written"].items():
        print(f"  {name:26} {count:>7,}행")

    record_list = records.load_records(QUERY_ID)
    normalized = normalize.normalize_all(record_list)
    stats = unmatched.collect_unmatched(record_list, normalized, "keywords_norm")
    rows = unmatched.to_rows(stats, top=200)
    target = GOLDEN_DIR / f"unmatched_{QUERY_ID}_keywords.csv"
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=unmatched.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {target.name:26} {len(rows):>7,}행")


if __name__ == "__main__":
    main()
