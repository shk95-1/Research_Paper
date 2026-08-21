"""paper-radar CLI. 인자 파싱과 출력만 담당한다. 수집 로직은 evidence.pipeline 에 있다.

  paper-radar evidence collect --query "cosmetic" --from 2016 --to 2026 --limit 100
  paper-radar evidence trend   --query "cosmetic retinol"
  paper-radar evidence cite    --keyword "skin barrier" --min-confidence 70
  paper-radar trials  collect --query "sunscreen" --limit 100
  paper-radar trials  list    --keyword "sunscreen" --limit 20
  paper-radar ingredient resolve       --name "niacinamide"
  paper-radar ingredient show          --name "niacinamide"
  paper-radar ingredient import-cosing --path data/reference/cosing/cosing.csv

evidence collect/trend 는 네트워크를 쓴다. evidence cite 는 로컬 DB 만 읽는다.
플래그·출력 문구는 papers/cli.py 와 동일하게 맞췄다 — 사용자 눈에는 같은
도구가 이름만 바뀐 것으로 보여야 한다.

trials 그룹(T11)은 evidence 와 다른 저장소(trial 테이블, papers 테이블이
아니다)를 쓰는 완전히 별개의 레코드 종류다 — "논문이 성분을 언급한다"와
"그 주장 뒤에 대조시험이 있다"의 차이를 만든다. 실제 수집 로직(RunLog
자기기록 + iter_studies() 점진 소비 + upsert_records())은 evidence.pipeline/
trend.collect 와 대칭으로 trials.collect 에 있다 — 이 파일은 evidence/trend
와 마찬가지로 인자 해석 -> trials.collect.run() 호출 -> 출력만 담당한다.
status 판정("ok"/"partial")은 trials.collect.run() 안에서만 계산하고
TrialsReport.status 로 노출한다 — CollectReport 와 동일하게, CLI 핸들러는
그 값을 그대로 exit code 로 옮길 뿐 재계산하지 않는다(단일 출처 원칙).

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
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from paper_radar.evidence import pipeline
from paper_radar.ingredients import import_cosing as ingredients_import_cosing
from paper_radar.ingredients import resolve as ingredients_resolve
from paper_radar.sources import openalex, pubchem
from paper_radar.storage import repository
from paper_radar.transport import warn
from paper_radar.transport.http import Transport
from paper_radar.trend import aggregate as trend_aggregate
from paper_radar.trend import collect as trend_collect
from paper_radar.trend import collect_pubmed as trend_collect_pubmed
from paper_radar.trend import mesh_aggregate as trend_mesh_aggregate
from paper_radar.trend import normalize as trend_normalize
from paper_radar.trend import overlap as trend_overlap
from paper_radar.trend import records as trend_records
from paper_radar.trend import suggest as trend_suggest
from paper_radar.trend import unmatched as trend_unmatched
from paper_radar.trials import collect as trials_collect

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

    # trend_group: papers_trend/ 를 이식한 별도 파이프라인(T6). "evidence trend"
    # (연도별 논문 수 히스토그램, 위 트렌드 변수)와 이름이 겹치지 않게 최상위
    # 그룹 이름은 "trend" 로 고르되 헷갈림을 줄이려고 서브커맨드는 collect/
    # records/normalize/aggregate/unmatched 로 legacy `python -m papers_trend.X`
    # 와 그대로 대응시킨다.
    trend_group = top.add_parser("trend", help="논문 키워드 트렌드 파이프라인 (papers_trend 이식)")
    trend_sub = trend_group.add_subparsers(dest="command", required=True)

    t_collect = trend_sub.add_parser("collect", help="OpenAlex/PubMed 전수 수집")
    t_collect.add_argument(
        "--profile", default="all", help="config.json 의 프로파일 이름, 또는 all"
    )
    t_collect.add_argument("--config", default=None, help="config.json 경로 (생략 시 내장 기본값)")
    # provider: T15 — pubmed 를 trend 의 제2 프로바이더로 추가. 기본값
    # openalex 로 기존 동작 불변(브리핑 지시). collect.run()(openalex, 커서
    # 페이지네이션)과 collect_pubmed.run()(pubmed, 월별 retstart 페이지네이션)
    # 은 페이지네이션 단위 자체가 달라 별도 모듈이다 — 여기서는 그 둘 중
    # 어느 쪽을 부를지만 고른다(_run_trend_collect() 참고).
    t_collect.add_argument(
        "--provider",
        default="openalex",
        choices=["openalex", "pubmed"],
        help="수집 프로바이더 (기본 openalex)",
    )
    t_collect.add_argument(
        "--dry-run", action="store_true", help="건수와 예상 요청 수만 출력하고 수집하지 않는다"
    )
    t_collect.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="이번 실행에서 받을 페이지 상한 (--provider openalex). 커서가 저장되므로"
        " 여러 번 나눠 전수를 채울 수 있다",
    )
    t_collect.add_argument(
        "--max-months",
        type=int,
        default=None,
        help="이번 실행에서 처리할 달 상한 (--provider pubmed). 월 커서가 저장되므로"
        " 여러 번 나눠 전수를 채울 수 있다",
    )
    t_collect.add_argument("--db", default=DEFAULT_DB, help=argparse.SUPPRESS)

    t_records = trend_sub.add_parser("records", help="원본 JSONL 에서 집계용 필드를 추출한다")
    t_records.add_argument("--profile", required=True)
    t_records.add_argument(
        "--out", default=None, help="JSONL 출력 경로. '-' 면 stdout. 생략하면 요약만 출력"
    )

    t_normalize = trend_sub.add_parser("normalize", help="사전을 적용해 키워드를 표준키로 접는다")
    t_normalize.add_argument("--profile", required=True)
    t_normalize.add_argument("--top", type=int, default=20)

    t_aggregate = trend_sub.add_parser("aggregate", help="JSONL -> CSV 집계. DB 를 쓰지 않는다")
    t_aggregate.add_argument("--profile", required=True)
    t_aggregate.add_argument("--out", default=None, help="출력 디렉터리 (생략 시 out/trend/)")
    # provider: T15 — pubmed 면 mesh_monthly.csv 하나(keyword/topic_monthly.csv
    # 가 아니다), openalex 면 기존 CSV 4종. 두 provider 를 한 CSV 로 합치지
    # 않는다(모듈 docstring "대원칙" 참고) — provider 로 산출 파일 자체가
    # 갈린다.
    t_aggregate.add_argument(
        "--provider",
        default="openalex",
        choices=["openalex", "pubmed"],
        help="집계 프로바이더 (기본 openalex)",
    )
    t_aggregate.add_argument(
        "--allow-sample",
        action="store_true",
        help="전수가 아닌 데이터로도 집계한다. prevalence 가 표본 내"
        " 비율이 된다는 것을 알고 쓸 때만",
    )

    t_unmatched = trend_sub.add_parser(
        "unmatched", help="사전 미매칭 표현을 빈도순으로 뽑는다"
    )
    t_unmatched.add_argument("--profile", required=True)
    t_unmatched.add_argument(
        "--field",
        default="keywords_norm",
        choices=["keywords_norm", "topics_norm", "concepts_norm"],
    )
    t_unmatched.add_argument("--top", type=int, default=200, help="CSV 에 담을 상한. 0 이면 전부")
    t_unmatched.add_argument("--show", type=int, default=25, help="화면에 출력할 개수")
    t_unmatched.add_argument("--out", default=None)

    # overlap: T15 — openalex/pubmed raw 를 DOI 로 대조하는 진단(지표가
    # 아니다, trend/overlap.py 모듈 docstring 참고). --provider 를 받지
    # 않는다 — 정의상 두 프로바이더를 동시에 본다.
    t_overlap = trend_sub.add_parser(
        "overlap", help="openalex/pubmed raw 를 DOI 로 대조하는 교차 진단 (지표 아님)"
    )
    t_overlap.add_argument("--profile", required=True)
    t_overlap.add_argument("--out", default=None, help="출력 디렉터리 (생략 시 out/trend/)")

    # suggest: T14 — unmatched 미매칭 표현을 ingredient 테이블(PubChem+CosIng,
    # T12·T13)의 name_key/inci_name/synonym 과 정확 일치로 대조해 사전 확장
    # 후보를 제안한다. 로컬 전용(네트워크 없음, 새 소스 없음) — trials list/
    # ingredient show 와 같은 부류라 --db 를 받는다.
    t_suggest = trend_sub.add_parser(
        "suggest", help="unmatched 표현에 PubChem/CosIng 동의어 후보를 제안한다 (로컬 전용)"
    )
    t_suggest.add_argument("--profile", required=True)
    t_suggest.add_argument("--db", default=DEFAULT_DB, help=argparse.SUPPRESS)
    t_suggest.add_argument("--out", default=None, help="출력 베이스 디렉터리 (생략 시 out/trend/)")

    # trials: ClinicalTrials.gov v2(T11). papers/trend 어느 쪽과도 무관한
    # 독립 테이블(trial)이라 별도 최상위 그룹으로 둔다.
    trials = top.add_parser("trials", help="ClinicalTrials.gov 임상시험 레코드")
    trials_sub = trials.add_subparsers(dest="command", required=True)

    # 서브파서 변수명은 trials_collect_parser/trials_list_parser 로 둔다 —
    # 모듈 상단에서 import 한 trials_collect(paper_radar.trials.collect)와
    # 이름이 겹치면 헷갈린다(둘은 서로 다른 함수 스코프라 실제 충돌은 없지만
    # 가독성을 해친다).
    trials_collect_parser = trials_sub.add_parser("collect", help="임상시험 수집 (네트워크)")
    trials_collect_parser.add_argument("--query", required=True, help="영문 검색어")
    trials_collect_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    trials_list_parser = trials_sub.add_parser("list", help="저장된 임상시험 검색 (로컬 전용)")
    trials_list_parser.add_argument("--keyword", required=True)
    trials_list_parser.add_argument("--limit", type=int, default=20)

    for sub in (trials_collect_parser, trials_list_parser):
        sub.add_argument("--db", default=DEFAULT_DB, help=argparse.SUPPRESS)

    # ingredient: PubChem(T12)/CosIng(T13) 성분 실체 해소. trials 와 마찬가지로
    # 완전히 별개의 저장 표면(ingredient 테이블)이라 별도 최상위 그룹으로 둔다.
    ingredient = top.add_parser("ingredient", help="PubChem 성분 실체 해소")
    ingredient_sub = ingredient.add_subparsers(dest="command", required=True)

    ingredient_resolve_parser = ingredient_sub.add_parser(
        "resolve", help="PubChem 이름 -> CID/CAS/동의어 조회·저장 (네트워크)"
    )
    ingredient_resolve_parser.add_argument("--name", required=True, help="성분 이름(영문)")

    ingredient_show_parser = ingredient_sub.add_parser(
        "show", help="저장된 성분 실체 조회 (로컬 전용)"
    )
    ingredient_show_parser.add_argument("--name", required=True, help="성분 이름(영문)")

    # import-cosing: T13 — CosIng CSV(사람이 tool/fetch_cosing.py 로 미리 받아
    # 둔 파일)를 읽어 ingredient 테이블에 merge upsert 한다. 로컬 전용(네트워크
    # 없음) — trials list/ingredient show 와 같은 부류.
    ingredient_import_cosing_parser = ingredient_sub.add_parser(
        "import-cosing", help="CosIng CSV 임포트 -> INCI/CAS 조인 (로컬 전용)"
    )
    ingredient_import_cosing_parser.add_argument(
        "--path", required=True, help="tool/fetch_cosing.py 가 받아 둔 cosing.csv 경로"
    )

    for sub in (
        ingredient_resolve_parser,
        ingredient_show_parser,
        ingredient_import_cosing_parser,
    ):
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
        # conn 이 아직 열려 있는 동안 한 번에 배치 조회한다 — conn.close()
        # 이후 레코드마다 다시 쿼리하는 실수를 구조적으로 피한다.
        pdf_urls = repository.oa_pdf_urls(conn, (r.get("doi") for r in found))
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
            # PDF 는 링크만 — 어떤 경로로도 파일을 내려받지 않는다. oa_location
            # 에 그 DOI 가 없거나 pdf_url 이 없으면(비 OA 등) 아무것도 찍지
            # 않는다 — 기존 출력은 완전히 그대로다.
            pdf_url = pdf_urls.get(record["doi"])
            if pdf_url:
                print(f"    PDF: {pdf_url}")
        summary = record.get("tldr") or record.get("abstract")
        if summary:
            preview = summary[:ABSTRACT_PREVIEW]
            print(f"    {preview}{'...' if len(summary) > ABSTRACT_PREVIEW else ''}")
        print()
    return 0


def _run_trend_collect(args):
    """`paper-radar trend collect` — papers_trend/collect_openalex.py 의 main() 이식
    + T15 의 --provider 라우팅.

    프로파일별로 trend.collect.run()(openalex) 또는 trend.collect_pubmed.run()
    (pubmed) 을 호출한다 — 실제 수집·RunLog 기록은 거기서 한다. 여기서는
    인자 해석과 출력만.
    """
    if args.provider == "pubmed":
        config = trend_collect_pubmed.load_config(args.config or trend_collect_pubmed.CONFIG_PATH)
    else:
        config = trend_collect.load_config(args.config or trend_collect.CONFIG_PATH)
    profiles = config["profiles"]
    wanted = list(profiles) if args.profile == "all" else [args.profile]
    unknown = [name for name in wanted if name not in profiles]
    if unknown:
        warn(f"config 에 없는 프로파일: {', '.join(unknown)}. 사용 가능: {', '.join(profiles)}")
        return 1

    if args.provider == "pubmed":
        # pubmed_query 가 없는 프로파일은 이 provider 로 수집할 수 없다.
        # --profile all 이면(수집 미완 프로파일이 섞여 있을 수 있다) 조용히
        # 걸러내고, 특정 프로파일을 직접 지정했는데 없으면 명확한 오류로
        # 알린다(config.json 의 _pubmed_query_note 참고 — cosmetics 처럼
        # 아직 pubmed_query 를 안 넣은 프로파일이 있을 수 있다).
        missing = [name for name in wanted if "pubmed_query" not in profiles[name]]
        if args.profile != "all" and missing:
            warn(
                f"{', '.join(missing)}: config 에 pubmed_query 가 없습니다."
                " sunscreen 프로파일처럼 pubmed_query 를 추가한 뒤 다시 시도하세요."
            )
            return 1
        wanted = [name for name in wanted if name not in missing]
        if not wanted:
            warn("pubmed_query 가 설정된 프로파일이 없습니다")
            return 1

    if args.dry_run:
        if args.provider == "pubmed":
            results = trend_collect_pubmed.run(wanted, config, db_path=args.db, dry_run=True)
            for query_id, info in results.items():
                print(f"[{query_id}] {info['count']:,}건 ({info['months']}개월 창)")
            return 0
        results = trend_collect.run(wanted, config, db_path=args.db, dry_run=True)
        for query_id, info in results.items():
            count = info["count"]
            if count is None:
                print(f"[{query_id}] 건수 확인 실패")
                continue
            print(f"[{query_id}] {count:,}건 -> 예상 요청 {info['estimated_requests']}회")
        return 0

    if args.provider == "pubmed":
        results = trend_collect_pubmed.run(
            wanted, config, db_path=args.db, max_months=args.max_months
        )
    else:
        results = trend_collect.run(wanted, config, db_path=args.db, max_pages=args.max_pages)
    if any(meta is None for meta in results.values()):
        return 1
    if any((meta or {}).get("stopped_reason") for meta in results.values()):
        return 1
    return 0


def _run_trend_records(args):
    """`paper-radar trend records` — papers_trend/records.py 의 main() 이식."""
    record_list = trend_records.load_records(args.profile)
    summary = trend_records.summarize(record_list)

    if args.out:
        # stdout 은 with 블록으로 닫으면 안 되므로 try/finally 로 실제 파일일 때만 닫는다.
        stream = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")  # noqa: SIM115
        try:
            for record in record_list:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            if stream is not sys.stdout:
                stream.close()
        if args.out != "-":
            print(f"{len(record_list):,}건 -> {args.out}", file=sys.stderr)

    report_stream = sys.stderr if args.out == "-" else sys.stdout
    print(f"[{args.profile}] 레코드 {summary['total']:,}건", file=report_stream)
    print(f"  date_precision: {summary['date_precision']}", file=report_stream)
    print(
        f"  월별 집계 제외 (month_bucket 없음): {summary['excluded_from_monthly']:,}건",
        file=report_stream,
    )
    print(f"  DOI 보유: {summary['with_doi']:,}건", file=report_stream)
    print(
        f"  고유 keywords {summary['unique_keywords']:,} / "
        f"topics {summary['unique_topics']:,} / "
        f"concepts {summary['unique_concepts']:,}",
        file=report_stream,
    )
    print(
        f"  월 범위: {min(summary['months'], default='-')} ~ "
        f"{max(summary['months'], default='-')} ({len(summary['months'])}개월)",
        file=report_stream,
    )
    return 0


def _run_trend_normalize(args):
    """`paper-radar trend normalize` — papers_trend/normalize.py 의 main() 이식."""
    entries, alias_map = trend_normalize.load_lexicon()
    stopwords = trend_normalize.load_stopwords()
    record_list = trend_records.load_records(args.profile)
    normalized = trend_normalize.normalize_all(record_list, alias_map, entries, stopwords)

    print(f"[{args.profile}] 레코드 {len(record_list):,}건")
    print(f"  사전 항목 {len(entries)}개, 별칭 {len(alias_map)}개, 불용어 {len(stopwords)}개\n")

    print("=== 정규화 전/후 고유 표현 수 ===")
    for field, stat in trend_normalize.summarize(record_list, normalized).items():
        print(
            f"  {field:9} {stat['unique_before']:>6,} -> {stat['unique_after']:>6,}"
            f"  (감소 {stat['reduction']:,} / 사전 적중 {stat['lexicon_keys_hit']}개)"
        )

    print(f"\n=== 불용어·정규화 전 상위 {args.top} (keywords 원본) ===")
    for term, count in trend_normalize.top_raw_terms(record_list, "keywords", args.top):
        print(f"  {count:>5}  {term}")

    print(f"\n=== 불용어·정규화 후 상위 {args.top} (keywords) ===")
    for term, count in trend_normalize.top_terms(normalized, "keywords_norm", args.top):
        print(f"  {count:>5}  {term}")
    return 0


def _run_trend_aggregate(args):
    """`paper-radar trend aggregate` — papers_trend/aggregate.py 의 main() 이식
    + T15 의 --provider 라우팅.

    CensusError 를 여기서 잡아 기존 SystemExit 과 같은 메시지를 stderr 로
    내고 exit code 1 로 옮긴다 — trend.aggregate.census_guard() 의 docstring
    참조(라이브러리가 프로세스를 직접 죽이지 않는 이유). mesh_aggregate.py
    는 이 census_guard()/CensusError 를 그대로 재사용하므로 여기서 잡는
    예외 타입은 provider 와 무관하게 하나(trend_aggregate.CensusError)다.
    """
    if args.provider == "pubmed":
        try:
            result = trend_mesh_aggregate.run(
                args.profile, out_dir=args.out, allow_sample=args.allow_sample
            )
        except trend_mesh_aggregate.CensusError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"[{args.profile}/pubmed] 레코드 {result['records']:,}건, {result['months']}개월")
        for name, count in result["written"].items():
            print(f"  {name:26} {count:>7,}행")
        print(f"  provisional (색인 미완 추정): {', '.join(result['provisional']) or '없음'}")
        print(f"  low_sample: {', '.join(result['low_sample']) or '없음'}")
        return 0

    try:
        result = trend_aggregate.run(args.profile, out_dir=args.out, allow_sample=args.allow_sample)
    except trend_aggregate.CensusError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"[{args.profile}] 레코드 {result['records']:,}건, {result['months']}개월")
    for name, count in result["written"].items():
        print(f"  {name:26} {count:>7,}행")
    print(f"  provisional (색인 미완 추정): {', '.join(result['provisional']) or '없음'}")
    print(f"  low_sample: {', '.join(result['low_sample']) or '없음'}")

    classes = defaultdict(int)
    for row in result["metrics"]:
        classes[row["trend_class"]] += 1
    print(f"  trend_class: {dict(sorted(classes.items()))}")
    return 0


def _run_trend_unmatched(args):
    """`paper-radar trend unmatched` — papers_trend/unmatched.py 의 main() 이식."""
    result = trend_unmatched.run(args.profile, field=args.field, top=args.top, out_path=args.out)
    rows = result["rows"]
    print(
        f"[{args.profile}] {args.field} 미매칭 표현 {result['total_terms']:,}종, "
        f"등장 {result['total_hits']:,}회"
    )
    print(f"  -> {result['target']} ({len(rows):,}행)")
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


def _run_trend_overlap(args):
    """`paper-radar trend overlap` — trend.overlap.run() 호출 -> 출력 (T15).

    한쪽(또는 양쪽) provider 의 raw 가 없으면 trend.overlap.MissingRawError
    를 잡아 안내만 하고 exit 0 으로 옮긴다 — 이 진단은 옵션이다(모듈
    docstring 참고). CensusError 와 달리 exit 1 이 아니다: "아직 pubmed 를
    수집하지 않았다"는 실패가 아니라 "아직 안 했다"는 정상 상태다.
    """
    try:
        result = trend_overlap.run(args.profile, out_dir=args.out)
    except trend_overlap.MissingRawError as exc:
        print(str(exc))
        return 0

    rows = result["rows"]
    total_both = sum(row["both_by_doi"] for row in rows)
    total_oa_only = sum(row["openalex_only"] for row in rows)
    total_pm_only = sum(row["pubmed_only"] for row in rows)
    total_missing = sum(row["pubmed_doi_missing"] for row in rows)
    print(f"[{args.profile}] {len(rows)}개월 대조 -> {result['target']} ({result['written']:,}행)")
    print(
        f"  both_by_doi 합 {total_both:,} / openalex_only 합 {total_oa_only:,} /"
        f" pubmed_only 합 {total_pm_only:,} / pubmed_doi_missing 합 {total_missing:,}"
    )
    return 0


def _run_trend_suggest(args):
    """`paper-radar trend suggest` — trend.suggest.run() 호출 -> 출력.

    실제 대조·CSV 생성은 trend.suggest.run() 이 한다. ingredient 테이블이
    비어 있으면(resolve/import-cosing 을 아직 안 돌린 경우) 오류가 아니라
    안내만 하고 exit 0(브리핑 지시) — 빈 파일도 만들지 않는다.
    """
    result = trend_suggest.run(args.profile, args.db, out_dir=args.out or trend_suggest.OUT_DIR)
    if result["ingredient_table_empty"]:
        print(
            "ingredient 테이블이 비어 있습니다 — paper-radar ingredient resolve /"
            " import-cosing 을 먼저 실행하세요"
        )
        return 0

    print(
        f"[{args.profile}] unmatched {result['reviewed']:,}행 검토, "
        f"제안 {result['suggested']:,}건"
    )
    print(f"  -> {result['target']}")
    print("  verdict 컬럼을 사람이 채운다 — 제안은 결정이 아니다\n")
    return 0


def _run_trials_collect(args):
    print(f"검색: {args.query!r}  상한: {args.limit}건")

    def progress(index, total, trial):
        title = (trial.title or "(제목 없음)")[:60]
        print(
            f"  [{index}/{total}] {trial.nct_id} "
            f"({trial.phase or '-'}, {trial.sponsor_class or '-'})  {title}"
        )

    conn = repository.connect(args.db)
    try:
        report = trials_collect.run(
            conn, Transport(), args.query, args.limit, on_progress=progress
        )
    finally:
        conn.close()

    print(f"\n저장 완료: {len(report.records)}건 -> {args.db}")
    # "ok"/"partial" 판정은 trials.collect.run() 한 곳에서만 계산한다(단일
    # 출처) — CLI 는 그 결과(report.status)를 그대로 exit code 로 옮길 뿐,
    # 여기서 stopped_reason 을 다시 훑어 재계산하지 않는다.
    return 0 if report.status == "ok" else 1


def _run_trials_list(args):
    conn = repository.connect(args.db)
    try:
        found = repository.search_trials(conn, args.keyword, limit=args.limit)
    finally:
        conn.close()

    if not found:
        print("결과가 없습니다.")
        return 0

    print(f"'{args.keyword}' 관련 {len(found)}건\n")
    for trial in found:
        print(f"[{trial.nct_id}] {trial.title or '(제목 없음)'}")
        print(
            f"    {trial.status or '상태 미상'} / {trial.phase or '-'} / "
            f"{trial.sponsor_class or '-'} / "
            f"등록 {trial.enrollment if trial.enrollment is not None else '미공개'}명"
        )
        if trial.results_posted:
            print("    결과 게시됨")
        print(f"    {trial.url}")
        print()
    return 0


def _run_ingredient_resolve(args):
    """`paper-radar ingredient resolve` — ingredients.resolve.run() 호출 -> 출력.

    실제 조회·RunLog 자기기록은 ingredients.resolve.run() 이 한다(trials
    collect 와 대칭). NotFound 는 run() 안에서 이미 흡수돼 report.record=None
    으로 온다 — "PubChem 에 없는 이름"은 실패가 아니므로 exit 0(브리핑 지시).
    """
    conn = repository.connect(args.db)
    try:
        report = ingredients_resolve.run(conn, Transport(), args.name)
    finally:
        conn.close()

    if report.record is None:
        print("PubChem 에 없는 이름입니다")
        return 0

    record = report.record
    print(f"저장 완료 -> {args.db}")
    print(f"  name_key: {record.name_key}")
    print(f"  CID: {record.cid}")
    print(f"  CAS: {record.cas or '미상'}")
    print(f"  동의어 {len(record.synonyms)}개")
    print(f"  출처: {', '.join(record.sources)}")
    return 0


def _run_ingredient_show(args):
    """`paper-radar ingredient show` — 로컬 DB 만 읽는다(네트워크 없음)."""
    conn = repository.connect(args.db)
    try:
        record = repository.get_ingredient(conn, pubchem.name_key(args.name))
    finally:
        conn.close()

    if record is None:
        print("결과가 없습니다.")
        return 0

    preview = ", ".join(record.synonyms[:10])
    more = f" 외 {len(record.synonyms) - 10}개" if len(record.synonyms) > 10 else ""
    print(f"name_key: {record.name_key}")
    print(f"  INCI: {record.inci_name or '미상(CosIng 미조인)'}")
    print(f"  CID: {record.cid if record.cid is not None else '미상'}")
    print(f"  CAS: {record.cas or '미상'}")
    print(f"  동의어({len(record.synonyms)}개): {preview}{more}")
    print(f"  출처: {', '.join(record.sources)}")
    print(f"  갱신: {record.fetched_at}")
    return 0


def _run_ingredient_import_cosing(args):
    """`paper-radar ingredient import-cosing` — ingredients.import_cosing.run() 호출 -> 출력.

    실제 파싱·merge upsert·RunLog 자기기록은 ingredients.import_cosing.run()
    이 한다(ingredient resolve/trials collect 와 대칭). 네트워크가 없으므로
    Transport 를 만들지 않는다.
    """
    conn = repository.connect(args.db)
    try:
        report = ingredients_import_cosing.run(conn, args.path)
    finally:
        conn.close()

    print(f"CosIng 임포트 완료: {args.path} -> {args.db}")
    print(f"  읽은 행: {report.read}건")
    print(f"  저장한 레코드: {report.saved}건")
    print(f"  건너뛴 행: {report.skipped}건 (INCI name 없음)")
    return 0


COMMANDS = {"collect": _run_collect, "trend": _run_trend, "cite": _run_cite}
TREND_COMMANDS = {
    "collect": _run_trend_collect,
    "records": _run_trend_records,
    "normalize": _run_trend_normalize,
    "aggregate": _run_trend_aggregate,
    "unmatched": _run_trend_unmatched,
    "overlap": _run_trend_overlap,
    "suggest": _run_trend_suggest,
}
TRIALS_COMMANDS = {"collect": _run_trials_collect, "list": _run_trials_list}
INGREDIENT_COMMANDS = {
    "resolve": _run_ingredient_resolve,
    "show": _run_ingredient_show,
    "import-cosing": _run_ingredient_import_cosing,
}
GROUPS = {
    "evidence": COMMANDS,
    "trend": TREND_COMMANDS,
    "trials": TRIALS_COMMANDS,
    "ingredient": INGREDIENT_COMMANDS,
}


def run(args):
    return GROUPS[args.group][args.command](args)


def main(argv=None):
    load_dotenv()
    return run(parse_args(argv))
