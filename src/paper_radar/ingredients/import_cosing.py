"""CosIng CSV 임포트 — reference.cosing.iter_records() 로 읽어 저장, 실행 자체를 RunLog 에 기록한다.

evidence.pipeline.collect()/trials.collect.run()/ingredients.resolve.run() 과
같은 골격(RunLog.start -> 조회 -> upsert -> record_source -> finish)이지만,
네트워크가 전혀 없다 — CosIng 은 사람이 tool/fetch_cosing.py 로 미리 받아 둔
로컬 CSV 를 읽을 뿐이다. observer/host_to_source 배선(evidence.pipeline·
trials.collect·ingredients.resolve 가 공유하는 부분)은 필요 없다 — 보낼
요청이 없으니 Transport 도 받지 않는다. RunLog.record_source() 에는
requests=0 을 그대로 기록해 "이 소스는 네트워크를 안 썼다"가 fetch_log 를
보지 않고도 run_source 한 줄로 드러나게 한다.

cli.py 밖에 있는 이유는 ingredients.resolve.run() 과 동일하다(계약: "cli.py
는 인자 파싱과 출력만 담당한다" — trials.collect.run() 이 옮겨진 코드리뷰
선례를 T12·T13 모두 처음부터 따른다).

fetched_at 출처 (브리핑에 명시되지 않은 설계 결정)
    reference.cosing.iter_records() 는 fetched_at 을 인자로 주입받아야
    하는데(브리핑: "fetched_at 은 _meta 의 값"), `paper-radar ingredient
    import-cosing` CLI 에는 --fetched-at 플래그가 없다(브리핑 CLI 절도
    --path/--db 만 선언한다). tool/fetch_cosing.py 가 CSV 와 같은 디렉터리에
    _meta.json 을 쓰므로, 이 모듈은 --path 파일과 같은 디렉터리의
    _meta.json 을 찾아 "fetched_at" 값을 읽는다 — 있으면 그 값을,
    없으면(_meta.json 이 아직 없거나 읽기 실패) 지금 시각(UTC)으로
    대체한다. _meta.json 없이 CSV 픽스처만 바로 넣는 테스트/수동 실행도
    막지 않기 위함이다.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from paper_radar.reference import cosing
from paper_radar.storage import repository
from paper_radar.storage.runlog import RunLog

# tool/fetch_cosing.py 가 쓰는 파일 이름과 반드시 같아야 한다(같은 디렉터리
# 관례로 fetched_at 을 넘겨받는다) — tool/fetch_cosing.py 의 META_FILENAME 참고.
META_FILENAME = "_meta.json"


@dataclass(frozen=True)
class ImportCosingReport:
    """run() 실행 결과 요약. CLI 는 이 값으로 출력·exit code 를 정한다.

    status 는 항상 "ok" 다 — PubChem 조회(ingredients.resolve)처럼 "이름을
    모른다"는 자연스러운 부재가 없고(파일에 있는 행을 있는 그대로 읽을
    뿐이다), 파일이 없거나 깨졌으면 run() 자체가 처리하지 못한 예외로
    끝나 ImportCosingReport 가 아예 만들어지지 않는다(그 경로는 이 필드로
    관측되지 않는다 — ResolveReport/TrialsReport 의 "failed" 관측 불가와
    같은 계약). 그래도 evidence.pipeline.CollectReport 와 같은 "단일 출처"
    계약을 위해 필드로 남긴다.
    """

    run_id: str
    read: int
    saved: int
    skipped: int
    status: str


def _resolve_fetched_at(csv_path: str) -> str:
    """csv_path 와 같은 디렉터리의 _meta.json 에서 fetched_at 을 읽는다.

    없거나 읽지 못하면(사람이 _meta.json 없이 CSV 만 갖다 둔 경우 등) 지금
    시각(UTC)으로 대체한다 — "언제 반영했는지"는 항상 기록해야 한다는
    원칙(모델의 fetched_at 은 항상 채워져야 한다, T12 보고서 참고)을
    지키되, 다운로드 이력이 없다고 임포트 자체를 막지는 않는다.
    """
    meta_path = Path(csv_path).with_name(META_FILENAME)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        fetched_at = meta.get("fetched_at")
        if isinstance(fetched_at, str) and fetched_at.strip():
            return fetched_at
    except (OSError, ValueError):
        pass
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count_data_rows(csv_path: str) -> int:
    """CSV 의 데이터 행 수(헤더 제외). "읽은 행" 출력을 위한 용도.

    reference.cosing.iter_records() 는 parse_row() 가 None 을 돌려준 행을
    건너뛰며 순회한다(단일 책임: 파싱+필터링을 한 번에) — "건너뛴 행까지
    포함한 전체 행 수"가 필요한 이 함수는 parse_row() 의 판정 로직을 다시
    구현하지 않고, csv.DictReader 로 행 개수만 별도로 센다(파일을 2회
    순회하지만, "수만 행" 규모의 일회성 임포트에서는 무시할 수 있는 비용).
    """
    with open(csv_path, encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def run(conn, path: str, *, on_progress=None) -> ImportCosingReport:
    """CosIng CSV -> IngredientRecord 들 -> upsert(merge 정책), 실행 자체를 RunLog 에 기록한다.

    upsert_records() 가 "merge" 정책(TABLE_FOR[IngredientRecord])으로
    저장하므로, name_key 가 PubChem 이 이미 채운 행과 같으면 inci_name/cas
    는 COALESCE 로, synonyms/sources 는 합집합으로 병합된다 — T12 보고서
    "T13·T14 용 요약" 참고.
    """
    run_log = RunLog(conn)
    run_id = run_log.start("ingredient import-cosing", {"path": path})

    status = "failed"
    try:
        read = _count_data_rows(path)
        fetched_at = _resolve_fetched_at(path)
        records = list(cosing.iter_records(path, fetched_at=fetched_at))
        skipped = read - len(records)

        if on_progress:
            for index, record in enumerate(records, start=1):
                on_progress(index, len(records), record)

        saved = repository.upsert_records(conn, records) if records else 0

        run_log.record_source(
            run_id,
            "cosing",
            requests=0,  # 네트워크 없음 — 로컬 파일만 읽는다
            records=saved,
            errors=0,
            budget_remaining=None,
            stopped_reason=None,
        )

        status = "ok"
        return ImportCosingReport(
            run_id=run_id, read=read, saved=saved, skipped=skipped, status=status
        )
    finally:
        run_log.finish(run_id, status)
