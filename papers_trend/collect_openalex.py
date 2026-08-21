"""1단계: OpenAlex 전수 수집. 네트워크를 만지는 유일한 단계다.

    python -m papers_trend.collect_openalex --profile sunscreen
    python -m papers_trend.collect_openalex --profile all --dry-run

설계 방침
  1. 스키마는 나중에 정한다 -> API 응답 원본(raw)을 그대로 보존한다.
     지금 필드를 골라내면 나중에 되돌릴 수 없다. 추출은 records.py 가 한다.
  2. 정렬로 모집단을 자르지 않는다. 커서로 전건을 받으므로 정렬 순서는
     결과에 영향을 주지 않는다. config 의 limit 을 쓰면 그건 표본이고,
     _meta.json 의 is_census 가 false 로 기록된다.
  3. 중단/재개를 전제로 한다. 커서 상태를 파일에 남겨 재실행 시 이어간다.
  4. 중복 제거는 openalex_id 기준이다. DOI 없는 논문이 존재하므로
     DOI 를 1차 키로 쓰지 않는다.
  5. OpenAlex 2026-02-13부터 계량제. mailto/polite pool 은 폐지돼 요청 파라미터로
     보내지 않는다 (User-Agent 는 유지 — Crossref 등 다른 소스용 설정 표면).
     api_key 가 있으면 무인증 1,000크레딧/일 대신 100,000크레딧/일을 쓴다.
     402/409 또는 잔량 0 은 예산 소진으로 보고 재시도 없이 커서를 저장하고 멈춘다.

OpenAlex 응답 구조 실측: 2026-08-20
  top-level 50개 필드. keywords / topics / concepts / primary_topic 모두 존재하며
  전부 list[dict] (primary_topic 만 dict).
    keywords[]  = {id, display_name, score}
    topics[]    = {id, display_name, score, subfield, field, domain}
    concepts[]  = {id, display_name, score, level, wikidata}
  표본 400건에서 keywords 의 display_name 집합은 항상 concepts 의 부분집합이었다
  (100%, 그중 27%는 완전 동일). 즉 keywords 는 자유 서술 필드가 아니라
  concepts 를 걸러낸 것이다. 그래도 topics(평균 2.8개)보다 해상도가 높다
  (평균 10.5개)는 점은 유효하다.
  publication_date 는 400/400 이 YYYY-MM-DD 완전 정밀도였다.
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE = "https://api.openalex.org/works"
USER_AGENT = "cosmetics-papers-trend/0.1"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
RAW_DIR = HERE / "raw"

MAX_RETRIES = 8
MAX_SLEEP = 60.0
BASE_BACKOFF = 2.0
MIN_INTERVAL = 0.15
RETRY_STATUS = {429, 500, 502, 503, 504}
# OpenAlex 2026-02-13부터 계량제. 402/409 는 일일 예산 소진 추정이고
# UTC 자정에나 초기화되므로 재시도해도 무의미하다 — 즉시 중단한다.
BUDGET_STATUS = {402, 409}


def warn(message):
    print(f"[warn] {message}", file=sys.stderr)


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def build_filter(query, window):
    return ",".join(
        [
            f"title_and_abstract.search:{query}",
            f"from_publication_date:{window['from']}",
            f"to_publication_date:{window['to']}",
        ]
    )


def make_session(mailto):
    session = requests.Session()
    agent = f"{USER_AGENT} (mailto:{mailto})" if mailto else USER_AGENT
    session.headers["User-Agent"] = agent
    return session


def retry_delay(response, attempt):
    """Retry-After 가 지수 백오프를 이긴다. OpenAlex 는 39~40초를 준다."""
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return min(float(header), MAX_SLEEP)
        except TypeError, ValueError:
            pass
    return min(BASE_BACKOFF * (2**attempt), MAX_SLEEP)


def parse_budget_remaining(headers):
    """x-ratelimit-remaining 헤더를 정수로. 없거나 파싱 불가면 None. (순수 함수)"""
    if not headers:
        return None
    value = headers.get("x-ratelimit-remaining")
    if value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def determine_stopped_reason(*, budget_exhausted, hit_max_pages):
    """중단 사유를 우선순위대로 정한다. 정상 완료(또는 그 외 실패)면 None. (순수 함수)

    우선순위: 예산 소진이 --max-pages 보다 앞선다 — 둘 다 해당해도 원인은
    예산이지, 마침 그 페이지에서 상한에 닿은 것이 아니기 때문이다.
    """
    if budget_exhausted:
        return "budget_exhausted"
    if hit_max_pages:
        return "max_pages"
    return None


def get_page(session, params, api_key):
    """한 페이지 요청과 예산 정보를 함께 돌려준다. 예외를 밖으로 던지지 않는다.

    반환: (payload, budget) 튜플.
      payload: 성공 시 파싱된 JSON, 최종 실패나 예산 소진이면 None.
      budget: {"remaining": int | None, "exhausted": bool}.
        exhausted 는 402/409 응답이거나 remaining 이 0 이 됐을 때 True.
        mailto 는 더 이상 요청 파라미터로 보내지 않는다 — 2026-02-13부터
        OpenAlex 가 mailto/polite pool 자체를 폐지해 무효한 값이 됐다.
    """
    if api_key:
        params = dict(params, api_key=api_key)
    budget = {"remaining": None, "exhausted": False}
    for attempt in range(MAX_RETRIES):
        time.sleep(MIN_INTERVAL)
        try:
            response = session.get(BASE, params=params, timeout=90)
        except requests.RequestException as exc:
            warn(f"요청 실패 ({exc})")
            time.sleep(retry_delay(None, attempt))
            continue

        observed = parse_budget_remaining(response.headers)
        if observed is not None:
            budget["remaining"] = observed

        if response.status_code in BUDGET_STATUS:
            warn(
                f"{response.status_code} — 일일 예산 소진 추정 (UTC 자정 초기화). "
                "재시도하지 않습니다."
            )
            budget["exhausted"] = True
            return None, budget

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                warn("JSON 파싱 실패")
                return None, budget
            if budget["remaining"] == 0:
                budget["exhausted"] = True
            return payload, budget

        if response.status_code in RETRY_STATUS:
            delay = retry_delay(response, attempt)
            warn(f"{response.status_code}, {delay:.0f}초 후 재시도 ({attempt + 1}/{MAX_RETRIES})")
            time.sleep(delay)
            continue
        warn(f"예상치 못한 상태 {response.status_code}: {response.text[:200]}")
        return None, budget
    warn(f"{MAX_RETRIES}회 재시도 실패")
    return None, budget


def total_count(session, query, window, api_key):
    payload, _ = get_page(
        session,
        {
            "filter": build_filter(query, window),
            "select": "id",
            "per-page": 1,
        },
        api_key,
    )
    return ((payload or {}).get("meta") or {}).get("count")


# --- 재개 상태 -------------------------------------------------------------


def profile_dir(query_id):
    return RAW_DIR / query_id


def state_path(query_id):
    return profile_dir(query_id) / "_state.json"


def load_state(query_id):
    path = state_path(query_id)
    if not path.exists():
        return {"cursor": "*", "pages": 0, "written": 0}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError, ValueError:
        warn(f"{query_id}: 상태 파일을 읽을 수 없어 처음부터 시작합니다")
        return {"cursor": "*", "pages": 0, "written": 0}


def save_state(query_id, state):
    profile_dir(query_id).mkdir(parents=True, exist_ok=True)
    with open(state_path(query_id), "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)


def seen_ids(query_id):
    """이미 받아둔 openalex_id 집합. 재개 시 중복을 막는다."""
    found = set()
    directory = profile_dir(query_id)
    if not directory.exists():
        return found
    for path in sorted(directory.glob("*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    identifier = json.loads(line).get("id")
                except ValueError:
                    continue
                if identifier:
                    found.add(identifier)
    return found


def jsonl_path(query_id, stamp=None):
    stamp = stamp or date.today().isoformat()
    return profile_dir(query_id) / f"{stamp}.jsonl"


def write_meta(query_id, meta):
    profile_dir(query_id).mkdir(parents=True, exist_ok=True)
    with open(profile_dir(query_id) / "_meta.json", "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=1)


# --- 수집 ------------------------------------------------------------------


def read_meta(query_id):
    path = profile_dir(query_id) / "_meta.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError, ValueError:
        return {}


def collect_profile(
    query_id, query, config, mailto, session=None, verbose=True, max_pages=None, api_key=None
):
    """전건을 raw/{query_id}/{YYYY-MM-DD}.jsonl 에 무손실 추가한다."""
    session = session or make_session(mailto)
    window = config["window"]
    limit = config.get("limit")
    per_page = config.get("per_page", 200)

    state = load_state(query_id)
    previous = read_meta(query_id)

    expected = total_count(session, query, window, api_key)
    if expected is None:
        # 커서가 남아 있으면 건수를 못 세도 이어갈 수 있다. 직전 실행이 기록해 둔
        # 기대값을 쓴다. 이것이 없을 때만 포기한다.
        expected = previous.get("expected_from_api")
        if expected is None:
            warn(f"{query_id}: 전체 건수를 확인할 수 없고 직전 기록도 없어 중단합니다")
            return None
        warn(f"{query_id}: 건수 조회 실패. 직전 기록의 {expected:,}건을 목표로 이어갑니다")
    already = seen_ids(query_id)
    if verbose:
        print(
            f"[{query_id}] 대상 {expected:,}건"
            + (f" / 상한 {limit:,}" if limit else " (전수)")
            + (f" / 이미 {len(already):,}건 보유, 이어서 수집" if already else "")
        )

    target = min(expected, limit) if limit else expected
    path = jsonl_path(query_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    new_records = 0
    duplicates = 0
    pages_this_run = 0
    cursor = state.get("cursor") or "*"
    started = time.monotonic()
    stopped_early = False
    hit_max_pages = False
    budget_exhausted = False

    with open(path, "a", encoding="utf-8") as handle:
        while cursor and len(already) < target:
            if max_pages and pages_this_run >= max_pages:
                stopped_early = True
                hit_max_pages = True
                if verbose:
                    print(
                        f"  --max-pages {max_pages} 에 도달. 커서를 저장하고 멈춥니다."
                        " 다시 실행하면 이어집니다."
                    )
                break
            payload, budget = get_page(
                session,
                {
                    "filter": build_filter(query, window),
                    "per-page": per_page,
                    "cursor": cursor,
                },
                api_key,
            )
            if not payload:
                stopped_early = True
                if budget["exhausted"]:
                    budget_exhausted = True
                    if verbose:
                        print(
                            "  예산 소진 추정. 커서를 저장하고 멈춥니다."
                            " UTC 자정 이후 다시 실행하면 이어집니다."
                        )
                else:
                    warn(
                        f"{query_id}: 페이지 수집 실패. 커서를 저장하고 멈춥니다."
                        " 잠시 뒤 다시 실행하면 이어집니다."
                    )
                break
            results = payload.get("results") or []
            if not results:
                cursor = None
                break
            for work in results:
                identifier = work.get("id")
                if not identifier:
                    continue
                if identifier in already:
                    duplicates += 1
                    continue
                already.add(identifier)
                handle.write(json.dumps(work, ensure_ascii=False) + "\n")
                new_records += 1
                if limit and len(already) >= target:
                    break
            handle.flush()
            pages_this_run += 1
            state["pages"] = state.get("pages", 0) + 1
            state["written"] = len(already)
            cursor = (payload.get("meta") or {}).get("next_cursor")
            state["cursor"] = cursor
            save_state(query_id, state)
            if verbose:
                elapsed = time.monotonic() - started
                remaining_note = (
                    f"  예산잔량 {budget['remaining']:,}" if budget["remaining"] is not None else ""
                )
                print(
                    f"  {len(already):,}/{target:,}  "
                    f"({state['pages']}페이지, {elapsed:.0f}초){remaining_note}",
                    flush=True,
                )
            if budget["exhausted"]:
                stopped_early = True
                budget_exhausted = True
                if verbose:
                    print(
                        "  예산 소진 추정 (잔량 0). 커서를 저장하고 멈춥니다."
                        " UTC 자정 이후 다시 실행하면 이어집니다."
                    )
                break

    is_census = not limit and cursor is None and not stopped_early
    meta = {
        "query_id": query_id,
        "query": query,
        "window": window,
        "expected_from_api": expected,
        "collected": len(already),
        "new_this_run": new_records,
        "duplicates_skipped": duplicates,
        "pages": state.get("pages", 0),
        "pages_this_run": pages_this_run,
        "stopped_early": stopped_early,
        "stopped_reason": determine_stopped_reason(
            budget_exhausted=budget_exhausted,
            hit_max_pages=hit_max_pages,
        ),
        "remaining_estimate": max(0, (expected or 0) - len(already)),
        "per_page": per_page,
        "limit": limit,
        "is_census": is_census,
        "census_note": (
            "전수. 정렬 순서는 결과에 영향을 주지 않는다."
            if is_census
            else "표본. limit 또는 중단으로 전건을 받지 않았으므로 OpenAlex 기본 정렬"
            " (relevance_score) 이 어떤 논문이 포함됐는지를 결정한다."
            " 비율 지표를 모집단 비율로 해석하지 말 것."
        ),
        "collected_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "openalex_fields_verified_on": "2026-08-20",
    }
    write_meta(query_id, meta)
    if verbose:
        print(
            f"[{query_id}] 완료: 신규 {new_records:,}건, 누적 {len(already):,}건, "
            f"중복 {duplicates:,}건 -> {path}"
        )
        if not is_census:
            warn(f"{query_id}: 전수가 아닙니다. {meta['census_note']}")
    return meta


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m papers_trend.collect_openalex",
        description="OpenAlex 전수 수집. 원본 JSONL 만 남기고 가공하지 않는다.",
    )
    parser.add_argument("--profile", default="all", help="config.json 의 프로파일 이름, 또는 all")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument(
        "--dry-run", action="store_true", help="건수와 예상 요청 수만 출력하고 수집하지 않는다"
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="이번 실행에서 받을 페이지 상한. 커서가 저장되므로 여러 번 나눠 전수를 채울 수 있다",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    config = load_config(args.config)
    mailto = os.environ.get(config.get("mailto_env", "OPENALEX_EMAIL"), "").strip()
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not api_key:
        warn(
            "OPENALEX_API_KEY 가 없어 무인증 예산(하루 1,000크레딧)으로 동작합니다. "
            "목록 호출은 페이지당 10크레딧이므로 하루 약 100페이지가 상한입니다."
        )

    profiles = config["profiles"]
    wanted = list(profiles) if args.profile == "all" else [args.profile]
    unknown = [name for name in wanted if name not in profiles]
    if unknown:
        parser.error(
            f"config 에 없는 프로파일: {', '.join(unknown)}. 사용 가능: {', '.join(profiles)}"
        )

    session = make_session(mailto)
    per_page = config.get("per_page", 200)
    for query_id in wanted:
        query = profiles[query_id]["query"]
        if args.dry_run:
            count = total_count(session, query, config["window"], api_key)
            if count is None:
                print(f"[{query_id}] 건수 확인 실패")
                continue
            print(f"[{query_id}] {count:,}건 -> 예상 요청 {count // per_page + 1}회")
            continue
        collect_profile(
            query_id,
            query,
            config,
            mailto,
            session=session,
            max_pages=args.max_pages,
            api_key=api_key,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
