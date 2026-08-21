"""SQLite 저장소 — papers/store.py 의 대체.

키 설계 (papers/store.py 와 동일)
    primary key 는 doi 가 아니라 key 다. OpenAlex 결과의 일부는 DOI 가 없는데
    (학위논문, 일부 컨퍼런스) doi 를 PK 로 쓰면 이들이 서로 충돌하거나 유실된다.
    key = DOI 가 있으면 DOI, 없으면 openalex_id. 둘 다 소문자로 정규화한다.
    DOI 없는 논문은 버리지 않고 verification.has_doi = false 로 표시해 남긴다.

verification 은 두 곳에 쓴다 (papers/store.py 와 동일)
    조회용으로 개별 컬럼에 펼치고(confidence_score 로 정렬/필터해야 하므로),
    원본 형태는 raw 컬럼의 통합 레코드 JSON 안에 그대로 둔다.
    읽을 때는 raw 를 신뢰한다. 컬럼은 SQL 이 볼 수 있게 복제한 것이다.

구 store.py 와의 차이 — 병합 upsert
    구 upsert() 는 매 호출마다 행 전체를 덮어썼다. 그래서 나중의 빈약한
    재수집(예: 초록/TLDR 없이 다시 긁힌 경우)이 예전의 풍부한 데이터를
    지워버렸다. 이 모듈의 upsert() 는 "내용 필드"는 COALESCE 로 병합하고
    (새 값이 없으면 기존 값을 지키고), "검증 필드"는 매번 이번 실행의
    판정으로 무조건 갱신한다 — 자세한 이유는 upsert() 의 docstring 참고.

dataclass 레코드 upsert (T8 이 T4 로부터 이연받은 기계)
    papers 테이블 밖의 신규 소스(T8 의 Unpaywall 이 첫 사례)가 만드는
    레코드는 dict 가 아니라 models.py 의 frozen dataclass 다. upsert_records()
    는 이 dataclass 들을 TABLE_FOR 매핑(타입 -> (테이블명, 병합정책))에 따라
    범용으로 저장한다 — papers 처럼 태스크마다 손으로 upsert 함수를 새로
    쓰지 않아도 되게 하기 위함이다. 자세한 이유는 upsert_records() docstring
    참고.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import fields

from paper_radar.models import OaLocationRecord, RetractionRecord, TrialRecord
from paper_radar.storage.schema import migrate

# dataclass 레코드 타입 -> (테이블명, 병합정책). "overwrite" = 자연키 충돌 시
# 전 컬럼을 excluded 값으로 무조건 갱신한다(papers.upsert() 의 COALESCE 병합과
# 다르다) — 이런 레코드는 "최신 관측이 진실"이라, 과거 값과 섞으면 이미 사라진
# 상태(예: 죽은 PDF 링크)가 영원히 남는다. 새 dataclass 레코드 타입을 추가하는
# 태스크는 여기 항목 하나만 더하면 upsert_records() 가 자동으로 처리한다.
TABLE_FOR: dict[type, tuple[str, str]] = {
    OaLocationRecord: ("oa_location", "overwrite"),
    RetractionRecord: ("retraction", "overwrite"),
    TrialRecord: ("trial", "overwrite"),
}

# 스펙 8절 레코드 스키마 + is_retracted(버그 수정, 아래 public_record 참고).
# public_record() 가 내보내는 키의 정의이기도 하다.
RECORD_FIELDS = (
    "doi",
    "openalex_id",
    "title",
    "authors",
    "year",
    "journal",
    "abstract",
    "tldr",
    "keywords",
    "topics",
    "citation_count",
    "is_open_access",
    "url",
    "verification",
    "collected_at",
    "is_retracted",
    "mesh_terms",  # T10 — PubMed 가 채우는 MeSH 용어. 의도된 스키마 확장(보고서 참고)
)
LIST_FIELDS = ("authors", "keywords", "topics", "mesh_terms")

# upsert 시 COALESCE(excluded.col, papers.col) 로 병합하는 "내용" 컬럼.
# 재수집이 빈약해도(예: abstract 를 못 얻음) 예전에 얻은 값을 지우지 않는다.
# mesh_terms 도 다른 소스가 채울 수 없는 내용 필드라 여기 포함한다(PubMed
# 조회를 건너뛴 재수집이 예전에 얻은 MeSH 를 지우지 않게).
MERGE_COLUMNS = (
    "title",
    "authors",
    "year",
    "journal",
    "abstract",
    "tldr",
    "keywords",
    "topics",
    "citation_count",
    "is_open_access",
    "url",
    "doi",
    "openalex_id",
    "mesh_terms",
)

# upsert 시 excluded.col 로 무조건 덮어쓰는 "검증" 컬럼 + raw.
# 검증 결과는 이번 실행의 판정이다 — 과거와 병합하면 철회 해제 같은 정정을
# 반영하지 못한다(예: 지난 실행엔 is_retracted=1 이었는데 이번엔 철회가
# 취소됐다는 걸 Crossref 가 알려줘도, 병합하면 예전 1 이 남는다).
ALWAYS_COLUMNS = (
    "collected_at",
    "crossref_verified",
    "title_match",
    "found_in_sources",
    "is_retracted",
    "has_doi",
    "confidence_score",
    "evidence",
    "raw",
)


def connect(path: str) -> sqlite3.Connection:
    """DB 를 열고 스키마를 최신으로 마이그레이션한 뒤 연결을 돌려준다."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    # SQLite 는 연결마다 이 PRAGMA 를 새로 켜야 한다(DB 파일 자체에 저장되는
    # 설정이 아니다) — 꺼진 채로 두면 m0002_runlog.py 의 "ON DELETE CASCADE"
    # 선언이 장식으로만 남는다: run 행을 지워도 run_source/fetch_log 의 관련
    # 행이 고아로 남아, 나중에 run 삭제 API 가 생겼을 때 조용히 데이터가 샌다.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def record_key(record: dict) -> str:
    """doi 가 있으면 doi, 없으면 openalex_id. 소문자로 정규화. 둘 다 없으면 빈 문자열."""
    identifier = record.get("doi") or record.get("openalex_id") or ""
    return identifier.strip().lower()


