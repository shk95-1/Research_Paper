"""paper-radar CLI. 인자 파싱과 출력만 담당한다. 수집 로직은 evidence.pipeline 에 있다.

  paper-radar evidence collect --query "cosmetic" --from 2016 --to 2026 --limit 100
  paper-radar evidence trend   --query "cosmetic retinol"
  paper-radar evidence cite    --keyword "skin barrier" --min-confidence 70

evidence collect/trend 는 네트워크를 쓴다. evidence cite 는 로컬 DB 만 읽는다.
플래그·출력 문구는 papers/cli.py 와 동일하게 맞췄다 — 사용자 눈에는 같은
도구가 이름만 바뀐 것으로 보여야 한다.

명령 구조
    최상위 서브파서(dest="group")가 "evidence" 하나를 갖고, 그 아래
    서브파서(dest="command")가 collect/trend/cite 세 개를 갖는다. GROUPS 는
    이 두 단계를 (group, command) -> 핸들러로 잇는 딕셔너리다 — 나중에
    "evidence" 옆에 다른 그룹(예: papers_trend 를 잇는 T6 의 새 그룹)을 더할
    때 GROUPS 에 항목 하나만 추가하면 된다.

exit code
    0   완전 수집(evidence.pipeline.CollectReport.status == "ok") — cite/trend 는
        결과 유무와 무관하게 항상 0.
    1   부분 수집(status == "partial" — BudgetExhausted 로 중단됐거나 소스
        오류가 하나라도 있었다). "ok"/"partial" 판정은 pipeline.collect()
        한 곳에서만 계산한다 — CLI 는 report.status 를 그대로 옮길 뿐,
        stopped_reason/errors_by_source 를 다시 훑어 재계산하지 않는다.
    2   예약(reserved). 이번 태스크(T5b)에서는 배정하지 않는다. 파이프라인
        자체가 처리되지 않은 예외로 죽으면(버그 등, RunLog 에는 status="failed"
        로 남는다) 이 CLI 는 그 예외를 따로 잡지 않고 그대로 흘려보낸다 —
        프로세스는 인터프리터 기본 exit code(1)로 죽는다. "정상적인 부분
        수집"과 "파이프라인이 죽음"을 지금은 exit code 로 구분하지 않는다;
        구분이 필요해지면 그때 2 를 배정한다.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from paper_radar.evidence import pipeline
from paper_radar.sources import openalex
from paper_radar.storage import repository
from paper_radar.transport import warn
from paper_radar.transport.http import Transport

RECENT_YEARS = 10
DEFAULT_LIMIT = 25
ABSTRACT_PREVIEW = 300

# papers/store.py 시절과 같은 위치(papers/out/papers.db)를 기본값으로 유지한다
# — 이미 그 경로에 쌓아 둔 DB 를 쓰던 사용자가 --db 없이도 이어 쓸 수 있어야
# 한다. src/paper_radar/cli.py 기준 두 단계 위가 저장소 루트다.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = str(_REPO_ROOT / "papers" / "out" / "papers.db")
DEFAULT_JSON = str(_REPO_ROOT / "papers" / "out" / "papers.json")


def _default_years():
    """기본은 최근 10년. 오래된 논문은 처방과 규제가 이미 바뀌었을 수 있다."""
    this_year = datetime.now(UTC).year
    return this_year - (RECENT_YEARS - 1), this_year


def parse_args(argv):
    year_from, year_to = _default_years()
    parser = argparse.ArgumentParser(
        prog="paper-radar",
        description="화장품 트렌드 주장을 뒷받침할 논문 근거를 수집·검증합니다.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 경로")
    top = parser.add_subparsers(dest="group", required=True)

    evidence = top.add_parser("evidence", help="논문 근거 수집·검증")
    evidence_sub = evidence.add_subparsers(dest="command", required=True)

    collect = evidence_sub.add_parser("collect", help="논문 수집")
    collect.add_argument("--query", required=True, help="영문 검색어")
    collect.add_argument("--from", dest="year_from", type=int, default=year_from)
    collect.add_argument("--to", dest="year_to", type=int, default=year_to)
    collect.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    collect.add_argument("--json", dest="json_path", default=DEFAULT_JSON)

    trend = evidence_sub.add_parser("trend", help="연도별 논문 수")
    trend.add_argument("--query", required=True, help="영문 검색어")
    trend.add_argument("--from", dest="year_from", type=int, default=year_from)
    trend.add_argument("--to", dest="year_to", type=int, default=year_to)

    cite = evidence_sub.add_parser("cite", help="저장된 논문 검색 (근거 인용용)")
    cite.add_argument("--keyword", required=True)
    cite.add_argument("--min-confidence", dest="min_confidence", type=int, default=0)
    cite.add_argument("--limit", type=int, default=20)
    cite.add_argument("--include-retracted", action="store_true")

    # --db 를 서브커맨드 뒤에도 쓸 수 있게 한다
    for sub in (collect, trend, cite):
        sub.add_argument("--db", default=DEFAULT_DB, help=argparse.SUPPRESS)

    return parser.parse_args(argv)


def _check_credentials():
    if not os.environ.get("OPENALEX_API_KEY", "").strip():
        warn(
            "OPENALEX_API_KEY 가 없어 무인증 예산(하루 1,000크레딧, search 기준 ~100건)"
            "으로 동작합니다. 무료 키를 등록하면 하루 100,000크레딧으로 늘어납니다."
        )
    if not os.environ.get("OPENALEX_EMAIL", "").strip():
        warn(
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

    conn = repository.connect(args.db)
    try:
        report = pipeline.collect(
            conn,
            Transport(),
            args.query,
            args.year_from,
            args.year_to,
            args.limit,
            json_path=args.json_path,
            on_progress=progress,
        )
    finally:
        conn.close()

    records = report.records
    print(f"\n저장 완료: {len(records)}건 -> {args.db}")
    if records:
        scores = [(r.get("verification") or {}).get("confidence_score", 0) for r in records]
        high = sum(1 for s in scores if s >= 70)
        with_abstract = sum(1 for r in records if r.get("abstract"))
        print(f"  신뢰도 70점 이상: {high}건 / 초록 확보: {with_abstract}건")
        print(f"  JSON 백업: {args.json_path}")
    # "ok"/"partial" 판정은 pipeline.collect() 한 곳에서만 계산한다(단일 출처) —
    # CLI 는 그 결과(report.status)를 그대로 exit code 로 옮길 뿐, 여기서
    # stopped_reason/errors_by_source 를 다시 훑어 재계산하지 않는다.
    return 0 if report.status == "ok" else 1


def _run_trend(args):
    _check_credentials()
    counts = openalex.trend(Transport(), args.query, args.year_from, args.year_to)
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
    conn = repository.connect(args.db)
    try:
        found = repository.search(
            conn,
            args.keyword,
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
        print(
            f"    {byline} ({record.get('year') or '연도 미상'})"
            f"  {record.get('journal') or '저널 미상'}"
        )
        print(
            f"    신뢰도 {verification.get('confidence_score', 0)}점"
            f"  인용 {record.get('citation_count') or 0}회"
            f"  출처 {', '.join(verification.get('found_in_sources') or [])}"
        )
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
GROUPS = {"evidence": COMMANDS}


def run(args):
    return GROUPS[args.group][args.command](args)


def main(argv=None):
    load_dotenv()
    return run(parse_args(argv))
