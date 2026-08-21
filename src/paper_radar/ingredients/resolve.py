"""PubChem 이름 해소 — 조회 -> 저장, 그리고 실행 자체를 RunLog 에 기록한다.

evidence.pipeline.collect()/trend.collect.run()/trials.collect.run() 과 같은
골격(RunLog.start -> 조회 -> upsert -> record_source -> finish)이지만, 페이지
네이션도 다중 레코드도 없는 훨씬 단순한 형태다 — 이름 하나가 IngredientRecord
하나(또는 PubChem 이 모르는 이름이면 없음)로 끝난다.

cli.py 밖에 있는 이유는 trials.collect.run() 과 동일하다(trials/collect.py
모듈 docstring "코드리뷰 대응" 참고) — 오케스트레이션(RunLog 자기기록 +
observer 설치/해제)을 CLI 계층에 두면 evidence.pipeline/trend.collect/
trials.collect 에 이은 네 번째 사본이 된다. T12(이 태스크)는 처음부터 이
자리에 둔다.

NotFound 는 실패가 아니라 "PubChem 이 이 이름을 모른다"는 정상적인 결과의
하나다(브리핑: "부재는 실패가 아니다") — 여기서 잡아 ResolveReport.record=None
/ status="not_found" 로 접어서 돌려준다. CLI 는 그 값을 보고 exit 0 으로
안내 메시지만 찍는다(예외를 잡아 처리하지 않는다).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from paper_radar import registry
from paper_radar.models import IngredientRecord
from paper_radar.sources import pubchem
from paper_radar.storage import repository
from paper_radar.storage.runlog import RunLog
from paper_radar.transport.errors import NotFound


@dataclass(frozen=True)
class ResolveReport:
    """run() 실행 결과 요약. CLI 는 이 값으로 출력·exit code 를 정한다.

    status: "ok"(PubChem 이 이름을 알고 있어 IngredientRecord 를 얻고
    저장했다) | "not_found"(PubChem 이 모르는 이름 — 실패가 아니다). trials
    의 CollectReport 와 같은 결의 "단일 출처" 계약이다: 이 판정은 이 dataclass
    를 만드는 run() 안에서만 계산하고, CLI 는 그 값을 그대로 옮길 뿐 다시
    계산하지 않는다. "failed"는 run() 이 처리하지 못한 예외로 끝나는 경우라
    이 필드로는 관측되지 않는다(그 경로에서는 ResolveReport 자체가 만들어지지
    않고 예외가 호출자까지 전파된다).
    """

    run_id: str
    record: IngredientRecord | None
    status: str


def run(conn, transport, name, *, on_progress=None) -> ResolveReport:
    """이름 -> PubChem 조회 -> 저장, 그리고 실행 자체를 RunLog 에 기록한다.

    observer 설치/해제는 trials.collect.run() 과 동일한 이유로 같은 방식을
    따른다: Transport 인스턴스가 이 호출 이후에도 재사용될 수 있어, finally
    에서 반드시 이전 observer 로 되돌린다.
    """
    run_log = RunLog(conn)
    run_id = run_log.start("ingredient resolve", {"name": name})

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

    # finally 가 이 값을 그대로 기록한다 — 아래에서 "ok"/"not_found" 로 갱신
    # 못하고 예외가 새면 "failed" 가 남는다(죽은 run 이 running 으로 안 남게).
    status = "failed"
    try:
        try:
            record = pubchem.resolve(transport, name)
        except NotFound:
            # "이름을 모른다"는 정상적인 부재다 — 여기서 흡수하고 그 사실을
            # stopped_reason/status 로만 남긴다(예외를 CLI 까지 새게 두지 않는다).
            record = None

        saved = 0
        if record is not None:
            saved = repository.upsert_records(conn, [record])
            if on_progress:
                on_progress(record)

        run_log.record_source(
            run_id,
            pubchem.PubChem.key,
            requests=request_counts.get(pubchem.PubChem.key, 0),
            records=saved,
            errors=0,
            budget_remaining=transport.budget.remaining(pubchem.PubChem.policy.host),
            stopped_reason=None if record is not None else "not_found",
        )

        status = "ok" if record is not None else "not_found"
        return ResolveReport(run_id=run_id, record=record, status=status)
    finally:
        run_log.finish(run_id, status)
        transport.set_observer(previous_observer)