def public_record(record: dict) -> dict:
    """스펙 8절 스키마 + is_retracted 에 맞는 dict. 내부 작업용 키는 떨어낸다."""
    result = {}
    for field in RECORD_FIELDS:
        value = record.get(field)
        if value is None and field in LIST_FIELDS:
            value = []
        result[field] = value
    # 버그 수정: is_retracted 를 최상위에도 노출한다(중첩된 verification 안이
    # 아니라). 구 store.py 의 public_record() 는 이 필드를 최상위에서
    # 떨어뜨렸다 — 그래서 dump_json 출력을 다시 재채점 파이프라인(verify.build
    # 등, record 최상위의 is_retracted 를 읽는다)에 먹이면 철회 표시가
    # 사라지고, 철회된 논문이 재채점에서 정상 점수를 받아버리는 잠복 버그가
    # 있었다. 항상 verification.is_retracted 에서 진실을 가져온다.
    result["is_retracted"] = bool((record.get("verification") or {}).get("is_retracted"))
    return result


def _norm_str(value):
    """빈 문자열을 NULL 로 정규화한다.

    COALESCE(excluded.col, papers.col) 병합에서 '' 는 NULL 이 아니므로
    "값 있음"으로 취급된다. 정규화하지 않으면 재수집이 필드를 못 채웠을 때
    빈 문자열을 그대로 넣어버려 예전 값을 지운다.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _norm_list_json(value):
    """리스트를 JSON 문자열로 직렬화한다. 빈 리스트는 '[]' 대신 NULL 로.

    이게 이 upsert 함정의 핵심이다: COALESCE 는 '[]' 를 "값 있음"으로 본다.
    빈 리스트를 '[]' 그대로 저장하면 병합이 무력화되어, 재수집에 keywords 가
    안 딸려왔을 때 예전에 모아둔 keywords 를 지워버린다.
    """
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False)


def _norm_bool_or_null(value):
    """None 은 NULL 로(예전 값 보존), 그 외엔 0/1 로."""
    if value is None:
        return None
    return int(bool(value))


# raw 는 INSERT 시점에 함께 쓰지 않는다 — 병합 결과(다른 필드는 COALESCE 로
# 합쳐진 뒤의 값)를 반영해야 하는데, INSERT 문 자체는 "이번에 들어온 값"만
# 알 뿐 병합 후 최종값은 모른다. 그래서 나머지 컬럼을 먼저 upsert 하고
# RETURNING 으로 병합된 행을 돌려받은 뒤, 그걸로 raw 를 다시 조립해서
# 별도 UPDATE 로 써넣는다. _RETURNING_COLUMNS 가 그 재조립에 필요한 컬럼이다.
_RETURNING_COLUMNS = ("key", *MERGE_COLUMNS, *(c for c in ALWAYS_COLUMNS if c != "raw"))


def _unflatten(row) -> dict:
    """upsert 가 RETURNING 으로 받은 병합-후 행을 public_record() 입력 모양으로 되돌린다."""

    def _list(value):
        return json.loads(value) if value else []

    is_open_access = row["is_open_access"]
    evidence = row["evidence"]
    verification = {
        "crossref_verified": bool(row["crossref_verified"]),
        "title_match": bool(row["title_match"]),
        "found_in_sources": _list(row["found_in_sources"]),
        "is_retracted": bool(row["is_retracted"]),
        "has_doi": bool(row["has_doi"]),
        "confidence_score": row["confidence_score"],
    }
    if evidence:
        verification["evidence"] = json.loads(evidence)
    return {
        "doi": row["doi"],
        "openalex_id": row["openalex_id"],
        "title": row["title"],
        "authors": _list(row["authors"]),
        "year": row["year"],
        "journal": row["journal"],
        "abstract": row["abstract"],
        "tldr": row["tldr"],
        "keywords": _list(row["keywords"]),
        "topics": _list(row["topics"]),
        "citation_count": row["citation_count"],
        "is_open_access": None if is_open_access is None else bool(is_open_access),
        "url": row["url"],
        "verification": verification,
        "collected_at": row["collected_at"],
        "mesh_terms": _list(row["mesh_terms"]),
    }


def upsert(conn: sqlite3.Connection, record: dict, verification: dict) -> None:
    """DOI(또는 openalex_id) 기준 upsert. 같은 논문을 두 번 넣어도 1행이다.

    내용 필드(MERGE_COLUMNS)는 COALESCE(excluded.col, papers.col) 로 병합한다
    — 새 값이 비어 있으면(빈 값 정규화 후 NULL) 기존 값을 지키므로, 나중의
    빈약한 재수집이 예전의 풍부한 필드(abstract, tldr 등)를 지우지 않는다.

    검증 필드(ALWAYS_COLUMNS)는 excluded.col 로 무조건 덮어쓴다 — verification
    은 이번 실행이 계산한 이번 실행의 판정이라, 과거와 병합하면 정정(예:
    철회 해제, confidence_score 재계산)을 반영할 수 없다.

    raw 컬럼(all_records/search/dump_json 이 신뢰하는 통합 JSON)은 병합이
    끝난 뒤의 값으로 다시 조립한다 — 그렇지 않으면 개별 컬럼은 옛 abstract
    를 지키는데 raw 는 이번에 들어온 빈 abstract 를 담아, 병합의 의미가
    없어진다.
    """
    combined = dict(record)
    combined["verification"] = verification
    clean = public_record(combined)

    key = record_key(clean)
    row = {
        "key": key,
        "doi": _norm_str(clean["doi"]),
        "openalex_id": _norm_str(clean["openalex_id"]),
        "title": _norm_str(clean["title"]),
        "authors": _norm_list_json(clean["authors"]),
        "year": clean["year"],
        "journal": _norm_str(clean["journal"]),
        "abstract": _norm_str(clean["abstract"]),
        "tldr": _norm_str(clean["tldr"]),
        "keywords": _norm_list_json(clean["keywords"]),
        "topics": _norm_list_json(clean["topics"]),
        "mesh_terms": _norm_list_json(clean["mesh_terms"]),
        "citation_count": clean["citation_count"],
        "is_open_access": _norm_bool_or_null(clean["is_open_access"]),
        "url": _norm_str(clean["url"]),
        "crossref_verified": int(bool(verification.get("crossref_verified"))),
        "title_match": int(bool(verification.get("title_match"))),
        "found_in_sources": _norm_list_json(verification.get("found_in_sources") or []),
        "is_retracted": int(bool(verification.get("is_retracted"))),
        "has_doi": int(bool(verification.get("has_doi"))),
        "confidence_score": int(verification.get("confidence_score") or 0),
        "collected_at": clean["collected_at"],
        "evidence": (
            json.dumps(verification["evidence"], ensure_ascii=False)
            if verification.get("evidence")
            else None
        ),
    }

    names = ", ".join(_RETURNING_COLUMNS)
    placeholders = ", ".join(":" + name for name in _RETURNING_COLUMNS)
    merge_updates = ", ".join(f"{c}=COALESCE(excluded.{c}, papers.{c})" for c in MERGE_COLUMNS)
    always_updates = ", ".join(f"{c}=excluded.{c}" for c in ALWAYS_COLUMNS if c != "raw")
    updates = ", ".join((merge_updates, always_updates))
    merged = conn.execute(
        f"INSERT INTO papers ({names}) VALUES ({placeholders})"
        f" ON CONFLICT(key) DO UPDATE SET {updates}"
        f" RETURNING {', '.join(_RETURNING_COLUMNS)}",
        row,
    ).fetchone()

    raw_record = public_record(_unflatten(merged))
    conn.execute(
        "UPDATE papers SET raw = ? WHERE key = ?",
        (json.dumps(raw_record, ensure_ascii=False), key),
    )
    conn.commit()


def _from_row(row):
    try:
        return json.loads(row["raw"])
    except (TypeError, ValueError):
        return None


def all_records(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT raw FROM papers ORDER BY confidence_score DESC, year DESC"
    ).fetchall()
    return [record for record in (_from_row(row) for row in rows) if record]


def search(
    conn: sqlite3.Connection,
    keyword: str,
    min_confidence: int = 0,
    include_retracted: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """제목·초록·키워드에서 대소문자 무시 부분 일치. 신뢰도 내림차순."""
    pattern = f"%{(keyword or '').strip().lower()}%"
    sql = (
        "SELECT raw FROM papers"
        " WHERE (LOWER(title) LIKE :pattern"
        "        OR LOWER(abstract) LIKE :pattern"
        "        OR LOWER(keywords) LIKE :pattern)"
        "   AND confidence_score >= :min_confidence"
    )
    params = {"pattern": pattern, "min_confidence": min_confidence}
    if not include_retracted:
        sql += " AND is_retracted = 0"
    sql += " ORDER BY confidence_score DESC, year DESC"
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = limit
    rows = conn.execute(sql, params).fetchall()
    return [record for record in (_from_row(row) for row in rows) if record]


def dump_json(conn: sqlite3.Connection, path: str) -> int:
    """DB 전체를 JSON 배열로 덮어쓴다. SQLite 가 깨졌을 때의 백업."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    records = all_records(conn)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, indent=1)
    return len(records)


