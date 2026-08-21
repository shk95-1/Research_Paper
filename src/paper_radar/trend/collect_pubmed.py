"""1단계(PubMed): MeSH 월별 트렌드 수집. trend/collect.py(OpenAlex)의 대칭짝.

배경(T15): OpenAlex 키워드 어휘는 2025-10 에 통째로 교체된 전력이 있다(구
ASSUMPTIONS.md 의 최대 발견). MeSH(Medical Subject Headings)는 사람이 직접
매기는 통제 어휘라 그런 단절이 없다 — PubMed 를 trend 의 제2 프로바이더로
붙이면 OpenAlex 시계열의 어휘 단절을 교차 검증할 대조군이 생긴다.

**대원칙: 프로바이더 간 병합 집계는 하지 않는다.** 이 모듈은 openalex 의
`data/raw/openalex/{query_id}/`, `out/trend/{query_id}/keyword_monthly.csv`
등 어떤 openalex 경로·파일도 읽거나 쓰지 않는다 — 이 모듈이 쓰는 raw 는
`data/raw/pubmed/{query_id}/` 뿐이다(records.new_raw_dir(query_id, "pubmed")).

trend/collect.py 와 같은 규약, 다른 페이지네이션 단위
    RunLog 자기기록(run.start -> observer 로 fetch_log -> record_source ->
    finish), 사이드카 id 인덱스(`_ids.txt`, 여기서는 openalex work id 대신
    PMID), `_state.json` 재개 커서, `_meta.json` 의 is_census/stopped_reason
    규약은 trend/collect.py 와 같다. 다른 것은 페이지네이션의 단위뿐이다 —
    OpenAlex 는 커서 하나(next_cursor)로 전체 결과를 꿴다. PubMed esearch 는
    그런 커서가 없고 retstart 오프셋 페이지네이션만 지원하며, 게다가 날짜
    범위 파라미터(mindate/maxdate)로 미리 창을 쪼개지 않으면 한 검색에 수년
    치가 다 걸려 재개 단위를 잡기 어렵다. 그래서 이 모듈은 config 의 window
    를 월 단위로 쪼개고, 재개 커서는 (완료한 마지막 달, 그 달 안의
    retstart) 쌍이다 — trend/collect.py 의 `_state.json`(cursor 하나)과
    다른 이유가 여기 있다. 이 파일을 별도 모듈로 둔 것도(collect.py 를
    고쳐 provider 분기를 추가하는 대신) 이 단위 차이 때문이다: cursor 기반
    루프와 (월, retstart) 기반 루프를 하나의 함수에 우겨넣으면 둘 다
    읽기 어려워진다.

원문 XML 무손실 보존 원칙에서의 의도적 이탈
    trend/collect.py(openalex)는 "스키마는 나중에 정한다"는 원칙으로 API
    응답 원문을 그대로 저장한다. 이 모듈은 그러지 않고 parse_efetch_batch()
    가 뽑아낸 필드(pmid/doi/title/journal/mesh_terms)만 저장한다 — 이 판단의
    근거 둘: (1) PubMed 는 PMID 로 언제든 재조회할 수 있으므로 원문이
    "사라지는" 것이 아니다(원문 보존이 필요해지면 그 PMID 로 다시 efetch
    하면 된다). (2) 이 트렌드 축(mesh_monthly.csv)에 필요한 필드가 이미
    명확하다 — MeSH 축은 mesh_terms 하나만 쓴다. 원문 XML 을 통째로 보존할
    이유(스키마 불확실성)가 openalex 만큼 강하지 않다.

NCBI 에는 예산(rate-limit credit) 헤더가 없다
    OpenAlex 의 x-ratelimit-remaining 같은 응답 헤더를 NCBI E-utilities 는
    보내지 않는다(BudgetTracker.observe() 가 관련 헤더를 못 찾으면 조용히
    아무것도 안 하므로, transport.budget.remaining(host) 는 항상 None 이다).
    그래서 trend/collect.py 의 진행 출력에 있는 "예산잔량 N" 문구를 이
    모듈의 진행 출력에는 넣지 않는다 — 항상 None 만 찍을 문구는 소음이다.
    다만 429(RateLimited)는 예산과 무관한 별개 메커니즘이라 여전히
    존재한다 — 재시도/백오프는 transport.http.Transport 가 이미 처리하므로
    이 모듈은 별도 코드 없이 그 혜택을 받는다.
"""

