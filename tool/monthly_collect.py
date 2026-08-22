"""월 1회 cron 주기 수집 진입점 (항목3, 주기 수집 브리핑).

`paper-radar` 를 서브프로세스로 부르지 않고 paper_radar.cli 를 import 해서
`cli.run(cli.parse_args([...]))` 로 직접 호출한다 — exit code 를 프로세스
문자열 파싱 없이 정수로 그대로 받을 수 있고, 이 스크립트 자체의
단위 테스트(계획 구성/exit code 집계)도 서브프로세스를 띄우지 않고 순수
함수로 검증할 수 있다(tests/paper_radar/test_tool_monthly_collect.py 참고).
cli.parse_args() 가 잘못된 인자에 argparse.error() -> SystemExit 로 반응하는
경로가 있어(우리가 만든 argv 는 정상이어야 하지만, 방어적으로) run_step()
이 SystemExit 를 잡아 exit code 로 옮긴다.

동작 순서
    1. 오늘(UTC) 기준 "직전 완료 월의 말일"을 계산한다(previous_completed_month_end).
       예: 2026-08-22 실행 -> "2026-07-31"(이번 달은 아직 안 끝났으므로 뺀다).
    2. 프로파일 x 프로바이더마다 `trend collect --window-to <계산값>` 을 돌린다.
    3. 이어서 프로파일마다 `trend aggregate`(openalex 는 항상, pubmed 는
       --providers 에 pubmed 가 있을 때만) 와 `trend overlap` 을 돌린다.
    4. --skip-trials 가 없으면 프로파일마다 `trials collect` 를 1회씩 돌린다.
       trials collect 는 --profile 이 아니라 --query 하나만 받는다(cli.py 의
       trials_collect_parser 참고) — 여러 프로파일에 그대로 대응시킬 표준
       쿼리가 config 에 없으므로(trend 프로파일의 query 는 OpenAlex 표현이라
       ClinicalTrials.gov 검색에 그대로 맞는다는 보장이 없다), 이 태스크에서는
       가장 단순하고 예측 가능한 선택으로 프로파일 이름 자체를 검색어로
       쓴다(예: "sunscreen"). 더 정교한 쿼리 매핑이 필요해지면 config.json
       에 프로파일별 trials_query 를 추가하는 식으로 나중에 확장한다.
    5. 각 단계의 exit code 를 모아 마지막에 요약 표를 찍고, 하나라도 0 이
       아니면(그리고 dry-run 으로 건너뛴 단계가 아니면) 스크립트 자체가 exit 1.
    6. --dry-run 이면 각 단계의 전체 명령줄을 출력만 한다 — 다만 `trend
       collect` 단계는 그 자체의 --dry-run(건수 조회만, 수집 없음)을 붙여
       실제로 실행한다. 그래야 이번 달 신규 건수를 사람이 미리 볼 수 있다.
       aggregate/overlap/trials 는 아직 없는 데이터에 대한 명령이라 dry-run
       에서는 명령줄만 보여주고 실행하지 않는다.

cron 등록 예시 (매월 3일 03:17 UTC)
    17 3 3 * * cd /path/to/Research_Paper && \\
        /usr/bin/env uv run python tool/monthly_collect.py \\
        >> logs/monthly_collect.log 2>&1
    분을 정각(00)이 아니라 17로 어긋나게 잡은 것은 같은 호스트의 다른 정시
    cron 작업들과 몰리지 않게 하려는 것이다(OpenAlex/PubMed 예산이 같은
    시각에 여러 작업과 경합하지 않도록). 매달 1~2일이 아니라 3일로 잡은
    것은 전달 말일 데이터가 소스 쪽에 완전히 반영될 여유를 하루이틀 더
    주기 위함이다.

이 스크립트는 네트워크를 쓴다(trend collect/trials collect 가 실제 API 를
부른다) — `uv run pytest`(testpaths=tests/) 는 tool/ 아래를 전혀 건드리지
않으므로(tool/live_smoke.py, tool/fetch_cosing.py 와 같은 배치) 기본 테스트
스위트는 여전히 오프라인이다. 이 태스크에서는 이 스크립트를 실제로
실행하지 않는다(브리핑 제약) — 순수 함수(previous_completed_month_end/
compute_steps/overall_exit_code)에 대한 단위 테스트로만 검증한다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from paper_radar import cli
from paper_radar.trend import collect as trend_collect

# 이 단계 종류들만 --db 를 받는다(cli.py 서브파서 정의 기준 — trend aggregate/
# overlap 은 --db 인자가 아예 없다. 없는 인자를 argv 에 넣으면 argparse 가
# "unrecognized arguments" 로 죽는다).
_ACCEPTS_DB_FLAG = frozenset({"trend_collect", "trials_collect"})


def previous_completed_month_end(today):
    """today(date) 기준 "직전 완료 월"의 말일을 "YYYY-MM-DD" 로 돌려준다. (순수 함수)

    이번 달은 아직 끝나지 않았으므로 창 끝에서 뺀다 — 이번 달분 데이터가
    소스(OpenAlex/PubMed)에 아직 다 반영되지 않았을 수 있다(부분 월).
    이번 달 1일로 돌아간 뒤 하루를 빼면 항상 전달 말일이 된다 — 1월(연
    경계)이든 30일/31일/28일(윤년 포함) 짜리 달이든 달력 계산을 직접
    하지 않고 date 산술만으로 맞다.
    """
    first_of_this_month = today.replace(day=1)
    last_day_of_previous_month = first_of_this_month - timedelta(days=1)
    return last_day_of_previous_month.isoformat()


def compute_steps(profiles, providers, window_to, *, skip_trials, pubmed_capable=None):
    """실행할 단계 목록을 순서대로 구성한다. (순수 함수 — 아무것도 실행하지 않는다)

    각 단계는 {"kind": ..., "argv": [...]} — argv 는 paper_radar.cli.parse_args()
    에 그대로 넘길 수 있는 서브커맨드 인자 목록이다("paper-radar" 자체는
    포함하지 않는다). "skipped" 종류는 argv 가 없다 — run_plan() 이 이를
    실행 없이 요약에만 한 줄 남긴다. 순서: 항목3 브리핑 "동작 순서" 그대로
    — collect 전부 -> (aggregate, overlap) 프로파일별 -> trials(옵션).

    pubmed_capable: config.json 에 pubmed_query 가 있는 프로파일 이름의
    집합. None 이면(테스트 편의용 기본값) 모든 프로파일을 pubmed-capable
    로 본다 — 실제 main() 은 항상 config 를 읽어 이 값을 명시적으로 넘긴다
    (_pubmed_capable_profiles() 참고). 리뷰 Finding3(실행에서 실제로 드러남):
    이 값을 무시하면 pubmed_query 가 없는 프로파일(예: cosmetics)에도
    `trend aggregate --provider pubmed` 단계를 계획에 넣어, 존재하지 않는
    raw 를 집계하려다 실패해 요약 표에 설명되지 않는 실패가 늘어난다 —
    그 프로파일의 pubmed 관련 단계(collect·aggregate 둘 다)를 계획에서
    빼고, "건너뜀(pubmed_query 없음)" 한 줄로 대신한다.
    """
    pubmed_capable = set(profiles) if pubmed_capable is None else set(pubmed_capable)
    steps = []

    for profile in profiles:
        for provider in providers:
            if provider == "pubmed" and profile not in pubmed_capable:
                # collect 뿐 아니라 그 아래 aggregate(pubmed) 단계도 함께
                # 건너뛴다(이 profile 은 애초에 pubmed raw 가 생기지 않는다) —
                # 두 번째 루프에서 pubmed_capable 를 다시 확인해 중복 없이 뺀다.
                steps.append(
                    {"kind": "skipped", "note": f"{profile}/pubmed 건너뜀(pubmed_query 없음)"}
                )
                continue
            steps.append(
                {
                    "kind": "trend_collect",
                    "argv": [
                        "trend",
                        "collect",
                        "--profile",
                        profile,
                        "--provider",
                        provider,
                        "--window-to",
                        window_to,
                    ],
                }
            )

    for profile in profiles:
        steps.append(
            {"kind": "trend_aggregate", "argv": ["trend", "aggregate", "--profile", profile]}
        )
        if "pubmed" in providers and profile in pubmed_capable:
            steps.append(
                {
                    "kind": "trend_aggregate",
                    "argv": [
                        "trend",
                        "aggregate",
                        "--profile",
                        profile,
                        "--provider",
                        "pubmed",
                    ],
                }
            )
        steps.append(
            {"kind": "trend_overlap", "argv": ["trend", "overlap", "--profile", profile]}
        )

    if not skip_trials:
        for profile in profiles:
            # trials collect 쿼리 선택 근거는 모듈 docstring "동작 순서 4" 참고
            # — 프로파일 이름을 그대로 쓴다.
            steps.append(
                {"kind": "trials_collect", "argv": ["trials", "collect", "--query", profile]}
            )

    return steps


def _display(argv):
    return "paper-radar " + " ".join(argv)


def run_step(argv):
    """paper_radar.cli 를 직접 호출해 exit code 를 정수로 돌려준다.

    cli.parse_args() 가 argparse.error() 로 SystemExit 을 던지는 경로가
    있다(모듈 docstring 참고) — 우리가 구성한 argv 는 정상이어야 하지만
    방어적으로 잡아 정수 exit code 로 옮긴다.
    """
    try:
        return cli.run(cli.parse_args(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def run_plan(steps, *, dry_run, db_path=None):
    """계획된 단계를 순서대로 실행(또는 dry-run 출력)한다.

    돌려주는 값: [(표시용 명령줄, exit_code), ...]. exit_code 는 실제 정수
    exit code 이거나, 실행하지 않은 단계를 나타내는 두 개의 구분된 표식
    중 하나다 — None(dry-run 이라 아직 없는 데이터를 다루는 단계를 건너뜀)
    과 "skipped"(compute_steps() 가 애초에 이 창에서는 실행 대상이 아니라고
    표시한 단계, 예: pubmed_query 없는 프로파일 — 리뷰 Finding3). 요약
    표에서 서로 다른 사유로 보이도록 값을 구분해 둔다. overall_exit_code()
    는 둘 다 실패로 세지 않는다.
    """
    results = []
    for step in steps:
        if step["kind"] == "skipped":
            print(f"[건너뜀] {step['note']}")
            results.append((step["note"], "skipped"))
            continue

        argv = list(step["argv"])
        if db_path and step["kind"] in _ACCEPTS_DB_FLAG:
            argv = argv + ["--db", db_path]

        if dry_run:
            print(f"[dry-run] {_display(argv)}")
            if step["kind"] == "trend_collect":
                # brief: trend collect 의 --dry-run(건수 조회만)까지 전달한다 —
                # 이번 달 신규 건수를 사람이 미리 볼 수 있게.
                exit_code = run_step(argv + ["--dry-run"])
                results.append((_display(argv), exit_code))
            else:
                # 아직 없는 데이터를 다루는 명령(aggregate/overlap/trials)이라
                # dry-run 에서는 실행하지 않는다.
                results.append((_display(argv), None))
            continue

        exit_code = run_step(argv)
        results.append((_display(argv), exit_code))
    return results


def overall_exit_code(results):
    """results([(명령줄, exit_code), ...])에서 스크립트 전체의 exit code 를 정한다. (순수 함수)

    exit_code 가 None(dry-run 으로 건너뛴 단계) 이거나 "skipped"(compute_steps()
    가 애초에 이 실행 대상이 아니라고 표시한 단계 — 리뷰 Finding3) 이거나
    0 이면 성공으로 본다. 하나라도 그 외의 값이면(부분/실패) 전체를 1 로
    묶는다 — 어떤 단계가 실패했는지는 요약 표에서 사람이 본다.
    """
    return 1 if any(code not in (0, None, "skipped") for _, code in results) else 0


def _print_summary(results):
    print("\n=== 실행 요약 ===")
    for display, code in results:
        if code is None:
            status = "dry-run"
        elif code == "skipped":
            status = "건너뜀"
        elif code == 0:
            status = "ok"
        else:
            status = f"실패(exit {code})"
        print(f"  [{status:>10}] {display}")


def _pubmed_capable_profiles(config):
    """config["profiles"] 중 pubmed_query 가 있는 프로파일 이름의 집합.

    리뷰 Finding3(실행에서 실제로 드러남): pubmed_query 가 없는 프로파일
    (예: cosmetics)에 `trend collect --provider pubmed`/`trend aggregate
    --provider pubmed` 를 계획에 넣으면, collect 는 cli.py 가 이미 exit 1
    로 거부하지만(_run_trend_collect() 의 missing pubmed_query 체크)
    aggregate 는 그 체크가 없어 존재하지 않는 raw 를 집계하려다 그대로
    실패한다 — compute_steps() 가 이 집합을 받아 두 단계 모두 계획에서
    뺀다.
    """
    return {name for name, profile in config["profiles"].items() if "pubmed_query" in profile}


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="월 1회 주기 수집 진입점 — trend collect/aggregate/overlap"
        " (+옵션 trials collect) 를 순서대로 돌린다."
    )
    parser.add_argument(
        "--profiles",
        default=None,
        help="쉼표로 구분한 프로파일 이름 목록. 생략하면 config.json 의 전체 프로파일",
    )
    parser.add_argument(
        "--providers",
        default="openalex,pubmed",
        help="쉼표로 구분한 프로바이더 목록 (기본 openalex,pubmed)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="실행할 명령줄만 출력한다 (모듈 docstring 참고)"
    )
    parser.add_argument(
        "--skip-trials", action="store_true", help="trials collect 단계를 건너뛴다"
    )
    parser.add_argument("--db", default=None, help="SQLite 경로 (생략하면 각 하위 명령의 기본값)")
    parser.add_argument(
        "--today",
        default=None,
        help="창 끝 계산 기준일 YYYY-MM-DD (테스트 주입용, 생략하면 오늘/UTC)",
    )
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if args.today:
        today = datetime.strptime(args.today, "%Y-%m-%d").date()
    else:
        today = datetime.now(UTC).date()
    window_to = previous_completed_month_end(today)

    # config 를 한 번만 읽어 기본 프로파일 목록과 pubmed_capable 판정 둘 다에 쓴다
    # (리뷰 Finding3 — _pubmed_capable_profiles() 참고).
    config = trend_collect.load_config()
    profiles = args.profiles.split(",") if args.profiles else list(config["profiles"])
    providers = args.providers.split(",")
    pubmed_capable = _pubmed_capable_profiles(config)

    steps = compute_steps(
        profiles,
        providers,
        window_to,
        skip_trials=args.skip_trials,
        pubmed_capable=pubmed_capable,
    )
    print(f"window_to={window_to}  프로파일={profiles}  프로바이더={providers}")
    results = run_plan(steps, dry_run=args.dry_run, db_path=args.db)
    _print_summary(results)
    return overall_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