def _record_row(record) -> dict:
    """dataclass 인스턴스를 SQL 바인딩 가능한 값으로 변환한다.

    tuple 필드(예: TrialRecord.conditions)는 JSON 문자열로, bool 필드는 0/1
    로 직렬화한다 — sqlite3 는 tuple 을 바인딩할 수 없고, bool 은 바인딩은
    되지만 0/1 로 저장해야 다른 정수 컬럼과 같은 방식으로 조회·비교된다.
    """
    row = {}
    for field in fields(record):
        value = getattr(record, field.name)
        if isinstance(value, tuple):
            row[field.name] = json.dumps(list(value), ensure_ascii=False)
        elif isinstance(value, bool):
            row[field.name] = int(value)
        else:
            row[field.name] = value
    return row


def upsert_records(conn: sqlite3.Connection, records: Iterable) -> int:
    """models.py 의 frozen dataclass 레코드를 TABLE_FOR 매핑에 따라 upsert 한다.

    각 레코드 타입의 NATURAL_KEY(ClassVar[tuple[str, ...]])로 ON CONFLICT
    절을 만든다. 정책이 "overwrite"(지금 등록된 유일한 정책)면 자연키가
    충돌할 때 자연키가 아닌 모든 컬럼을 excluded 값으로 무조건 덮어쓴다 —
    papers.upsert() 의 COALESCE 병합과 달리 옛 값을 지키지 않는다(이유는
    TABLE_FOR 주석 참고: 이런 레코드는 "최신 관측이 곧 진실"이다).

    등록되지 않은 타입을 만나면 KeyError(등록된 타입 이름 목록 포함).
    반환값은 upsert 한 레코드 수(records 를 소비한 개수, 실패 없이 전부
    처리했다는 전제 — 실패하면 예외가 그대로 전파되고 그때까지 처리한 것도
    commit 되지 않는다).

    자연키 컬럼이 None 이면 저장 전에 '' 로 강제한다(T9, RetractionRecord.
    retraction_doi 가 첫 사례) — SQLite 는 PK(및 그걸 구성하는 컬럼)에도
    역사적으로 NULL 을 허용하고, NULL 은 자기 자신과도 "다르다"고 비교되어
    PK/UNIQUE 제약이 NULL 값의 중복을 걸러내지 못한다. 그대로 두면 "공지
    DOI 미상"인 행을 여러 번 upsert 할 때마다 자연키 충돌 없이 새 행이
    조용히 계속 쌓인다.
    """
    count = 0
    for record in records:
        record_type = type(record)
        if record_type not in TABLE_FOR:
            known = ", ".join(sorted(t.__name__ for t in TABLE_FOR)) or "(없음)"
            raise KeyError(
                f"upsert_records: 등록되지 않은 레코드 타입 {record_type.__name__!r} "
                f"(TABLE_FOR 에 등록된 타입: {known})"
            )
        table, policy = TABLE_FOR[record_type]
        if policy != "overwrite":
            # 지금은 "overwrite" 하나뿐이다 — 새 정책이 필요해지면(예: papers 처럼
            # 부분 병합) 여기 분기를 늘린다. 모르는 정책 문자열로 조용히 아무
            # 갱신도 안 하는 SQL 을 만들지 않기 위해 즉시 실패한다.
            raise ValueError(f"upsert_records: 알 수 없는 병합 정책 {policy!r} ({table})")

        row = _record_row(record)
        natural_key = record_type.NATURAL_KEY
        for column in natural_key:
            if row.get(column) is None:
                row[column] = ""
        columns = list(row)
        names = ", ".join(columns)
        placeholders = ", ".join(":" + name for name in columns)
        conflict_columns = ", ".join(natural_key)
        updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in natural_key)

        conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})"
            f" ON CONFLICT({conflict_columns}) DO UPDATE SET {updates}",
            row,
        )
        count += 1
    conn.commit()
    return count


