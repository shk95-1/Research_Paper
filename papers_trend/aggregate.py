"""5단계: 집계. 입력은 JSONL, 출력은 CSV. DB 를 쓰지 않는다.

    python -m papers_trend.aggregate --profile sunscreen

재실행이 항상 같은 결과를 내야 한다. 그래서 상태를 들고 있지 않는다.
사전이나 불용어를 고치면 이 단계만 다시 돌린다.

산출물
    out/monthly_denominator.csv   월 x 프로파일. 분모와 신뢰 플래그
    out/keyword_monthly.csv       키워드 x 월 x 프로파일. 주 산출물
    out/topic_monthly.csv         topics 용. keywords 와 절대 합치지 않는다
    out/trend_metrics.csv         키워드당 1행. 떠오르는가 / 유지되는가

prevalence 가 주 지표다. 절대 건수를 쓰지 않는다. 논문 발행량 자체가 해마다
늘어 절대 건수는 전부 우상향한다.
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from . import normalize, records, weight

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
CONFIG_PATH = HERE / "config.json"

# --- trend_class 분류 상수. 전부 여기 한 곳에 모은다 ----------------------
RECENT_MONTHS = 12          # 최근 창
PRIOR_MONTHS = 24           # 비교 대상 창
MIN_TOTAL_PAPERS = 10       # 이 아래면 분류하지 않고 unrated
NEW_ENTRANT_MIN_RECENT = 5  # prior 0건이면서 recent 가 이만큼 이상이면 신규 진입
EMERGING_GROWTH = 1.5       # growth_ratio 하한
DECLINING_GROWTH = 0.67     # growth_ratio 상한
STEADY_PRESENCE = 0.5       # months_present / 전체 개월. 이상이면 '유지'로 본다
SPORADIC_PRESENCE = 0.25    # 이 아래면서 성장하면 sporadic (경계)


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def month_add(month, delta):
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + (mon - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def provisional_months(collected_at, count):
    """수집일 기준 최근 count 개월. OpenAlex 색인이 아직 차지 않은 구간."""
    stamp = (collected_at or date.today().isoformat())[:7]
    return {month_add(stamp, -offset) for offset in range(count)}


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def round_or_none(value, digits=6):
    return None if value is None else round(value, digits)


# --- 산출물 A --------------------------------------------------------------

DENOMINATOR_FIELDS = [
    "month_bucket", "query_id", "paper_count", "is_low_sample", "is_provisional",
]


def monthly_denominator(weighted, query_id, config, provisional):
    threshold = config.get("low_sample_threshold", 50)
    counts = defaultdict(int)
    for record in weighted:
        if record.get("month_bucket"):
            counts[record["month_bucket"]] += 1
    return [
        {
            "month_bucket": month,
            "query_id": query_id,
            "paper_count": count,
            "is_low_sample": count < threshold,
            "is_provisional": month in provisional,
        }
        for month, count in sorted(counts.items())
    ]


# --- 산출물 B --------------------------------------------------------------

MONTHLY_FIELDS = [
    "month_bucket", "query_id", "keyword_key", "canonical_en", "category",
    "is_in_lexicon", "paper_count", "total_papers", "prevalence",
    "citation_sum", "citation_median", "weighted_prevalence",
    "is_low_sample", "is_provisional",
]


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def term_monthly(weighted, query_id, config, provisional, field):
    """(keyword_key, month_bucket) 한 조합에 한 행.

    field 는 'keywords_norm' 또는 'topics_norm'. 두 필드는 성격이 달라
    같은 스키마로 각각 별도 파일에 낸다.
    """
    threshold = config.get("low_sample_threshold", 50)
    totals = defaultdict(int)
    for record in weighted:
        if record.get("month_bucket"):
            totals[record["month_bucket"]] += 1

    buckets = defaultdict(lambda: {
        "canonical_en": "", "category": "", "is_in_lexicon": False,
        "papers": 0, "citations": [], "percentiles": [],
    })
    for record in weighted:
        month = record.get("month_bucket")
        if not month:
            continue  # 월별 집계에서만 제외. 레코드 자체는 버리지 않았다
        for item in record.get(field) or []:
            bucket = buckets[(item["keyword_key"], month)]
            bucket["canonical_en"] = item["canonical_en"]
            bucket["category"] = item["category"]
            bucket["is_in_lexicon"] = item["is_in_lexicon"]
            bucket["papers"] += 1
            bucket["citations"].append(record.get("citation_count") or 0)
            bucket["percentiles"].append(record.get("citation_percentile_in_cohort"))

    rows = []
    for (key, month), bucket in buckets.items():
        total = totals.get(month, 0)
        rows.append({
            "month_bucket": month,
            "query_id": query_id,
            "keyword_key": key,
            "canonical_en": bucket["canonical_en"],
            "category": bucket["category"],
            "is_in_lexicon": bucket["is_in_lexicon"],
            "paper_count": bucket["papers"],
            "total_papers": total,
            "prevalence": round_or_none(bucket["papers"] / total if total else None),
            "citation_sum": sum(bucket["citations"]),
            "citation_median": round_or_none(median(bucket["citations"]), 2),
            "weighted_prevalence": round_or_none(
                weight.weighted_prevalence(bucket["percentiles"])
            ),
            "is_low_sample": total < threshold,
            "is_provisional": month in provisional,
        })
    rows.sort(key=lambda row: (row["month_bucket"], -row["paper_count"],
                              row["keyword_key"]))
    return rows


# --- 산출물 C --------------------------------------------------------------

METRICS_FIELDS = [
    "keyword_key", "canonical_en", "category", "is_in_lexicon", "query_id",
    "first_seen_month", "total_papers", "months_present", "months_observed",
    "prevalence_recent", "prevalence_prior", "growth_ratio", "is_new_entrant",
    "weighted_prevalence_recent", "trend_class",
]


def classify(total_papers, months_present, months_observed, growth_ratio,
             is_new_entrant):
    """임계값 없이 growth_ratio 만 보면 3편->9편도 300% 가 된다.

    그래서 total_papers 하한을 못 넘으면 분류하지 않는다.
    """
    if total_papers < MIN_TOTAL_PAPERS:
        return "unrated"
    presence = months_present / months_observed if months_observed else 0.0
    if is_new_entrant:
        return "emerging" if presence >= SPORADIC_PRESENCE else "sporadic"
    if growth_ratio is None:
        return "unrated"
    if growth_ratio >= EMERGING_GROWTH:
        if presence < SPORADIC_PRESENCE:
            return "sporadic"  # 특정 연구실이 한 번에 몇 편 낸 것일 가능성
        return "emerging"
    if growth_ratio <= DECLINING_GROWTH:
        return "declining"
    if presence >= STEADY_PRESENCE:
        return "steady"
    return "sporadic"


def trend_metrics(monthly_rows, query_id, all_months):
    """키워드당 1행.

    is_provisional 인 달은 prevalence_recent 계산에서 제외한다. 색인 미완
    구간을 최근 평균에 넣으면 모든 키워드가 하락으로 보인다.
    """
    if not all_months:
        return []
    latest = max(all_months)
    recent_window = {month_add(latest, -offset) for offset in range(RECENT_MONTHS)}
    prior_window = {
        month_add(latest, -offset)
        for offset in range(RECENT_MONTHS, RECENT_MONTHS + PRIOR_MONTHS)
    }

    grouped = defaultdict(list)
    for row in monthly_rows:
        grouped[row["keyword_key"]].append(row)

    observed_recent = sorted(
        m for m in all_months
        if m in recent_window and not _is_provisional_month(monthly_rows, m)
    )
    observed_prior = sorted(m for m in all_months if m in prior_window)

    rows = []
    for key, entries in grouped.items():
        by_month = {row["month_bucket"]: row for row in entries}
        recent = [by_month[m] for m in observed_recent if m in by_month]
        prior = [by_month[m] for m in observed_prior if m in by_month]

        prevalence_recent = _mean(
            [r["prevalence"] for r in recent], len(observed_recent)
        )
        prevalence_prior = _mean(
            [r["prevalence"] for r in prior], len(observed_prior)
        )
        recent_papers = sum(r["paper_count"] for r in recent)
        prior_papers = sum(r["paper_count"] for r in prior)

        growth = None
        if prevalence_prior:
            growth = prevalence_recent / prevalence_prior
        is_new = prior_papers == 0 and recent_papers >= NEW_ENTRANT_MIN_RECENT

        total_papers = sum(row["paper_count"] for row in entries)
        months_present = len(entries)
        first = entries[0]
        rows.append({
            "keyword_key": key,
            "canonical_en": first["canonical_en"],
            "category": first["category"],
            "is_in_lexicon": first["is_in_lexicon"],
            "query_id": query_id,
            "first_seen_month": min(by_month),
            "total_papers": total_papers,
            "months_present": months_present,
            "months_observed": len(all_months),
            "prevalence_recent": round_or_none(prevalence_recent),
            "prevalence_prior": round_or_none(prevalence_prior),
            "growth_ratio": round_or_none(growth, 4),
            "is_new_entrant": is_new,
            "weighted_prevalence_recent": round_or_none(
                _mean_present([r["weighted_prevalence"] for r in recent])
            ),
            "trend_class": classify(
                total_papers, months_present, len(all_months), growth, is_new
            ),
        })
    rows.sort(key=lambda row: (-row["total_papers"], row["keyword_key"]))
    return rows


_PROVISIONAL_CACHE = {}


def _is_provisional_month(monthly_rows, month):
    if month not in _PROVISIONAL_CACHE:
        _PROVISIONAL_CACHE[month] = any(
            row["is_provisional"] for row in monthly_rows
            if row["month_bucket"] == month
        )
    return _PROVISIONAL_CACHE[month]


def _mean(values, denominator):
    """등장하지 않은 달은 0 으로 센다. 등장한 달만 평균하면 희소한 키워드가
    과대평가된다 (한 달에 한 번 크게 나온 것이 매달 꾸준한 것보다 높아진다)."""
    if not denominator:
        return None
    return sum(v for v in values if v is not None) / denominator


def _mean_present(values):
    usable = [v for v in values if v is not None]
    return sum(usable) / len(usable) if usable else None


# --- 실행 ------------------------------------------------------------------

def census_guard(query_id, allow_sample=False):
    """전수가 아닌 데이터로 비율 지표를 내는 것을 막는다.

    is_census 가 false 면 OpenAlex 기본 정렬(relevance_score)이 어느 논문이
    포함됐는지를 결정한다. relevance 는 인용수를 반영하므로 최근 논문이 빠진다.
    그 표본의 prevalence 를 모집단 비율로 읽으면 조용히 틀린 결론이 나온다.
    """
    meta = records.raw_meta(query_id)
    if meta.get("is_census"):
        return meta
    collected = meta.get("collected") or 0
    expected = meta.get("expected_from_api") or 0
    ratio = f"{collected:,}/{expected:,}" if expected else f"{collected:,}"
    message = (
        f"{query_id} 는 전수가 아닙니다 ({ratio}). "
        "prevalence 를 모집단 비율로 해석할 수 없습니다. "
        "수집을 마치거나, 표본임을 알고 쓰려면 --allow-sample 을 붙이세요."
    )
    if not allow_sample:
        raise SystemExit(f"[중단] {message}")
    print(f"[warn] {message}", file=sys.stderr)
    print("[warn] 아래 CSV 의 prevalence 는 표본 내 비율입니다. "
          "모집단 비율이 아닙니다.", file=sys.stderr)
    return meta


def run(query_id, config=None, out_dir=OUT_DIR, allow_sample=False):
    config = config or load_config()
    census_guard(query_id, allow_sample)
    record_list = records.load_records(query_id)
    normalized = normalize.normalize_all(record_list)
    weighted = weight.assign_paper_weights(normalized)

    collected_at = records.raw_meta(query_id).get("collected_at")
    provisional = provisional_months(
        collected_at, config.get("provisional_months", 3)
    )
    _PROVISIONAL_CACHE.clear()

    denominator = monthly_denominator(weighted, query_id, config, provisional)
    all_months = [row["month_bucket"] for row in denominator]

    keyword_rows = term_monthly(weighted, query_id, config, provisional,
                                "keywords_norm")
    topic_rows = term_monthly(weighted, query_id, config, provisional,
                              "topics_norm")
    metric_rows = trend_metrics(keyword_rows, query_id, all_months)

    out_dir = Path(out_dir)
    written = {
        "monthly_denominator.csv": write_csv(
            out_dir / "monthly_denominator.csv", DENOMINATOR_FIELDS, denominator),
        "keyword_monthly.csv": write_csv(
            out_dir / "keyword_monthly.csv", MONTHLY_FIELDS, keyword_rows),
        "topic_monthly.csv": write_csv(
            out_dir / "topic_monthly.csv", MONTHLY_FIELDS, topic_rows),
        "trend_metrics.csv": write_csv(
            out_dir / "trend_metrics.csv", METRICS_FIELDS, metric_rows),
    }
    return {
        "written": written,
        "records": len(record_list),
        "months": len(all_months),
        "provisional": sorted(m for m in all_months if m in provisional),
        "low_sample": [r["month_bucket"] for r in denominator if r["is_low_sample"]],
        "metrics": metric_rows,
        "denominator": denominator,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m papers_trend.aggregate",
        description="JSONL -> CSV 집계. DB 를 쓰지 않는다.",
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--allow-sample", action="store_true",
                        help="전수가 아닌 데이터로도 집계한다. prevalence 가 표본 내"
                             " 비율이 된다는 것을 알고 쓸 때만")
    args = parser.parse_args(argv)

    result = run(args.profile, out_dir=Path(args.out),
                 allow_sample=args.allow_sample)
    print(f"[{args.profile}] 레코드 {result['records']:,}건, "
          f"{result['months']}개월")
    for name, count in result["written"].items():
        print(f"  {name:26} {count:>7,}행")
    print(f"  provisional (색인 미완 추정): {', '.join(result['provisional']) or '없음'}")
    print(f"  low_sample: {', '.join(result['low_sample']) or '없음'}")

    classes = defaultdict(int)
    for row in result["metrics"]:
        classes[row["trend_class"]] += 1
    print(f"  trend_class: {dict(sorted(classes.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
