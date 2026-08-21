"""마이그레이션 목록 — 순서 있는 (번호, 이름, apply) 튜플.

번호는 그대로 PRAGMA user_version 값이 된다. 번호를 건너뛰거나 재사용하면
같은 DB 를 서로 다른 버전의 코드로 열었을 때 어느 쪽이 "최신"인지 판단할
방법이 없어진다 — 그래서 번호는 이 목록에서만 배정하고, 항상 마지막 항목
뒤에 오름차순으로 이어 붙인다. 기존 번호의 apply 를 사후 수정하지 않는다
(이미 그 번호를 겪은 DB 와 아직 안 겪은 DB 가 서로 다른 결과를 갖게 된다 —
바꿔야 하면 새 번호의 마이그레이션을 추가한다).
"""

from __future__ import annotations

from collections.abc import Callable
from sqlite3 import Connection

from paper_radar.storage.migrations import (
    m0001_baseline,
    m0002_runlog,
    m0003_evidence_column,
    m0004_oa_location,
    m0005_retraction,
    m0006_mesh_terms,
)

# (번호, 이름, apply) — apply(conn) 는 반환값 없이 conn 에 스키마 변경을 적용한다.
MIGRATIONS: tuple[tuple[int, str, Callable[[Connection], None]], ...] = (
    (1, "baseline", m0001_baseline.apply),
    (2, "runlog", m0002_runlog.apply),
    (3, "evidence_column", m0003_evidence_column.apply),
    (4, "oa_location", m0004_oa_location.apply),
    (5, "retraction", m0005_retraction.apply),
    (6, "mesh_terms", m0006_mesh_terms.apply),
)
