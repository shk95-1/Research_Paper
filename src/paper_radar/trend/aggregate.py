"""5단계: 집계. 입력은 JSONL, 출력은 CSV. DB 를 쓰지 않는다.

papers_trend/aggregate.py 를 이식했다. 이 태스크(T6)에서 바꾼 것 셋:
    1. 출력 네임스페이스: out/trend/{query_id}/ 아래에 쓴다(구 코드는 여러
       프로파일이 out/ 하나를 공유해 서로 덮어썼다).
    2. _PROVISIONAL_CACHE 모듈 전역을 없앴다 — trend_metrics() 가 매 호출마다
       로컬 dict 를 만들어 _is_provisional_month() 에 넘긴다. 모듈 전역은
       여러 query_id 를 한 프로세스에서 연달아 집계할 때(예: --profile all)
       이전 호출의 캐시가 남아 있을 위험과, 테스트 간 상태 누수를 동시에
       만든다 — 인자로 전달하면 그 위험이 아예 성립하지 않는다.
    3. census_guard() 가 SystemExit 대신 CensusError(ValueError) 를 던진다.
       라이브러리 함수가 프로세스를 직접 죽이면(SystemExit) CLI 가 아닌
       맥락(테스트, 다른 파이프라인에서 import)에서 이 함수를 부를 수 없다
       — 종료 여부와 종료 코드를 결정하는 것은 호출자(CLI)의 몫이어야 한다.

재실행이 항상 같은 결과를 내야 한다. 그래서 상태를 들고 있지 않는다.
사전이나 불용어를 고치면 이 단계만 다시 돌린다.

산출물 (out/trend/{query_id}/ 아래)
    monthly_denominator.csv   월 x 프로파일. 분모와 신뢰 플래그
    keyword_monthly.csv       키워드 x 월 x 프로파일. 주 산출물
    topic_monthly.csv         topics 용. keywords 와 절대 합치지 않는다
    trend_metrics.csv         키워드당 1행. 떠오르는가 / 유지되는가

prevalence 가 주 지표다. 절대 건수를 쓰지 않는다. 논문 발행량 자체가 해마다
늘어 절대 건수는 전부 우상향한다.

CSV 표면(컬럼명·순서·값 형식·utf-8-sig 인코딩·불리언 "True"/"False")은
과거 산출물과의 조인이 걸린 공개 계약이다 — tests/fixtures/trend_golden/ 이
그 불변을 지킨다.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from paper_radar.trend import normalize, records, weight

HERE = Path(__file__).resolve().parent
OUT_DIR = records.REPO_ROOT / "out" / "trend"
CONFIG_PATH = HERE / "config.json"

# --- trend_class 분류 상수. 전부 여기 한 곳에 모은다 ----------------------
RECENT_MONTHS = 12  # 최근 창
PRIOR_MONTHS = 24  # 비교 대상 창
MIN_TOTAL_PAPERS = 10  # 이 아래면 분류하지 않고 unrated
NEW_ENTRANT_MIN_RECENT = 5  # prior 0건이면서 recent 가 이만큼 이상이면 신규 진입
EMERGING_GROWTH = 1.5  # growth_ratio 하한
DECLINING_GROWTH = 0.67  # growth_ratio 상한
STEADY_PRESENCE = 0.5  # months_present / 전체 개월. 이상이면 '유지'로 본다
SPORADIC_PRESENCE = 0.25  # 이 아래면서 성장하면 sporadic (경계)


class CensusError(ValueError):
    """census_guard 가 던진다 — 전수가 아닌 데이터를 --allow-sample 없이 집계하려 함.

    SystemExit 이 아니라 일반 예외인 이유: 라이브러리 함수가 프로세스를 직접
    죽이면 테스트나 다른 파이프라인에서 이 함수를 호출할 수 없게 된다.
    종료할지, 어떤 코드로 종료할지는 호출자(CLI)가 잡아서 결정한다.
    """


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
    "month_bucket",
    "query_id",
    "paper_count",
    "is_low_sample",
    "is_provisional",
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
    "month_bucket",
    "query_id",
    "keyword_key",
    "canonical_en",
    "category",
    "is_in_lexicon",
    "paper_count",
    "total_papers",
    "prevalence",
    "citation_sum",
    "citation_median",
    "weighted_prevalence",
    "is_low_sample",
    "is_provisional",
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

    buckets = defaultdict(
        lambda: {
            "canonical_en": "",
            "category": "",
            "is_in_lexicon": False,
            "papers": 0,
            "citations": [],
            "percentiles": [],
        }
    )
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
        rows.append(
            {
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
            }
        )
    rows.sort(key=lambda row: (row["month_bucket"], -row["paper_count"], row["keyword_key"]))
    return rows


# --- 산출물 C --------------------------------------------------------------

METRICS_FIELDS = [
    "keyword_key",
    "canonical_en",
    "category",
    "is_in_lexicon",
    "query_id",
    "first_seen_month",
    "total_papers",
    "months_present",
    "months_observed",
    "prevalence_recent",
    "prevalence_prior",
    "growth_ratio",
    "is_new_entrant",
    "weighted_prevalence_recent",
    "trend_class",
]


def classify(total_papers, months_present, months_observed, growth_ratio, is_new_entrant):
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


def _is_provisional_month(monthly_rows, month, cache):
    """month 가 provisional 월인지. cache 는 호출자(trend_metrics)가 소유한
    로컬 dict 다 — 모듈 전역으로 두지 않는다(위 모듈 docstring 참조)."""
    if month not in cache:
        cache[month] = any(
            row["is_provisional"] for row in monthly_rows if row["month_bucket"] == month
        )
    return cache[month]


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
        month_add(latest, -offset) for offset in range(RECENT_MONTHS, RECENT_MONTHS + PRIOR_MONTHS)
    }

    grouped = defaultdict(list)
    for row in monthly_rows:
        grouped[row["keyword_key"]].append(row)

    provisional_cache: dict[str, bool] = {}
    observed_recent = sorted(
        m
        for m in all_months
        if m in recent_window and not _is_provisional_month(monthly_rows, m, provisional_cache)
    )
    observed_prior = sorted(m for m in all_months if m in prior_window)

    rows = []
    for key, entries in grouped.items():
        by_month = {row["month_bucket"]: row for row in entries}
        recent = [by_month[m] for m in observed_recent if m in by_month]
        prior = [by_month[m] for m in observed_prior if m in by_month]

        prevalence_recent = _mean([r["prevalence"] for r in recent], len(observed_recent))
        prevalence_prior = _mean([r["prevalence"] for r in prior], len(observed_prior))
        recent_papers = sum(r["paper_count"] for r in recent)
        prior_papers = sum(r["paper_count"] for r in prior)

        growth = None
        if prevalence_prior:
            growth = prevalence_recent / prevalence_prior
        is_new = prior_papers == 0 and recent_papers >= NEW_ENTRANT_MIN_RECENT

        total_papers = sum(row["paper_count"] for row in entries)
        months_present = len(entries)
        first = entries[0]
        rows.append(
            {
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
            }
        )
    rows.sort(key=lambda row: (-row["total_papers"], row["keyword_key"]))
    return rows


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


def census_guard(query_id, allow_sample=False, provider=records.DEFAULT_PROVIDER):
    """전수가 아닌 데이터로 비율 지표를 내는 것을 막는다.

    is_census 가 false 면 OpenAlex 기본 정렬(relevance_score)이 어느 논문이
    포함됐는지를 결정한다. relevance 는 인용수를 반영하므로 최근 논문이 빠진다.
    그 표본의 prevalence 를 모집단 비율로 읽으면 조용히 틀린 결론이 나온다.

    allow_sample=False 이고 전수가 아니면 CensusError 를 던진다(SystemExit
    아님 — 모듈 docstring 참조).
    """
    meta = records.raw_meta(query_id, provider)
    if meta.get("is_census"):
        return meta
    collected = meta.get("collected") or 0
    expected = meta.get("expected_from_api") or 0
    ratio = f"{collected:,}/{expected:,}" if expected else f"{collected:,}"
    message = (
        f"[중단] {query_id} 는 전수가 아닙니다 ({ratio}). "
        "prevalence 를 모집단 비율로 해석할 수 없습니다. "
        "수집을 마치거나, 표본임을 알고 쓰려면 --allow-sample 을 붙이세요."
    )
    if not allow_sample:
        raise CensusError(message)

    print(f"[warn] {message}", file=sys.stderr)
    print(
        "[warn] 아래 CSV 의 prevalence 는 표본 내 비율입니다. 모집단 비율이 아닙니다.",
        file=sys.stderr,
    )
    return meta


def run(
    query_id,
    config=None,
    out_dir=None,
    allow_sample=False,
    provider=records.DEFAULT_PROVIDER,
):
    """query_id 하나를 집계해 out_dir/{query_id}/ 아래에 CSV 4개를 쓴다.

    out_dir 생략 시 OUT_DIR(out/trend/) 아래 query_id 네임스페이스를 쓴다 —
    여러 프로파일이 하나의 out/ 을 공유해 서로 덮어쓰던 구 버그(T6 배경)의
    수정이 바로 이 기본값이다.
    """
    config = config or load_config()
    census_guard(query_id, allow_sample, provider)
    record_list = records.load_records(query_id, provider)
    normalized = normalize.normalize_all(record_list)
    weighted = weight.assign_paper_weights(normalized)

    collected_at = records.raw_meta(query_id, provider).get("collected_at")
    provisional = provisional_months(collected_at, config.get("provisional_months", 3))

    denominator = monthly_denominator(weighted, query_id, config, provisional)
    all_months = [row["month_bucket"] for row in denominator]

    keyword_rows = term_monthly(weighted, query_id, config, provisional, "keywords_norm")
    topic_rows = term_monthly(weighted, query_id, config, provisional, "topics_norm")
    metric_rows = trend_metrics(keyword_rows, query_id, all_months)

    namespace = Path(out_dir) / query_id if out_dir else OUT_DIR / query_id
    written = {
        "monthly_denominator.csv": write_csv(
            namespace / "monthly_denominator.csv", DENOMINATOR_FIELDS, denominator
        ),
        "keyword_monthly.csv": write_csv(
            namespace / "keyword_monthly.csv", MONTHLY_FIELDS, keyword_rows
        ),
        "topic_monthly.csv": write_csv(namespace / "topic_monthly.csv", MONTHLY_FIELDS, topic_rows),
        "trend_metrics.csv": write_csv(
            namespace / "trend_metrics.csv", METRICS_FIELDS, metric_rows
        ),
    }
    return {
        "out_dir": namespace,
        "written": written,
        "records": len(record_list),
        "months": len(all_months),
        "provisional": sorted(m for m in all_months if m in provisional),
        "low_sample": [r["month_bucket"] for r in denominator if r["is_low_sample"]],
        "metrics": metric_rows,
        "denominator": denominator,
    }