from __future__ import annotations

import calendar
import json
import os
import sys
from datetime import UTC, date, datetime

from paper_radar.sources import pubmed
from paper_radar.storage import repository
from paper_radar.storage.runlog import RunLog
from paper_radar.transport.errors import BudgetExhausted, TransportError
from paper_radar.transport.http import Transport
from paper_radar.trend import records
from paper_radar.trend.aggregate import month_add

PROVIDER = "pubmed"
CONFIG_PATH = records.HERE / "config.json"

# 월 1회 esearch retmax. efetch 배치 상한(EFETCH_BATCH_MAX=200)보다 넉넉히
# 잡아 한 달 안에서의 esearch 페이지 수 자체를 줄인다(re search 요청도
# 페이스 제한을 받으므로, 페이지가 적을수록 그 달을 더 빨리 끝낸다).
ESEARCH_PAGE = 500


def warn(message):
    print(f"[warn] {message}", file=sys.stderr)


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def determine_stopped_reason(*, budget_exhausted, transport_error=False, hit_max_months):
    """중단 사유를 우선순위대로 정한다. 정상 완료(또는 그 외 실패)면 None. (순수 함수)

    trend/collect.py 의 determine_stopped_reason() 과 같은 우선순위 규칙
    (예산 소진 > transport 오류 > 상한 도달)이되, 단위가 페이지가 아니라
    달이라 별도 함수로 둔다 — collect.py 의 함수를 hit_max_pages= 라는
    이름으로 호출하면 pubmed 컨텍스트에서 "페이지"라는 말 자체가 오해를
    부른다. 리뷰 대응(Finding 1): _fetch_month() 가 이미 "transport_error"
    를 반환했는데도 이전에는 이 함수가 그 값을 몰라 결과적으로 stopped_reason
    이 None(정상 완료)이 되고, RunLog 상태 "ok"/CLI exit 0 으로 잘못
    기록됐다 — collect.py 와 같은 인자를 추가해 바로잡는다.
    """
    if budget_exhausted:
        return "budget_exhausted"
    if transport_error:
        return "transport_error"
    if hit_max_months:
        return "max_months"
    return None


def month_list(window):
    """window['from']~window['to'] 를 양끝 포함 "YYYY-MM" 리스트로 쪼갠다.

    월 산술은 trend.aggregate.month_add() 를 그대로 재사용한다(중복 구현
    금지 — aggregate.py 가 이미 검증된 순수 함수로 갖고 있다).
    """
    start = window["from"][:7]
    end = window["to"][:7]
    months = [start]
    while months[-1] < end:
        months.append(month_add(months[-1], 1))
    return months


def month_bounds(month, window):
    """month("YYYY-MM")의 mindate/maxdate(esearch 용 "YYYY-MM-DD").

    창의 시작/끝 달이면 달력 월 전체가 아니라 window 경계로 자른다 — window
    가 월 중간에서 시작·끝나는 경우(예: from=2023-09-15) 그 달의 09-01~09-14
    를 이중으로 세거나 창 밖 날짜를 요청하지 않기 위해서다.
    """
    year, mon = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, mon)[1]
    start = f"{month}-01"
    end = f"{month}-{last_day:02d}"
    if start < window["from"]:
        start = window["from"]
    if end > window["to"]:
        end = window["to"]
    return start, end


# --- 재개 상태 (provider="pubmed" 고정 경로) ---------------------------------


def profile_dir(query_id):
    return records.new_raw_dir(query_id, PROVIDER)


def state_path(query_id):
    return profile_dir(query_id) / "_state.json"


def meta_path(query_id):
    return profile_dir(query_id) / "_meta.json"


def ids_path(query_id):
    return profile_dir(query_id) / "_ids.txt"


def jsonl_path(query_id, stamp=None):
    stamp = stamp or date.today().isoformat()
    return profile_dir(query_id) / f"{stamp}.jsonl"


