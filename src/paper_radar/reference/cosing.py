"""CosIng CSV 파서 — 로컬 파일 -> IngredientRecord 이터레이터. 순수 함수만 담는다.

컬럼은 헤더 이름으로 찾는다(순서 의존 금지) — Task 13 브리핑의 "대표 컬럼"은
구성 예시일 뿐이고, 실제 배포 파일의 헤더가 이 순서와 다르거나 컬럼이 더
있어도 헤더 이름만 맞으면 그대로 동작해야 한다. 실제 헤더 원문은 이 모듈이
아니라 tool/fetch_cosing.py 가 _meta.json 의 header_row 로 기록해 검증한다.

Function/Restriction 컬럼(규제 기능 정보)은 이번 범위에서 저장하지 않는다
— ingredient 테이블에 그 컬럼이 없다(models.py IngredientRecord 에 필드가
없다). 소비자가 생기면(예: "레티놀 함량 규제 표시") 그때 스키마를 넓히는
것이 YAGNI 판단이다 — 지금 만들어 두면 아무도 읽지 않는 컬럼을 저장만 하는
꼴이 된다.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import replace

from paper_radar.models import IngredientRecord
from paper_radar.sources.pubchem import name_key

# 실제 배포 파일의 헤더 이름(브리핑 "대표 컬럼" 구성 예시 그대로). 헤더 이름이
# 바뀌면(EU 배포처가 컬럼명을 조용히 바꾸는 사고) parse_row() 가 INCI name 을
# 못 찾아 모든 행을 건너뛰게 된다 — tool/fetch_cosing.py 의 _meta.json
# header_row 로 그 드리프트를 사람이 확인할 수 있다.
_COL_INCI_NAME = "INCI name"
_COL_INN_NAME = "INN name"
_COL_CAS_NO = "CAS No"
_COL_CHEM_IUPAC = "Chem/IUPAC Name / Description"

# fetched_at 은 iter_records() 가 호출자로부터 받은 값으로 나중에 채운다
# (parse_row() 는 파일 하나를 통째로 다루는 iter_records() 와 달리 행 하나만
# 보고, "언제 받았는지"는 알 수 없다 — 그래서 이 플레이스홀더를 넣고
# iter_records() 가 dataclasses.replace() 로 덮어쓴다).
_FETCHED_AT_PLACEHOLDER = ""


def parse_row(row: dict) -> IngredientRecord | None:
    """CSV 한 행(csv.DictReader 가 만든 dict) -> IngredientRecord. 순수 함수.

    INCI name 이 없는(빈 문자열/공백뿐) 행은 None — 이 값이 name_key 의
    재료라 없으면 ingredient 테이블에 조인할 방법이 없다(건너뛴다).
    CAS No 가 빈 문자열이면 None 으로 정규화한다(COALESCE 병합에서 빈
    문자열은 "값 있음"으로 취급돼 기존 값을 지워버리므로 — storage/
    repository.py 의 _norm_str() 과 같은 이유).
    synonyms 는 INN name·Chem/IUPAC 이름 중 실제로 값이 있는 것만 담는다
    (빈 문자열은 제외 — synonyms 는 merge 시 합집합 대상이라, 빈 문자열이
    끼어들면 그 자체가 하나의 "동의어"로 영구히 남는다).
    """
    inci_name = (row.get(_COL_INCI_NAME) or "").strip()
    if not inci_name:
        return None

    cas = (row.get(_COL_CAS_NO) or "").strip() or None
    synonyms = tuple(
        value
        for value in (
            (row.get(_COL_INN_NAME) or "").strip(),
            (row.get(_COL_CHEM_IUPAC) or "").strip(),
        )
        if value
    )

    return IngredientRecord(
        name_key=name_key(inci_name),
        inci_name=inci_name,
        cid=None,  # CosIng 은 CID 를 모른다 — PubChem(T12)이 이미 채웠으면 merge 가 보존한다
        cas=cas,
        synonyms=synonyms,
        sources=("cosing",),
        fetched_at=_FETCHED_AT_PLACEHOLDER,
    )


def iter_records(csv_path: str, fetched_at: str | None = None) -> Iterator[IngredientRecord]:
    """CosIng CSV 파일 -> IngredientRecord 이터레이터.

    utf-8-sig 로 연다 — 엑셀에서 내보낸 CSV 는 흔히 UTF-8 BOM 을 앞에
    붙이는데, 일반 utf-8 로 읽으면 첫 컬럼 헤더("COSING Ref No" 등)
    앞에 BOM 문자가 붙어 그 컬럼만 못 찾게 된다. csv.DictReader 로
    헤더 이름 기준 매핑을 하므로 컬럼 순서는 무관하다.

    parse_row() 가 None 을 돌려준 행(INCI name 없음)은 건너뛴다.
    fetched_at 이 주어지면(호출자가 _meta.json 의 값을 넘긴다) 모든
    레코드의 fetched_at 을 그 값으로 채운다 — 주어지지 않으면(테스트
    등에서 이 함수 하나만 쓸 때) 빈 문자열로 남는다(호출자가 후처리로
    채우거나, 값이 필요 없는 순수 파싱 테스트라는 뜻).
    """
    with open(csv_path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            record = parse_row(row)
            if record is None:
                continue
            if fetched_at is not None:
                record = replace(record, fetched_at=fetched_at)
            yield record