def _trial_from_row(row) -> TrialRecord:
    """trial 테이블의 행 -> TrialRecord. conditions/interventions 는 JSON 왕복,
    results_posted 는 0/1 -> bool 로 되돌린다(upsert_records()/_record_row() 의
    반대 방향 변환)."""

    def _tuple(value):
        return tuple(json.loads(value)) if value else ()

    return TrialRecord(
        nct_id=row["nct_id"],
        title=row["title"],
        status=row["status"],
        phase=row["phase"],
        sponsor_class=row["sponsor_class"],
        enrollment=row["enrollment"],
        conditions=_tuple(row["conditions"]),
        interventions=_tuple(row["interventions"]),
        outcomes_json=row["outcomes_json"],
        first_posted=row["first_posted"],
        results_posted=bool(row["results_posted"]),
        url=row["url"],
        matched_query=row["matched_query"],
        captured_at=row["captured_at"],
    )


def search_trials(
    conn: sqlite3.Connection, keyword: str, limit: int | None = 20
) -> list[TrialRecord]:
    """title/conditions/interventions 에서 대소문자 무시 부분 일치. first_posted 내림차순.

    conditions/interventions 는 JSON 배열 문자열로 저장돼 있어(예:
    '["Sunburn"]') LIKE 검색이 그 원문 문자열 안에서 부분 일치를 찾는
    형태다 — 별도 정규화 없이도 성분/조건 이름이 그대로 들어 있으면 걸린다.
    """
    pattern = f"%{(keyword or '').strip().lower()}%"
    sql = (
        "SELECT * FROM trial"
        " WHERE (LOWER(title) LIKE :pattern"
        "        OR LOWER(conditions) LIKE :pattern"
        "        OR LOWER(interventions) LIKE :pattern)"
        " ORDER BY first_posted DESC"
    )
    params = {"pattern": pattern}
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = limit
    rows = conn.execute(sql, params).fetchall()
    return [_trial_from_row(row) for row in rows]


def oa_pdf_urls(conn: sqlite3.Connection, dois: Iterable[str]) -> dict[str, str]:
    """주어진 DOI 들 중 oa_location 에 pdf_url 이 저장된 것만 {doi: pdf_url} 로.

    cli.py 의 cite 출력이 검색 결과를 순회하기 전, conn 이 아직 열려 있는
    동안 한 번에 배치 조회하기 위한 헬퍼다 — conn.close() 이후 레코드마다
    다시 쿼리하려는 실수를 구조적으로 막는다. 대소문자·트림 정규화는 하지
    않는다(papers.doi 도 oa_location.doi 도 이미 소문자·트림된 값만 저장하는
    것이 두 upsert 경로의 공통 규약이다).
    """
    doi_list = [d for d in dois if d]
    if not doi_list:
        return {}
    placeholders = ", ".join("?" for _ in doi_list)
    rows = conn.execute(
        f"SELECT doi, pdf_url FROM oa_location WHERE doi IN ({placeholders})"
        " AND pdf_url IS NOT NULL",
        doi_list,
    ).fetchall()
    return {row["doi"]: row["pdf_url"] for row in rows}
