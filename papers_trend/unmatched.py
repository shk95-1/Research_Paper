"""사전 확장 도구. 정규화 후에도 사전에 없는 표현을 빈도순으로 뽑는다.

    python -m papers_trend.unmatched --profile sunscreen --top 50

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

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from . import normalize, records

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"

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


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m papers_trend.unmatched",
        description="사전 미매칭 표현을 빈도순으로 뽑는다. 사람이 검수해서 사전에 넣는다.",
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument(
        "--field",
        default="keywords_norm",
        choices=["keywords_norm", "topics_norm", "concepts_norm"],
    )
    parser.add_argument("--top", type=int, default=200, help="CSV 에 담을 상한. 0 이면 전부")
    parser.add_argument("--show", type=int, default=25, help="화면에 출력할 개수")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    record_list = records.load_records(args.profile)
    normalized = normalize.normalize_all(record_list)
    stats = collect_unmatched(record_list, normalized, args.field)
    rows = to_rows(stats, args.top or None)

    target = (
        Path(args.out)
        if args.out
        else (OUT_DIR / f"unmatched_{args.profile}_{args.field.replace('_norm', '')}.csv")
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    total_terms = len(stats)
    total_hits = sum(entry["papers"] for entry in stats.values())
    print(f"[{args.profile}] {args.field} 미매칭 표현 {total_terms:,}종, 등장 {total_hits:,}회")
    print(f"  -> {target} ({len(rows):,}행)")
    print("  verdict 컬럼을 사람이 채운다: lexicon / stopword / keep\n")

    print(f"=== 상위 {args.show} ===")
    for row in rows[: args.show]:
        print(
            f"  {row['rank']:>3}. {row['paper_count']:>5}편  "
            f"{row['months_present']:>2}개월  {row['term']}"
        )
        if row["example_title_1"]:
            print(f"        예: {row['example_title_1'][:88]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
