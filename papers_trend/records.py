"""2단계: 원본 JSONL 에서 집계에 쓸 필드만 뽑는다.

순수 변환이다. 네트워크도 DB도 만지지 않는다. 입력은 dict, 출력은 dict.

    python -m papers_trend.records --profile sunscreen          # 요약만 출력
    python -m papers_trend.records --profile sunscreen --out -   # JSONL 을 stdout 으로

month_bucket 이 모든 집계의 시간 단위다. publication_date 를 월 단위로 절삭한다.
발행일이 연도까지만 있는 레코드는 버리지 않고 month_bucket=None,
date_precision="year_only" 로 남긴 뒤 월별 집계에서만 빠진다. 제외 건수는 로그에 남는다.

OpenAlex 필드 경로 실측: 2026-08-20 (collect_openalex.py 상단 주석 참조)
    keywords[].display_name / topics[].display_name / concepts[].display_name
    publication_date 는 이 3년 창의 표본 400건에서 전부 YYYY-MM-DD 였다.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"

FIELDS = (
    "openalex_id", "doi", "title", "publication_date", "month_bucket",
    "date_precision", "year", "journal", "keywords", "topics", "concepts",
    "citation_count", "collected_at", "query_id",
)


def display_names(items):
    """list[dict] 에서 display_name 만. 원본 표기를 그대로 둔다 (정규화는 normalize.py)."""
    if not isinstance(items, list):
        return []
    return [
        item.get("display_name")
        for item in items
        if isinstance(item, dict) and item.get("display_name")
    ]


def parse_date(raw, fallback_year=None):
    """(month_bucket, date_precision, year) 를 돌려준다.

    OpenAlex 는 대체로 YYYY-MM-DD 를 주지만 YYYY 만 오는 레코드가 존재한다.
    그런 레코드는 버리지 않는다. 월별 집계에서만 빠진다.
    """
    text = (raw or "").strip()
    if len(text) >= 10:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            return parsed.strftime("%Y-%m"), "day", parsed.year
        except ValueError:
            pass
    if len(text) >= 7:
        try:
            parsed = datetime.strptime(text[:7], "%Y-%m")
            return parsed.strftime("%Y-%m"), "month", parsed.year
        except ValueError:
            pass
    if len(text) >= 4 and text[:4].isdigit():
        return None, "year_only", int(text[:4])
    if fallback_year:
        return None, "year_only", int(fallback_year)
    return None, "unknown", None


def bare_doi(value):
    if not value:
        return None
    doi = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi or None


def to_record(work, query_id, collected_at=None):
    """OpenAlex work 원본 -> 집계용 레코드. 실패하지 않는다."""
    month_bucket, precision, year = parse_date(
        work.get("publication_date"), work.get("publication_year")
    )
    source = (work.get("primary_location") or {}).get("source") or {}
    return {
        "openalex_id": work.get("id"),
        "doi": bare_doi(work.get("doi")),
        "title": work.get("title"),
        "publication_date": work.get("publication_date"),
        "month_bucket": month_bucket,
        "date_precision": precision,
        "year": year,
        "journal": source.get("display_name"),
        "keywords": display_names(work.get("keywords")),
        "topics": display_names(work.get("topics")),
        "concepts": display_names(work.get("concepts")),
        "citation_count": work.get("cited_by_count") or 0,
        "collected_at": collected_at,
        "query_id": query_id,
    }


def raw_meta(query_id):
    path = RAW_DIR / query_id / "_meta.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def iter_raw(query_id):
    """raw/{query_id}/*.jsonl 전체를 openalex_id 기준으로 중복 없이 흘려준다."""
    directory = RAW_DIR / query_id
    if not directory.exists():
        raise FileNotFoundError(
            f"{directory} 가 없습니다. 먼저 collect_openalex 를 실행하세요."
        )
    seen = set()
    for path in sorted(directory.glob("*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    work = json.loads(line)
                except ValueError:
                    continue
                identifier = work.get("id")
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                yield work


def load_records(query_id):
    collected_at = raw_meta(query_id).get("collected_at")
    return [to_record(work, query_id, collected_at) for work in iter_raw(query_id)]


def summarize(records):
    precision = Counter(record["date_precision"] for record in records)
    months = Counter(record["month_bucket"] for record in records if record["month_bucket"])
    return {
        "total": len(records),
        "date_precision": dict(precision),
        "excluded_from_monthly": sum(
            1 for record in records if not record["month_bucket"]
        ),
        "months": dict(sorted(months.items())),
        "unique_keywords": len({k for r in records for k in r["keywords"]}),
        "unique_topics": len({t for r in records for t in r["topics"]}),
        "unique_concepts": len({c for r in records for c in r["concepts"]}),
        "with_doi": sum(1 for record in records if record["doi"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m papers_trend.records",
        description="원본 JSONL 에서 집계용 필드를 추출한다. 순수 변환.",
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--out", default=None,
                        help="JSONL 출력 경로. '-' 면 stdout. 생략하면 요약만 출력")
    args = parser.parse_args(argv)

    records = load_records(args.profile)
    summary = summarize(records)

    if args.out:
        stream = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")
        try:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            if stream is not sys.stdout:
                stream.close()
        if args.out != "-":
            print(f"{len(records):,}건 -> {args.out}", file=sys.stderr)

    report = sys.stderr if args.out == "-" else sys.stdout
    print(f"[{args.profile}] 레코드 {summary['total']:,}건", file=report)
    print(f"  date_precision: {summary['date_precision']}", file=report)
    print(f"  월별 집계 제외 (month_bucket 없음): {summary['excluded_from_monthly']:,}건",
          file=report)
    print(f"  DOI 보유: {summary['with_doi']:,}건", file=report)
    print(f"  고유 keywords {summary['unique_keywords']:,} / "
          f"topics {summary['unique_topics']:,} / "
          f"concepts {summary['unique_concepts']:,}", file=report)
    print(f"  월 범위: {min(summary['months'], default='-')} ~ "
          f"{max(summary['months'], default='-')} ({len(summary['months'])}개월)",
          file=report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
