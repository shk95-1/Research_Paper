"""사전 확장 도구. 정규화 후에도 사전에 없는 표현을 빈도순으로 뽑는다.

papers_trend/unmatched.py 를 이식했다(로직 변경 없음 — 출력 경로만
out/trend/{query_id}/ 네임스페이스 아래로 옮겼다). 파일명 자체는 그대로다
(unmatched_{query_id}_{field}.csv) — CSV 표면은 손대지 않는다.

이 단계를 자동화하지 않는다. 사람이 보는 단계가 사전 품질을 결정한다.
자동으로 상위 N개를 사전에 밀어넣으면 'population' 이나 'chromatography' 처럼
빈도만 높고 트렌드 신호가 아닌 것들이 표준키가 되어 되돌리기 어려워진다.

각 표현마다 예시 논문 제목을 붙인다. 어떤 맥락에서 나온 말인지 봐야
사전에 넣을지, 불용어로 보낼지, 그냥 둘지 판단할 수 있다.

판정 경로는 셋이다.
    사전에 추가        -> keyword_lexicon.json 에 표준키와 별칭
    불용어로           -> stopwords.json 의 field_labels
    그냥 두기          -> 아무것도 하지 않음. 정규화 표면형으로 계속 집계된다
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from paper_radar.trend import normalize, records

OUT_DIR = records.REPO_ROOT / "out" / "trend"

FIELDS = [
    "rank",
    "term",
    "paper_count",
    "first_seen_month",
    "last_seen_month",
    "months_present",
    "example_title_1",
    "example_title_2",
    "example_title_3",
    "verdict",
]
EXAMPLES_PER_TERM = 3
TITLE_MAX = 140


def collect_unmatched(record_list, normalized, field="keywords_norm"):
    """정규화됐지만 사전에 없는 표현을 모은다. 예시 제목을 함께 붙인다."""
    stats = defaultdict(lambda: {"papers": 0, "titles": [], "months": set()})
    # strict=False: 길이가 다를 때 기존처럼 짧은 쪽에 맞춰 자르던 동작을 유지한다.
    for record, normal in zip(record_list, normalized, strict=False):
        title = (record.get("title") or "").strip()
        month = record.get("month_bucket")
        for item in normal.get(field) or []:
            if item["is_in_lexicon"]:
                continue
            entry = stats[item["keyword_key"]]
            entry["papers"] += 1
            if month:
                entry["months"].add(month)
            if title and len(entry["titles"]) < EXAMPLES_PER_TERM:
                entry["titles"].append(title[:TITLE_MAX])
    return stats


def to_rows(stats, top=None):
    ordered = sorted(stats.items(), key=lambda kv: (-kv[1]["papers"], kv[0]))
    if top:
        ordered = ordered[:top]
    rows = []
    for rank, (term, entry) in enumerate(ordered, start=1):
        titles = entry["titles"] + [""] * EXAMPLES_PER_TERM
        months = sorted(entry["months"])
        rows.append(
            {
                "rank": rank,
                "term": term,
                "paper_count": entry["papers"],
                "first_seen_month": months[0] if months else "",
                "last_seen_month": months[-1] if months else "",
                "months_present": len(months),
                "example_title_1": titles[0],
                "example_title_2": titles[1],
                "example_title_3": titles[2],
                "verdict": "",  # 사람이 채운다: lexicon / stopword / keep
            }
        )
    return rows


def default_path(query_id, field, out_dir=OUT_DIR):
    """out/trend/{query_id}/unmatched_{query_id}_{field}.csv (구 파일명 그대로)."""
    return Path(out_dir) / query_id / f"unmatched_{query_id}_{field.replace('_norm', '')}.csv"


def run(
    query_id,
    field="keywords_norm",
    top=200,
    out_path=None,
    out_dir=OUT_DIR,
    provider=records.DEFAULT_PROVIDER,
):
    record_list = records.load_records(query_id, provider)
    normalized = normalize.normalize_all(record_list)
    stats = collect_unmatched(record_list, normalized, field)
    rows = to_rows(stats, top or None)

    target = Path(out_path) if out_path else default_path(query_id, field, out_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    return {
        "target": target,
        "rows": rows,
        "total_terms": len(stats),
        "total_hits": sum(entry["papers"] for entry in stats.values()),
    }
