"""papers.mesh_terms 컬럼 추가 — 마이그레이션 0006.

PubMed(T10)가 채우는 MeSH(Medical Subject Headings) 용어 목록을 JSON 배열
문자열로 담는다. 예: '["Retinol", "Skin Aging"]'.

NULL 의 의미는 두 가지를 하나로 묶는다 — "아직 PubMed 를 조회하지 않았다"와
"조회했지만 그 논문에 MeSH 가 없었다(또는 PubMed 가 그 DOI 를 색인하지
않았다)"를 컬럼 하나로는 구분하지 않는다. 구분이 필요해지면(예: "PubMed
조회를 이미 시도했는가"를 알아야 하는 경우) evidence 컬럼(papers.evidence,
소스별 원본 응답)에서 "pubmed" 키의 존재 여부로 판단할 수 있다 — 이 컬럼은
어디까지나 병합·검색에 쓰는 평면화된 값일 뿐이다.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE papers ADD COLUMN mesh_terms TEXT")
