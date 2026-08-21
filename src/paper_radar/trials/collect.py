"""임상시험 수집 — 검색 -> 저장, 그리고 실행 자체를 RunLog 에 기록한다.

evidence.pipeline.collect()/trend.collect.run() 과 같은 골격(RunLog.start ->
점진 소비 -> upsert -> record_source -> finish)이지만, 다른 소스로 빈 칸을
채우는 보강(enrich) 단계가 없어 훨씬 단순하다 —
clinicaltrials.iter_studies() 를 제너레이터인 채로 직접 순회해서(list() 로
감싸지 않고) 중간 페이지에서 예외가 나도 이미 받은 레코드를 보존한다
(openalex.iter_search()/evidence.pipeline.collect() 와 같은 이유 —
sources/clinicaltrials.py 모듈 docstring 참고).

코드리뷰 대응(이 모듈이 cli.py 밖으로 나온 이유): 원래 이 러너는
cli.py 안에 `_collect_trials()`로 있었는데, cli.py 자신의 "인자 파싱과
출력만 담당한다"는 계약과 모순됐고 evidence.pipeline/trend.collect 에 이어
같은 패턴(RunLog/observer/host_to_source 오케스트레이션)의 세 번째 사본을
CLI 계층에 만들고 있었다. evidence/·trend/ 와 대칭이 되도록 여기로 옮겼다
— 로직은 옮기면서 바뀐 것이 없다(import 경로만 바뀌었다).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from paper_radar import registry
from paper_radar.models import TrialRecord
from paper_radar.sources import clinicaltrials
from paper_radar.storage import repository
from paper_radar.storage.runlog import RunLog
from paper_radar.transport.errors import BudgetExhausted


@dataclass(frozen=True)
class TrialsReport:
    """run() 실행 결과 요약. CLI 는 이 값으로 exit code 를 정한다.

    evidence.pipeline.CollectReport 와 같은 결의 "단일 출처" 계약이다 —
    status("ok"/"partial")는 이 dataclass 를 만드는 run() 안에서만 계산하고,
    CLI(_run_trials_collect)는 그 값을 그대로 옮길 뿐 stopped_reason 을 다시
    훑어 재계산하지 않는다. "failed"는 run() 이 처리하지 못한 예외로 끝나는
    경우라 이 필드로는 관측되지 않는다(그 경로에서는 TrialsReport 자체가
    만들어지지 않고 예외가 호출자까지 전파된다).
    """

    run_id: str
    records: list[TrialRecord]
    stopped_reason: str | None
    status: str


def run(conn, transport, query, limit, *, on_progress=None) -> TrialsReport:
    """검색 -> 저장, 그리고 실행 자체를 RunLog 에 기록한다.

    observer 설치/해제는 evidence.pipeline.collect() 와 동일한 이유로 같은
    방식을 따른다: Transport 인스턴스가 이 호출 이후에도 재사용될 수 있어,
    finally 에서 반드시 이전 observer 로 되돌린다.
    """
    run_log = RunLog(conn)
    run_id = run_log.start("trials collect", {"query": query, "limit": limit})

    host_to_source = {source_cls.policy.host: key for key, source_cls in registry.SOURCES.items()}
    request_counts: dict[str, int] = dict.fromkeys(registry.SOURCES, 0)

    def observer(fetch, status, attempt, elapsed_ms, error):
        source = host_to_source.get(urlsplit(fetch.url).netloc)
        if source is None:
            return  # 등록되지 않은 host — 집계 대상 밖
        request_counts[source] += 1
        run_log.log_fetch(
            run_id,
            source=source,
            url=fetch.url,
            status=status,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    previous_observer = transport.set_observer(observer)

    # finally 가 이 값을 그대로 기록한다 — 아래에서 "ok"/"partial" 로 갱신
    # 못하고 예외가 새면 "failed" 가 남는다(죽은 run 이 running 으로 안 남게).
    status = "failed"
    stopped_reason: str | None = None
    try:
        studies: list[TrialRecord] = []
        try:
            for study in clinicaltrials.iter_studies(transport, query, limit):
                studies.append(study)
        except BudgetExhausted:
            stopped_reason = "budget_exhausted"

        if on_progress:
            for index, study in enumerate(studies, start=1):
                on_progress(index, len(studies), study)

        saved = repository.upsert_records(conn, studies) if studies else 0

        run_log.record_source(
            run_id,
            clinicaltrials.ClinicalTrials.key,
            requests=request_counts.get(clinicaltrials.ClinicalTrials.key, 0),
            records=saved,
            errors=0,
            budget_remaining=transport.budget.remaining(clinicaltrials.ClinicalTrials.policy.host),
            stopped_reason=stopped_reason,
        )

        status = "partial" if stopped_reason else "ok"
        return TrialsReport(
            run_id=run_id, records=studies, stopped_reason=stopped_reason, status=status
        )
    finally:
        run_log.finish(run_id, status)
        transport.set_observer(previous_observer)
