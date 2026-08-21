"""1단계: OpenAlex 전수 수집. 네트워크를 만지는 유일한 단계다.

papers_trend/collect_openalex.py 를 재작성했다. 이 태스크(T6)에서 고친 것 넷:
    1. 자체 재시도/페이스/인증 루프를 버리고 paper_radar.transport.http.Transport
       + sources.openalex.OpenAlex.policy 로 위임한다("통합 transport"). 402/409
       는 transport.errors.BudgetExhausted 로 온다 — 재시도 없이 그 자리에서
       커서를 저장하고 멈춘다(get_page() 의 수동 상태 머신이 사라졌다).
    2. RunLog 에 이 실행 자체를 기록한다(evidence.pipeline.collect() 와 같은
       패턴: run.start() -> observer 로 log_fetch -> record_source -> finish).
       DB 연결은 run 기록 전용이다 — 이 모듈은 storage.repository.upsert() 를
       호출하지 않는다(트렌드 산출물은 CSV 지 DB 레코드가 아니다).
    3. provider 필드를 _meta.json 에 추가하고, raw 경로가
       data/raw/{provider}/{query_id}/ 로 바뀐다(trend.records 의 새 경로와
       같다).
    4. seen_ids() 의 전체 JSONL 재스캔을 사이드카 _ids.txt 로 대체한다 —
       있으면 그것만 읽고(재개가 O(1)), 없으면 하위호환으로 기존처럼 전체
       재스캔한 뒤 그 결과로 사이드카를 만든다.

설계 방침(legacy 계승)
  1. 스키마는 나중에 정한다 -> API 응답 원본(raw)을 그대로 보존한다. select
     파라미터를 쓰지 않는다 — evidence 용 sources.openalex.search() 는 select
     로 필드를 제한하지만, trend 는 아직 무엇이 필요할지 모르는 장기 보관용
     원자재라 원문 전체가 필요하다.
  2. 정렬로 모집단을 자르지 않는다. 커서로 전건을 받으므로 정렬 순서는
     결과에 영향을 주지 않는다. config 의 limit 을 쓰면 그건 표본이고,
     _meta.json 의 is_census 가 false 로 기록된다.
  3. 중단/재개를 전제로 한다. 커서 상태를 _state.json 에 남겨 재실행 시 이어간다.
  4. 중복 제거는 openalex_id(work id) 기준이다. DOI 없는 논문이 존재하므로
     DOI 를 1차 키로 쓰지 않는다.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime

from paper_radar.sources.openalex import BASE, OpenAlex
from paper_radar.storage import repository
from paper_radar.storage.runlog import RunLog
from paper_radar.transport.errors import BudgetExhausted, TransportError
from paper_radar.transport.http import Transport
from paper_radar.trend import records

CONFIG_PATH = records.HERE / "config.json"


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


def determine_stopped_reason(*, budget_exhausted, hit_max_pages):
    """중단 사유를 우선순위대로 정한다. 정상 완료(또는 그 외 실패)면 None. (순수 함수)

    우선순위: 예산 소진이 --max-pages 보다 앞선다 — 둘 다 해당해도 원인은
    예산이지, 마침 그 페이지에서 상한에 닿은 것이 아니기 때문이다.
    papers_trend/collect_openalex.py 의 동명 함수와 동일한 규칙이다.
    """
    if budget_exhausted:
        return "budget_exhausted"
    if hit_max_pages:
        return "max_pages"
    return None


def total_count(transport, query, window):
    """전체 건수. 조회 실패(어떤 사유든)는 None — 호출자가 직전 기록으로 대체한다."""
    params = [
        ("filter", build_filter(query, window)),
        ("select", "id"),
        ("per-page", "1"),
    ]
    try:
        payload = transport.get_json(BASE, params=params, policy=OpenAlex.policy)
    except TransportError as exc:
        warn(f"건수 조회 실패 ({exc})")
        return None
    return (payload.get("meta") or {}).get("count")


# --- 재개 상태 (provider 경로) ----------------------------------------------


def profile_dir(query_id, provider=records.DEFAULT_PROVIDER):
    return records.new_raw_dir(query_id, provider)


def state_path(query_id, provider=records.DEFAULT_PROVIDER):
    return profile_dir(query_id, provider) / "_state.json"


def load_state(query_id, provider=records.DEFAULT_PROVIDER):
    path = state_path(query_id, provider)
    if not path.exists():
        return {"cursor": "*", "pages": 0, "written": 0}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError, ValueError:
        warn(f"{query_id}: 상태 파일을 읽을 수 없어 처음부터 시작합니다")
        return {"cursor": "*", "pages": 0, "written": 0}


def save_state(query_id, state, provider=records.DEFAULT_PROVIDER):
    profile_dir(query_id, provider).mkdir(parents=True, exist_ok=True)
    with open(state_path(query_id, provider), "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)


def jsonl_path(query_id, provider=records.DEFAULT_PROVIDER, stamp=None):
    stamp = stamp or date.today().isoformat()
    return profile_dir(query_id, provider) / f"{stamp}.jsonl"


def write_meta(query_id, meta, provider=records.DEFAULT_PROVIDER):
    profile_dir(query_id, provider).mkdir(parents=True, exist_ok=True)
    with open(profile_dir(query_id, provider) / "_meta.json", "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=1)


def read_meta(query_id, provider=records.DEFAULT_PROVIDER):
    path = profile_dir(query_id, provider) / "_meta.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError, ValueError:
        return {}


# --- 사이드카 id 인덱스 (T6) -------------------------------------------------


def ids_path(query_id, provider=records.DEFAULT_PROVIDER):
    return profile_dir(query_id, provider) / "_ids.txt"


def _rescan_ids(query_id, provider):
    """하위호환: 사이드카 도입 이전 raw 를 위한 전체 jsonl 재스캔."""
    found = set()
    directory = profile_dir(query_id, provider)
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


def _write_ids(query_id, provider, ids):
    profile_dir(query_id, provider).mkdir(parents=True, exist_ok=True)
    with open(ids_path(query_id, provider), "w", encoding="utf-8") as handle:
        for identifier in sorted(ids):
            handle.write(identifier + "\n")


def _append_id(query_id, provider, identifier):
    with open(ids_path(query_id, provider), "a", encoding="utf-8") as handle:
        handle.write(identifier + "\n")


def load_seen_ids(query_id, provider=records.DEFAULT_PROVIDER):
    """이미 받아둔 work id 집합. 사이드카(_ids.txt)가 있으면 그것만 읽는다
    (O(1) 재개). 없으면 하위호환으로 전체 jsonl 을 재스캔해 만든 뒤, 다음
    번을 위해 그 결과로 사이드카를 생성한다.
    """
    path = ids_path(query_id, provider)
    if path.exists():
        with open(path, encoding="utf-8") as handle:
            return {line.strip() for line in handle if line.strip()}
    found = _rescan_ids(query_id, provider)
    if found:
        _write_ids(query_id, provider, found)
    return found


# --- 수집 --------------------------------------------------------------------


def _fetch_profile(query_id, query, config, transport, *, provider, verbose, max_pages):
    """전건을 data/raw/{provider}/{query_id}/{YYYY-MM-DD}.jsonl 에 무손실 추가한다.

    RunLog 를 모른다 — 실행 기록은 collect_profile() 이 이 함수를 감싸서 한다.
    """
    window = config["window"]
    limit = config.get("limit")
    per_page = config.get("per_page", 200)

    state = load_state(query_id, provider)
    previous = read_meta(query_id, provider)

    expected = total_count(transport, query, window)
    if expected is None:
        # 커서가 남아 있으면 건수를 못 세도 이어갈 수 있다. 직전 실행이 기록해 둔
        # 기대값을 쓴다. 이것이 없을 때만 포기한다.
        expected = previous.get("expected_from_api")
        if expected is None:
            warn(f"{query_id}: 전체 건수를 확인할 수 없고 직전 기록도 없어 중단합니다")
            return None
        warn(f"{query_id}: 건수 조회 실패. 직전 기록의 {expected:,}건을 목표로 이어갑니다")

    already = load_seen_ids(query_id, provider)
    if verbose:
        print(
            f"[{query_id}] 대상 {expected:,}건"
            + (f" / 상한 {limit:,}" if limit else " (전수)")
            + (f" / 이미 {len(already):,}건 보유, 이어서 수집" if already else "")
        )

    target = min(expected, limit) if limit else expected
    path = jsonl_path(query_id, provider)
    path.parent.mkdir(parents=True, exist_ok=True)

    new_records = 0
    duplicates = 0
    pages_this_run = 0
    cursor = state.get("cursor") or "*"
    stopped_early = False
    hit_max_pages = False
    budget_exhausted = False
    host = OpenAlex.policy.host

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

            params = [
                ("filter", build_filter(query, window)),
                ("per-page", str(per_page)),
                ("cursor", cursor),
            ]
            try:
                payload = transport.get_json(BASE, params=params, policy=OpenAlex.policy)
            except BudgetExhausted:
                stopped_early = True
                budget_exhausted = True
                if verbose:
                    print(
                        "  예산 소진 추정. 커서를 저장하고 멈춥니다."
                        " UTC 자정 이후 다시 실행하면 이어집니다."
                    )
                break
            except TransportError as exc:
                stopped_early = True
                warn(
                    f"{query_id}: 페이지 수집 실패 ({exc}). 커서를 저장하고 멈춥니다."
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
                _append_id(query_id, provider, identifier)
                new_records += 1
                if limit and len(already) >= target:
                    break
            handle.flush()
            pages_this_run += 1
            state["pages"] = state.get("pages", 0) + 1
            state["written"] = len(already)
            cursor = (payload.get("meta") or {}).get("next_cursor")
            state["cursor"] = cursor
            save_state(query_id, state, provider)

            remaining = transport.budget.remaining(host)
            if verbose:
                remaining_note = f"  예산잔량 {remaining:,}" if remaining is not None else ""
                print(
                    f"  {len(already):,}/{target:,}  ({state['pages']}페이지){remaining_note}",
                    flush=True,
                )
            if remaining == 0:
                # 성공(200) 응답인데 잔량이 0 인 경우도 소진으로 본다 — 다음 페이지의
                # 402/409 를 기다리지 않고 여기서 깨끗하게 멈춘다(legacy 와 동일 판정).
                stopped_early = True
                budget_exhausted = True
                if verbose:
                    print(
                        "  예산 소진 추정 (잔량 0). 커서를 저장하고 멈춥니다."
                        " UTC 자정 이후 다시 실행하면 이어집니다."
                    )
                break

    is_census = not limit and cursor is None and not stopped_early
    stopped_reason = determine_stopped_reason(
        budget_exhausted=budget_exhausted, hit_max_pages=hit_max_pages
    )
    meta = {
        "query_id": query_id,
        "provider": provider,
        "query": query,
        "window": window,
        "expected_from_api": expected,
        "collected": len(already),
        "new_this_run": new_records,
        "duplicates_skipped": duplicates,
        "pages": state.get("pages", 0),
        "pages_this_run": pages_this_run,
        "stopped_early": stopped_early,
        "stopped_reason": stopped_reason,
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
        # legacy 필드 그대로 유지(브리핑: "_meta.json 필드 전부 기존과 동일"). OpenAlex
        # 응답 구조를 실측 확인한 날짜 — 이 값 자체를 재계산하지 않는다. 응답 구조가
        # 바뀌었다고 판단되면 새로 실측하고 이 상수를 갱신할 것(collect_openalex.py
        # 상단 주석의 실측 기록과 같은 근거).
        "openalex_fields_verified_on": "2026-08-20",
    }
    write_meta(query_id, meta, provider)
    if verbose:
        print(
            f"[{query_id}] 완료: 신규 {new_records:,}건, 누적 {len(already):,}건, "
            f"중복 {duplicates:,}건 -> {path}"
        )
        if not is_census:
            warn(f"{query_id}: 전수가 아닙니다. {meta['census_note']}")
    return meta


def collect_profile(
    query_id,
    query,
    config,
    transport,
    run_log,
    *,
    provider=records.DEFAULT_PROVIDER,
    verbose=True,
    max_pages=None,
):
    """한 프로파일 수집을 RunLog 실행 1건으로 감싼다.

    run_id 를 발급하고(run.start) transport 의 매 요청을 fetch_log 로 기록한
    뒤(observer), 끝나면 run_source/run 을 채운다. evidence.pipeline.collect()
    와 같은 이유로, 실행 동안만 observer 를 설치하고 끝나면 이전 값으로
    되돌린다 — Transport 인스턴스가 이 호출 이후 재사용될 수 있는데, 안
    되돌리면 죽은 run_id 로 fetch_log 가 계속 쌓이거나 호출자가 미리 걸어
    둔 observer 가 사라진다.
    """
    run_id = run_log.start(
        "trend collect", {"profile": query_id, "provider": provider, "max_pages": max_pages}
    )
    request_count = 0

    def observer(fetch, status, attempt, elapsed_ms, error):
        nonlocal request_count
        request_count += 1
        run_log.log_fetch(
            run_id,
            source=provider,
            url=fetch.url,
            status=status,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    previous_observer = transport.set_observer(observer)
    status = "failed"
    meta = None
    try:
        meta = _fetch_profile(
            query_id,
            query,
            config,
            transport,
            provider=provider,
            verbose=verbose,
            max_pages=max_pages,
        )
        if meta is None:
            status = "failed"
        elif meta.get("stopped_reason"):
            status = "partial"
        else:
            status = "ok"
        return meta
    finally:
        run_log.record_source(
            run_id,
            provider,
            requests=request_count,
            records=(meta.get("new_this_run", 0) if meta else 0),
            errors=0,
            budget_remaining=transport.budget.remaining(OpenAlex.policy.host),
            stopped_reason=(meta.get("stopped_reason") if meta else None),
        )
        run_log.finish(run_id, status)
        transport.set_observer(previous_observer)


def run(
    query_ids,
    config,
    *,
    db_path,
    max_pages=None,
    dry_run=False,
    verbose=True,
    provider=records.DEFAULT_PROVIDER,
    transport=None,
):
    """profiles(query_id 목록)를 순회하며 수집한다.

    dry_run 이면 DB 를 열지 않고 건수/예상 요청 수만 돌려준다. 그 외에는
    한 커넥션을 열어 RunLog 를 만들고, 프로파일마다 collect_profile() 을
    불러 각자 독립된 run 으로 기록한다(하나의 CLI 호출이 여러 프로파일을
    수집해도, RunLog 의 run_source 는 run_id+source 가 키라 프로파일마다
    run_id 를 분리해야 서로 덮어쓰지 않는다).
    """
    transport = transport or Transport()
    per_page = config.get("per_page", 200)

    if not os.environ.get("OPENALEX_API_KEY", "").strip():
        warn(
            "OPENALEX_API_KEY 가 없어 무인증 예산(하루 1,000크레딧)으로 동작합니다. "
            "목록 호출은 페이지당 10크레딧이므로 하루 약 100페이지가 상한입니다."
        )

    results = {}
    if dry_run:
        for query_id in query_ids:
            query = config["profiles"][query_id]["query"]
            count = total_count(transport, query, config["window"])
            results[query_id] = {
                "dry_run": True,
                "count": count,
                "estimated_requests": (count // per_page + 1) if count is not None else None,
            }
        return results

    conn = repository.connect(db_path)
    try:
        run_log = RunLog(conn)
        for query_id in query_ids:
            query = config["profiles"][query_id]["query"]
            results[query_id] = collect_profile(
                query_id,
                query,
                config,
                transport,
                run_log,
                provider=provider,
                verbose=verbose,
                max_pages=max_pages,
            )
    finally:
        conn.close()
    return results
