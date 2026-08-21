"""SQLite 저장소.

키 설계
    primary key 는 doi 가 아니라 key 다. OpenAlex 결과의 일부는 DOI 가 없는데
    (학위논문, 일부 컨퍼런스) doi 를 PK 로 쓰면 이들이 서로 충돌하거나 유실된다.
    key = DOI 가 있으면 DOI, 없으면 openalex_id. 둘 다 소문자로 정규화한다.
    DOI 없는 논문은 버리지 않고 verification.has_doi = false 로 표시해 남긴다.

verification 은 두 곳에 쓴다
    조회용으로 개별 컬럼에 펼치고(confidence_score 로 정렬/필터해야 하므로),
    원본 형태는 raw 컬럼의 통합 레코드 JSON 안에 그대로 둔다.
    읽을 때는 raw 를 신뢰한다. 컬럼은 SQL 이 볼 수 있게 복제한 것이다.
"""

import json
import os
import sqlite3

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "out", "papers.db")
DEFAULT_JSON = os.path.join(os.path.dirname(__file__), "out", "papers.json")

# 스펙 8절 레코드 스키마. public_record() 가 내보내는 키의 정의이기도 하다.
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
)
LIST_FIELDS = ("authors", "keywords", "topics")

VERIFICATION_FIELDS = (
    "crossref_verified",
    "title_match",
    "found_in_sources",
    "is_retracted",
    "has_doi",
    "confidence_score",
)

COLUMNS = (
    "key",
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
    "crossref_verified",
    "title_match",
    "found_in_sources",
    "is_retracted",
    "has_doi",
    "confidence_score",
    "collected_at",
    "raw",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    key               TEXT PRIMARY KEY,
    doi               TEXT,
    openalex_id       TEXT,
    title             TEXT,
    authors           TEXT,
    year              INTEGER,
    journal           TEXT,
    abstract          TEXT,
    tldr              TEXT,
    keywords          TEXT,
    topics            TEXT,
    citation_count    INTEGER,
    is_open_access    INTEGER,
    url               TEXT,
    crossref_verified INTEGER,
    title_match       INTEGER,
    found_in_sources  TEXT,
    is_retracted      INTEGER,
    has_doi           INTEGER,
    confidence_score  INTEGER,
    collected_at      TEXT,
    raw               TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_confidence ON papers(confidence_score DESC);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);

CREATE TABLE IF NOT EXISTS cache (
    source     TEXT,
    key        TEXT,
    response   TEXT,
    fetched_at TEXT,
    PRIMARY KEY (source, key)
);
"""


def connect(path=DEFAULT_DB):
    """DB 를 열고 스키마를 보장한다. 이미 있으면 그대로 쓴다."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_key(record):
    identifier = record.get("doi") or record.get("openalex_id") or ""
    return identifier.strip().lower()


def public_record(record):
    """스펙 8절 스키마에 정확히 맞는 dict. 내부 작업용 키는 떨어낸다."""
    result = {}
    for field in RECORD_FIELDS:
        value = record.get(field)
        if value is None and field in LIST_FIELDS:
            value = []
        result[field] = value
    return result


def upsert(conn, record):
    """DOI(또는 openalex_id) 기준 upsert. 같은 논문을 두 번 넣어도 1행이다."""
    clean = public_record(record)
    verification = clean.get("verification") or {}
    row = {
        "key": record_key(clean),
        "doi": clean["doi"],
        "openalex_id": clean["openalex_id"],
        "title": clean["title"],
        "authors": json.dumps(clean["authors"], ensure_ascii=False),
        "year": clean["year"],
        "journal": clean["journal"],
        "abstract": clean["abstract"],
        "tldr": clean["tldr"],
        "keywords": json.dumps(clean["keywords"], ensure_ascii=False),
        "topics": json.dumps(clean["topics"], ensure_ascii=False),
        "citation_count": clean["citation_count"],
        "is_open_access": int(bool(clean["is_open_access"])),
        "url": clean["url"],
        "crossref_verified": int(bool(verification.get("crossref_verified"))),
        "title_match": int(bool(verification.get("title_match"))),
        "found_in_sources": json.dumps(
            verification.get("found_in_sources") or [], ensure_ascii=False
        ),
        "is_retracted": int(bool(verification.get("is_retracted"))),
        "has_doi": int(bool(verification.get("has_doi"))),
        "confidence_score": int(verification.get("confidence_score") or 0),
        "collected_at": clean["collected_at"],
        "raw": json.dumps(clean, ensure_ascii=False),
    }
    names = ", ".join(COLUMNS)
    placeholders = ", ".join(":" + name for name in COLUMNS)
    updates = ", ".join(f"{name}=excluded.{name}" for name in COLUMNS if name != "key")
    conn.execute(
        f"INSERT INTO papers ({names}) VALUES ({placeholders})"
        f" ON CONFLICT(key) DO UPDATE SET {updates}",
        row,
    )
    conn.commit()


def _from_row(row):
    try:
        return json.loads(row["raw"])
    except TypeError, ValueError:
        return None


def all_records(conn):
    rows = conn.execute(
        "SELECT raw FROM papers ORDER BY confidence_score DESC, year DESC"
    ).fetchall()
    return [record for record in (_from_row(row) for row in rows) if record]


def search(conn, keyword, min_confidence=0, include_retracted=False, limit=None):
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


def dump_json(conn, path=DEFAULT_JSON):
    """DB 전체를 JSON 배열로 덮어쓴다. SQLite 가 깨졌을 때의 백업."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    records = all_records(conn)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, indent=1)
    return len(records)
