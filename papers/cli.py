"""CLI. 인자 파싱과 출력만 담당한다. 수집 로직은 pipeline 에 있다.

  python -m papers collect --query "cosmetic" --from 2016 --to 2026 --limit 100
  python -m papers trend   --query "cosmetic retinol"
  python -m papers cite    --keyword "skin barrier" --min-confidence 70

collect 와 trend 는 네트워크를 쓴다. cite 는 로컬 DB 만 읽는다.
"""

import argparse
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from . import http, pipeline, store
from .sources import openalex

RECENT_YEARS = 10
DEFAULT_LIMIT = 25
ABSTRACT_PREVIEW = 300


def _default_years():
    """기본은 최근 10년. 오래된 논문은 처방과 규제가 이미 바뀌었을 수 있다."""
    this_year = datetime.now(timezone.utc).year
    return this_year - (RECENT_YEARS - 1), this_year


def parse_args(argv):
    year_from, year_to = _default_years()
    parser = argparse.ArgumentParser(
        prog="python -m papers",
        description="화장품 트렌드 주장을 뒷받침할 논문 근거를 수집·검증합니다.",
    )
    parser.add_argument("--db", default=store.DEFAULT_DB, help="SQLite 경로")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="논문 수집")
    collect.add_argument("--query", required=True, help="영문 검색어")
    collect.add_argument("--from", dest="year_from", type=int, default=year_from)
    collect.add_argument("--to", dest="year_to", type=int, default=year_to)
    collect.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    collect.add_argument("--json", dest="json_path", default=store.DEFAULT_JSON)

    trend = subparsers.add_parser("trend", help="연도별 논문 수")
    trend.add_argument("--query", required=True, help="영문 검색어")
    trend.add_argument("--from", dest="year_from", type=int, default=year_from)
    trend.add_argument("--to", dest="year_to", type=int, default=year_to)

    cite = subparsers.add_parser("cite", help="저장된 논문 검색 (근거 인용용)")
    cite.add_argument("--keyword", required=True)
    cite.add_argument("--min-confidence", dest="min_confidence", type=int, default=0)
    cite.add_argument("--limit", type=int, default=20)
    cite.add_argument("--include-retracted", action="store_true")

    # --db 를 서브커맨드 뒤에도 쓸 수 있게 한다
    for sub in (collect, trend, cite):
        sub.add_argument("--db", default=store.DEFAULT_DB, help=argparse.SUPPRESS)

    return parser.parse_args(argv)


def _check_credentials():
    if not os.environ.get("OPENALEX_API_KEY", "").strip():
        http.warn(
            "OPENALEX_API_KEY 가 없어 무인증 예산(하루 1,000크레딧, search 기준 ~100건)"
            "으로 동작합니다. 무료 키를 등록하면 하루 100,000크레딧으로 늘어납니다."
        )
    if not http.contact_email():
        http.warn(
            "OPENALEX_EMAIL 이 없습니다. OpenAlex 의 polite pool 은 2026-02-13 폐지"
            "됐지만, 이 값은 Crossref polite 풀 연락처로는 여전히 쓰입니다. "
            ".env 에 추가하세요."
        )


def _run_collect(args):
    _check_credentials()
    print(f"검색: {args.query!r}  기간: {args.year_from}~{args.year_to}  상한: {args.limit}건")

    def progress(index, total, record):
        score = (record.get("verification") or {}).get("confidence_score", 0)
        title = (record.get("title") or "(제목 없음)")[:60]
        print(f"  [{index}/{total}] {score:3d}점  {title}")

    conn = store.connect(args.db)
    try:
        records = pipeline.collect(
            conn, args.query, args.year_from, args.year_to, args.limit,
            json_path=args.json_path, on_progress=progress,
        )
    finally:
        conn.close()

    print(f"\n저장 완료: {len(records)}건 -> {args.db}")
    if records:
        scores = [(r.get("verification") or {}).get("confidence_score", 0) for r in records]
        high = sum(1 for s in scores if s >= 70)
        with_abstract = sum(1 for r in records if r.get("abstract"))
        print(f"  신뢰도 70점 이상: {high}건 / 초록 확보: {with_abstract}건")
        print(f"  JSON 백업: {args.json_path}")
    return 0


def _run_trend(args):
    _check_credentials()
    counts = openalex.trend(args.query, args.year_from, args.year_to)
    if not counts:
        print("결과가 없습니다.")
        return 0
    print(f"검색: {args.query!r}  기간: {args.year_from}~{args.year_to}\n")
    widest = max(count for _, count in counts) or 1
    for year, count in counts:
        bar = "#" * max(1, round(count / widest * 40))
        print(f"  {year}  {count:>7,}  {bar}")
    print(f"\n합계 {sum(count for _, count in counts):,}건")
    return 0


def _run_cite(args):
    conn = store.connect(args.db)
    try:
        found = store.search(
            conn, args.keyword,
            min_confidence=args.min_confidence,
            include_retracted=args.include_retracted,
            limit=args.limit,
        )
    finally:
        conn.close()

    if not found:
        print("결과가 없습니다.")
        return 0

    print(f"'{args.keyword}' 관련 {len(found)}건 (신뢰도 {args.min_confidence}점 이상)\n")
    for index, record in enumerate(found, start=1):
        verification = record.get("verification") or {}
        authors = record.get("authors") or []
        byline = authors[0] + (" 외" if len(authors) > 1 else "") if authors else "저자 미상"
        print(f"[{index}] {record.get('title') or '(제목 없음)'}")
        print(f"    {byline} ({record.get('year') or '연도 미상'})"
              f"  {record.get('journal') or '저널 미상'}")
        print(f"    신뢰도 {verification.get('confidence_score', 0)}점"
              f"  인용 {record.get('citation_count') or 0}회"
              f"  출처 {', '.join(verification.get('found_in_sources') or [])}")
        if not verification.get("has_doi"):
            print("    DOI 없음")
        elif record.get("doi"):
            print(f"    https://doi.org/{record['doi']}")
        summary = record.get("tldr") or record.get("abstract")
        if summary:
            preview = summary[:ABSTRACT_PREVIEW]
            print(f"    {preview}{'...' if len(summary) > ABSTRACT_PREVIEW else ''}")
        print()
    return 0


COMMANDS = {"collect": _run_collect, "trend": _run_trend, "cite": _run_cite}


def run(args):
    return COMMANDS[args.command](args)


def main(argv=None):
    load_dotenv()
    return run(parse_args(argv))