def _initial_state():
    return {"month_cursor": None, "retstart": 0, "complete": False, "complete_through_month": None}


def load_state(query_id):
    """월 커서: {"month_cursor", "retstart", "complete", "complete_through_month"}.

    month_cursor 는 "다음(또는 진행 중인) 달"을 가리킨다. retstart 는 그 달
    안에서 이미 받은 개수(재개 시 esearch 의 retstart 로 그대로 쓴다).

    complete 를 별도 필드로 두는 이유: "아직 시작 안 함"과 "전 구간을 이미
    끝냄"이 둘 다 month_cursor=None 이 되면 구분이 안 된다 — 구분하지
    않으면 이미 전수를 채운 뒤 `trend collect --provider pubmed` 를 다시
    실행할 때마다 매번 window 전체를 처음부터 다시 esearch 하게 된다
    (사이드카 덕에 결과 자체는 틀리지 않지만, 필요 없는 요청을 매번 반복
    한다). complete=True 면 `_fetch_profile()` 이 아무 달도 처리하지 않고
    바로 넘어간다.

    complete_through_month 는 complete=True 를 만든 시점의 마지막 달
    (그때의 config.window 기준 months[-1])이다(리뷰 대응, Finding 2) —
    window.to 를 늘려 재수집(가장 흔한 운영 행위)하면 새 months[-1] 이
    이 값과 달라지므로, `_fetch_profile()` 이 "창이 늘었다"를 감지해
    complete 를 해제하고 새로 늘어난 꼬리 달부터만 재개한다(기존 커서
    분기가 창이 바뀐 경우를 warn+리셋으로 다루는 것과 같은 방향).
    """
    path = state_path(query_id)
    if not path.exists():
        return _initial_state()
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except OSError, ValueError:
        warn(f"{query_id}: pubmed 상태 파일을 읽을 수 없어 처음부터 시작합니다")
        return _initial_state()
    state.setdefault("complete_through_month", None)  # 이 필드 도입 이전 상태 파일 하위호환
    return state


def save_state(query_id, state):
    profile_dir(query_id).mkdir(parents=True, exist_ok=True)
    with open(state_path(query_id), "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)


def write_meta(query_id, meta):
    profile_dir(query_id).mkdir(parents=True, exist_ok=True)
    with open(meta_path(query_id), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=1)


def read_meta(query_id):
    path = meta_path(query_id)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError, ValueError:
        return {}


def load_seen_pmids(query_id):
    """이미 받아둔 PMID 집합. 사이드카(`_ids.txt`)가 있으면 그것만 읽는다.

    trend/collect.py 의 load_seen_ids() 와 같은 O(1) 재개 아이디어이되, 그
    함수는 jsonl 레코드의 "id"(openalex work id) 키를 가정한다 — 이 모듈의
    레코드는 "pmid" 키를 쓰므로 그대로 재사용할 수 없다(전체 jsonl 재스캔
    하위호환 분기는 pubmed raw 가 항상 이 모듈이 새로 만든 것이라 필요 없다
    — openalex 처럼 사이드카 도입 "이전"의 레거시 raw 가 없다).
    """
    path = ids_path(query_id)
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as handle:
        return {line.strip() for line in handle if line.strip()}


def _write_ids(query_id, pmids):
    profile_dir(query_id).mkdir(parents=True, exist_ok=True)
    with open(ids_path(query_id), "w", encoding="utf-8") as handle:
        for pmid in sorted(pmids):
            handle.write(pmid + "\n")


def _append_pmid(query_id, pmid):
    with open(ids_path(query_id), "a", encoding="utf-8") as handle:
        handle.write(pmid + "\n")


# --- 수집 --------------------------------------------------------------------


def _fetch_month(query_id, query, month, window, transport, already, handle, *, retstart):
    """한 달치를 esearch(retstart 페이지네이션) + efetch 배치로 받아 handle 에 append 한다.

    반환: (expected, collected, duplicates, next_retstart, stopped_reason).
    expected 는 이 달의 esearch count(조회 실패 시 None). stopped_reason 은
    "budget_exhausted" 또는 "transport_error"(429 재시도 소진 등 그 외
    transport 오류) 또는 None(이 달을 끝까지 받았다). stopped_reason 이 있으면
    next_retstart 는 실패한 esearch 페이지가 시작한 retstart 그대로다(전진
    시키지 않는다 — 아래 루프 안의 주석 참고, Finding 1 대응).
    """
    mindate, maxdate = month_bounds(month, window)
    expected = None
    collected = 0
    duplicates = 0
    stopped_reason = None

    while True:
        try:
            pmids, count = pubmed.search_pmids(
                transport,
                query,
                retmax=ESEARCH_PAGE,
                retstart=retstart,
                mindate=mindate,
                maxdate=maxdate,
            )
        except BudgetExhausted:
            stopped_reason = "budget_exhausted"
            break
        except TransportError as exc:
            warn(
                f"{query_id} {month}: esearch 실패 ({exc}). 이 달의 커서를 저장하고 멈춥니다."
                " 잠시 뒤 다시 실행하면 이어집니다."
            )
            stopped_reason = "transport_error"
            break

        expected = count
        if not pmids:
            break

        duplicates += sum(1 for pmid in pmids if pmid in already)
        new_pmids = [pmid for pmid in pmids if pmid not in already]

        for start in range(0, len(new_pmids), pubmed.EFETCH_BATCH_MAX):
            chunk = new_pmids[start : start + pubmed.EFETCH_BATCH_MAX]
            try:
                articles = pubmed.fetch_batch(transport, chunk)
            except BudgetExhausted:
                stopped_reason = "budget_exhausted"
                break
            except TransportError as exc:
                warn(
                    f"{query_id} {month}: efetch 실패 ({exc}). 이 달의 커서를 저장하고 멈춥니다."
                    " 잠시 뒤 다시 실행하면 이어집니다."
                )
                stopped_reason = "transport_error"
                break

            for article in articles:
                pmid = article.get("pmid")
                if not pmid or pmid in already:
                    continue  # efetch 응답이 요청과 다른 pmid 를 줄 리는 없지만, 방어적으로.
                already.add(pmid)
                record = {
                    "pmid": pmid,
                    "doi": article.get("doi"),
                    "title": article.get("title"),
                    "journal": article.get("journal"),
                    "mesh_terms": list(article.get("mesh_terms") or ()),
                    "month_bucket": month,
                    "provider": PROVIDER,
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                _append_pmid(query_id, pmid)
                collected += 1

        if stopped_reason:
            # 리뷰 대응(Finding 1): retstart 를 전진시키지 않는다. 한 esearch
            # 페이지(최대 500개)가 여러 efetch 청크(200개씩)로 나뉘는데, 청크
            # 중간에 실패하면 그 청크와 그 뒤 미시도 청크의 PMID 는 이번 실행에서
            # 전혀 못 받은 것이다. retstart 를 전진시켜 버리면 다음 실행이 이
            # 페이지를 건너뛰어 그 PMID 들을 영원히 재시도하지 못하고, 그 달의
            # expected(esearch count) > collected(저장된 고유 PMID 수) 가
            # 고착돼 is_census 가 영영 False 로 남는다. retstart 를 그대로 두면 다음
            # 실행이 **같은 페이지를 처음부터 다시** esearch 하지만, 사이드카
            # (`_ids.txt`) 에 이미 실려 있는 PMID 는 `already` 로 걸러져
            # efetch 를 다시 보내지 않는다(멱등) — 그래서 이미 성공한 청크는
            # 반복 요청 없이, 실패/미시도 청크만 실제로 재시도된다.
            break

        retstart += len(pmids)
        if retstart >= count or len(pmids) < ESEARCH_PAGE:
            break

    return expected, collected, duplicates, retstart, stopped_reason


def _fetch_profile(query_id, query, config, transport, *, verbose, max_months):
    """전 구간을 월 단위로 data/raw/pubmed/{query_id}/{YYYY-MM-DD}.jsonl 에 append 한다.

    RunLog 를 모른다 — 실행 기록은 collect_profile() 이 이 함수를 감싸서 한다.
    """
    window = config["window"]
    months = month_list(window)

    state = load_state(query_id)
    previous = read_meta(query_id)
    previous_months = dict(previous.get("months") or {})

    already = load_seen_pmids(query_id)
    if verbose:
        print(
            f"[{query_id}/pubmed] {len(months)}개월 창"
            + (f" / 이미 {len(already):,}건 보유, 이어서 수집" if already else "")
        )

    path = jsonl_path(query_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    cursor = state.get("month_cursor")
    if state.get("complete"):
        completed_through = state.get("complete_through_month")
        if months and completed_through == months[-1]:
            # 같은 창을 이미 끝냈다 — 아무 달도 다시 처리하지 않는다(load_state()
            # docstring의 "complete 를 별도 필드로 두는 이유" 참고).
            start_index = len(months)
        else:
            # 리뷰 대응(Finding 2): 완료 시점의 마지막 달(complete_through_month)
            # 이 현재 window 의 마지막 달과 다르다 — window.to 연장(가장 흔한
            # 운영 행위)으로 새 달이 늘어났다는 뜻이다. complete 를 해제하고,
            # 완료 시점 다음 달부터만 재개한다(완료된 달을 다시 esearch 하지
            # 않는다). completed_through 가 지금 months 목록에 아예 없으면
            # (window.from 도 바뀌는 등 더 근본적인 변경) 안전하게 처음부터
            # 다시 시작한다 — 저장된 커서를 못 찾을 때의 기존 처리와 같은 원칙.
            warn(
                f"{query_id}: window 가 늘어났습니다"
                f"({completed_through or '?'} 까지 완료 -> 새 창 끝"
                f" {months[-1] if months else '?'}). 전 구간 완료 표시를 해제하고"
                " 새로 늘어난 달부터 이어서 수집합니다."
            )
            state["complete"] = False
            start_index = months.index(completed_through) + 1 if completed_through in months else 0
            cursor = months[start_index] if start_index < len(months) else None
            state["month_cursor"] = cursor
            state["retstart"] = 0
            save_state(query_id, state)
    elif cursor:
        try:
            start_index = months.index(cursor)
        except ValueError:
            # config 의 window 가 바뀌어 저장된 달이 더 이상 목록에 없다 — 처음부터.
            warn(f"{query_id}: 저장된 커서 달({cursor})이 현재 window 에 없어 처음부터 시작합니다")
            start_index = 0
    else:
        start_index = 0

    months_meta = previous_months
    new_records = 0
    months_this_run = 0
    stopped_early = False
    budget_exhausted = False
    transport_error = False
    hit_max_months = False

    with open(path, "a", encoding="utf-8") as handle:
        for index in range(start_index, len(months)):
            if max_months and months_this_run >= max_months:
                stopped_early = True
                hit_max_months = True
                if verbose:
                    print(
                        f"  --max-months {max_months} 에 도달. 커서를 저장하고 멈춥니다."
                        " 다시 실행하면 이어집니다."
                    )
                break

            month = months[index]
            retstart = state.get("retstart", 0) if month == cursor else 0
            expected, collected, duplicates, next_retstart, reason = _fetch_month(
                query_id, query, month, window, transport, already, handle, retstart=retstart
            )
            prior = months_meta.get(month, {})
            months_meta[month] = {
                "expected": expected if expected is not None else prior.get("expected"),
                "collected": prior.get("collected", 0) + collected,
                "duplicates": prior.get("duplicates", 0) + duplicates,
            }
            new_records += collected
            months_this_run += 1
            if verbose:
                print(
                    f"  [{month}] 신규 {collected:,}건, 중복 {duplicates:,}건"
                    + (f" / 목표 {expected:,}건" if expected is not None else "")
                )

            if reason:
                stopped_early = True
                budget_exhausted = reason == "budget_exhausted"
                # 리뷰 대응(Finding 1): reason 이 "transport_error"일 때 이전에는
                # 이 값을 버렸다(budget_exhausted 만 봤다) — determine_stopped_reason()
                # 이 결국 None 을 돌려줘 RunLog "ok"/CLI exit 0 으로 잘못 기록됐다.
                transport_error = reason == "transport_error"
                state["month_cursor"] = month
                state["retstart"] = next_retstart
                save_state(query_id, state)
                if verbose and reason == "budget_exhausted":
                    print(
                        "  예산 소진 추정. 커서를 저장하고 멈춥니다."
                        " UTC 자정 이후 다시 실행하면 이어집니다."
                    )
                break

            next_month = months[index + 1] if index + 1 < len(months) else None
            state["month_cursor"] = next_month
            state["retstart"] = 0
            state["complete"] = next_month is None
            if next_month is None:
                # 이 창의 마지막 달을 방금 끝냈다 — 다음 실행이 window.to 연장
                # 여부를 판단할 기준값(Finding 2, load_state() docstring 참고).
                state["complete_through_month"] = month
            save_state(query_id, state)

    all_months_done = state.get("complete", False)
    all_expected_known = len(months_meta) == len(months) and all(
        m.get("expected") is not None for m in months_meta.values()
    )
    expected_total = sum(m.get("expected") or 0 for m in months_meta.values())
    collected_total = sum(m.get("collected", 0) for m in months_meta.values())
    duplicates_total = sum(m.get("duplicates", 0) for m in months_meta.values())
    # census 판정은 expected_total == collected_total 로만 본다 —
    # duplicates_total 을 더하지 않는다(리뷰 대응, Finding 1 수정의 파생
    # 버그). months_meta[month]["collected"] 는 이미 실행을 거듭할 때마다
    # `prior.get("collected") + 이번 실행의 신규 건수`로 누적되므로, 그
    # 자체가 "이 달에 실제로 저장된 고유 PMID 총수"다. 반면
    # months_meta[month]["duplicates"] 는 "이번 esearch 페이지 재시도에서
    # 이미 알고 있던(already) PMID 로 다시 걸린 횟수"인데, retstart 를
    # 전진시키지 않는 재시도 설계(Finding 1) 상 같은 페이지를 여러 번
    # 재시도하면 그때마다 이미 collected_total 에 반영된 바로 그 PMID 들이
    # "새로운 duplicates" 로 또 잡혀 누적된다 — collected_total 과
    # duplicates_total 을 더하면 같은 PMID 를 이중으로 센다. duplicates_total
    # 은 그래서 census 등식에서 빼고, 진단용 참고값(meta 의
    # duplicates_skipped)으로만 남긴다.
    is_census = all_months_done and all_expected_known and not stopped_early and (
        expected_total == collected_total
    )
    stopped_reason = determine_stopped_reason(
        budget_exhausted=budget_exhausted,
        transport_error=transport_error,
        hit_max_months=hit_max_months,
    )

    meta = {
        "query_id": query_id,
        "provider": PROVIDER,
        "query": query,
        "window": window,
        "months": months_meta,
        # trend.aggregate.census_guard() 가 provider 무관하게 읽는 키 이름
        # (collected/expected_from_api) — openalex collect.py 의 _meta.json
        # 과 같은 이름을 써야 census_guard() 를 수정 없이 그대로 재사용할 수
        # 있다(mesh_aggregate.py 가 이 재사용을 한다).
        "expected_from_api": expected_total,
        "collected": collected_total,
        "new_this_run": new_records,
        "duplicates_skipped": duplicates_total,
        "months_this_run": months_this_run,
        "stopped_early": stopped_early,
        "stopped_reason": stopped_reason,
        "is_census": is_census,
        "census_note": (
            "전수. window 안의 모든 달을 완료했고, 각 달의 esearch count 합이"
            " 실제 저장된 고유 PMID 수 합과 일치한다."
            if is_census
            else "표본 또는 미완. window 의 일부 달만 수집됐거나 중단됐다면"
            " 아직 전수가 아니다. prevalence 를 모집단 비율로 해석하지 말 것."
        ),
        "collected_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # 리뷰 대응(minor): 이 태스크(T15)는 월별 esearch(mindate/maxdate/
        # retstart)·배치 efetch 경로를 실제 네트워크로 검증하지 않았다(전부
        # FakeSession — tool/live_smoke.py 에 스모크 테스트는 "작성"만 했다,
        # 브리핑 제약). "실측 확인한 날짜"라는 상수를 미검증 상태로 박아두면
        # 오해를 부르므로 None 으로 둔다 — `uv run python tool/live_smoke.py`
        # 를 실제로 돌려 이 경로(mindate/maxdate 페이지네이션, 배치 efetch)가
        # 통과한 날짜를 그때 사람이 채워 넣을 것.
        "pubmed_fields_verified_on": None,
    }
    write_meta(query_id, meta)
    if verbose:
        print(
            f"[{query_id}/pubmed] 완료: 신규 {new_records:,}건, 누적 {collected_total:,}건, "
            f"중복 {duplicates_total:,}건 -> {path}"
        )
        if not is_census:
            warn(f"{query_id}: pubmed 전수가 아닙니다. {meta['census_note']}")
    return meta


def collect_profile(
    query_id,
    query,
    config,
    transport,
    run_log,
    *,
    verbose=True,
    max_months=None,
):
    """한 프로파일 수집을 RunLog 실행 1건으로 감싼다. trend/collect.py 의 동명 함수와 같은 구조.

    Transport 인스턴스가 이 호출 이후에도 재사용될 수 있으므로, 실행 동안만
    observer 를 설치하고 끝나면 이전 값으로 되돌린다.
    """
    run_id = run_log.start(
        "trend collect", {"profile": query_id, "provider": PROVIDER, "max_months": max_months}
    )
    request_count = 0

    def observer(fetch, status, attempt, elapsed_ms, error):
        nonlocal request_count
        request_count += 1
        run_log.log_fetch(
            run_id,
            source=PROVIDER,
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
            query_id, query, config, transport, verbose=verbose, max_months=max_months
        )
        status = "partial" if meta.get("stopped_reason") else "ok"
        return meta
    finally:
        run_log.record_source(
            run_id,
            PROVIDER,
            requests=request_count,
            records=(meta.get("new_this_run", 0) if meta else 0),
            errors=0,
            # NCBI 는 예산 헤더를 보내지 않는다(모듈 docstring) — 항상 None.
            budget_remaining=transport.budget.remaining(pubmed.PubMed.policy.host),
            stopped_reason=(meta.get("stopped_reason") if meta else None),
        )
        run_log.finish(run_id, status)
        transport.set_observer(previous_observer)


def run(
    query_ids,
    config,
    *,
    db_path,
    max_months=None,
    dry_run=False,
    verbose=True,
    transport=None,
):
    """profiles(query_id 목록)를 순회하며 pubmed_query 로 수집한다.

    trend/collect.py 의 run() 과 같은 구조 — dry_run 이면 DB 를 열지 않는다.
    profile 에 pubmed_query 가 없으면 호출자(CLI)가 미리 걸러야 한다(이
    함수는 그 검증을 하지 않는다 — config["profiles"][query_id]["pubmed_query"]
    가 없으면 KeyError 로 바로 실패한다).
    """
    transport = transport or Transport()

    if not os.environ.get("NCBI_API_KEY", "").strip():
        warn(
            "NCBI_API_KEY 가 없어 무키 상한(3req/s)으로 동작합니다."
            " 무료 키를 등록하면 10req/s 로 늘어납니다."
        )

    results = {}
    if dry_run:
        for query_id in query_ids:
            query = config["profiles"][query_id]["pubmed_query"]
            window = config["window"]
            months = month_list(window)
            first_start, _ = month_bounds(months[0], window)
            _, last_end = month_bounds(months[-1], window)
            pmids, count = pubmed.search_pmids(
                transport, query, retmax=1, mindate=first_start, maxdate=last_end
            )
            results[query_id] = {"dry_run": True, "count": count, "months": len(months)}
        return results

    conn = repository.connect(db_path)
    try:
        run_log = RunLog(conn)
        for query_id in query_ids:
            query = config["profiles"][query_id]["pubmed_query"]
            results[query_id] = collect_profile(
                query_id,
                query,
                config,
                transport,
                run_log,
                verbose=verbose,
                max_months=max_months,
            )
    finally:
        conn.close()
    return results
