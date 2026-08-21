"""unmatched 검수 보조 — PubChem/CosIng ingredient 테이블에서 동의어 후보를 제안한다.

trend.unmatched 이 만드는 `unmatched_{query_id}_keywords.csv` 는 사람이
verdict(lexicon/stopword/keep)를 채우는 작업 목록이다. ingredient
테이블(T12 PubChem + T13 CosIng)이 생겼으니, 그 미매칭 표현이 이미 알려진
성분의 name_key/inci_name/synonym 과 정확히 같으면 "이거 아마 이 성분
아닐까요?"라고 제안해 검수 속도를 높인다.

자동 등록은 절대 하지 않는다 — 이 원칙은 기존 설계 결정이다(trend/
unmatched.py 모듈 docstring 참고: "population" 이나 "chromatography" 처럼
빈도만 높고 신호가 아닌 표현이 표준키가 되면 되돌리기 어렵다). 그래서 이
모듈은:
    - keyword_lexicon.json / stopwords.json / unmatched CSV 를 전혀 고치지
      않는다. 별도 파일(lexicon_suggestions.csv)만 새로 낸다.
    - 그 출력 파일에도 verdict 컬럼을 비워 둔다 — 제안은 결정이 아니다.
    - 정확 일치만 본다. 부분 일치·유사도(fuzzy) 매칭은 하지 않는다: 거짓
      양성이 섞이면 "제안은 믿을 만하다"는 전제가 깨져 검수자가 매번 다시
      원문을 대조해야 하고, 그러면 이 도구가 시간을 절약해주지 못한다.

매칭에 쓰는 정규화는 trend.normalize.normalize_term() 하나로 통일한다 —
unmatched 의 term 도, ingredient 의 name_key/inci_name/synonyms 도 전부 이
함수를 거친 뒤에만 비교한다. ingredient.name_key 는 이미
sources.pubchem.name_key() 로 정규화돼 있지만(casefold 기준), 그 규칙과
normalize_term()(NFKC + 구분자 정규화)이 다르므로 다시 한 번 통일된 규칙을
거쳐야 term 쪽과 공정하게 비교된다.
"""

from __future__ import annotations

import csv
from dataclasses import asdict
from pathlib import Path

from paper_radar.storage import repository
from paper_radar.trend import normalize, records, unmatched

OUT_DIR = records.REPO_ROOT / "out" / "trend"

# unmatched 파일 중 이 태스크가 다루는 필드는 keywords_norm 하나뿐이다(브리핑
# 명세: "out/trend/{query_id}/unmatched_{query_id}_keywords.csv"). topics/
# concepts 미매칭까지 제안하는 것은 이 태스크 범위 밖이다.
UNMATCHED_FIELD = "keywords_norm"

FIELDS = [
    "term",
    "paper_count",
    "matched_name_key",
    "matched_via",
    "inci_name",
    "cas",
    "sources",
    "suggested_key",
    "verdict",
]


def _suggested_key(name_key: str) -> str:
    """name_key(정규화된 소문자, 공백 구분) -> 사전 표준키 형식(대문자 스네이크).

    예: "zinc oxide" -> "ZINC_OXIDE". keyword_lexicon.json 의 실제 등록
    여부는 build_suggestions() 가 별도로 확인해 "(기존 키 있음)"을 병기한다
    — 이 함수는 형식 변환만 한다.
    """
    return name_key.strip().upper().replace(" ", "_")


def _ingredient_candidates(ingredient: dict):
    """ingredient 한 행에서 (정규화된 표면형, matched_via) 후보 목록.

    우선순위(먼저 나온 것이 이긴다): name_key -> inci_name -> synonyms(원본
    등장 순서). matched_via 의 synonym 항목은 정규화 전 원문을 그대로
    담는다(브리핑: "synonym:<원문>") — 검수자가 어떤 표현이 걸렸는지 눈으로
    봐야 한다.
    """
    candidates = []
    name_key = ingredient.get("name_key")
    if name_key:
        candidates.append((normalize.normalize_term(name_key), "name_key"))
    inci_name = ingredient.get("inci_name")
    if inci_name:
        candidates.append((normalize.normalize_term(inci_name), "inci_name"))
    for synonym in ingredient.get("synonyms") or ():
        if synonym:
            candidates.append((normalize.normalize_term(synonym), f"synonym:{synonym}"))
    return candidates


def _index_ingredients(ingredients):
    """정규화된 표면형 -> (ingredient, matched_via) 색인.

    같은 표면형이 여러 ingredient/후보에 걸리면 먼저 만들어진 항목을
    지킨다(입력 순서상 앞선 ingredient, 그 안에서는 name_key 가 synonym 을
    이긴다) — 정확 일치만 다루므로 이런 충돌은 실제로는 드물다.
    """
    index = {}
    for ingredient in ingredients:
        for normalized, matched_via in _ingredient_candidates(ingredient):
            if normalized and normalized not in index:
                index[normalized] = (ingredient, matched_via)
    return index


def build_suggestions(unmatched_rows, ingredients, lexicon_keys=None):
    """unmatched 행 term 을 ingredient 테이블과 정확 일치로만 대조한다. 순수 함수.

    lexicon_keys 는 keyword_lexicon.json 에 이미 등록된 표준키 집합이다.
    생략하면(테스트가 아닌 실제 실행) normalize.load_lexicon() 으로 실제
    사전을 읽는다 — trend.normalize.normalize_all() 의 alias_map=None 기본값
    관례(사전을 스스로 로드하되 테스트는 주입해 격리)를 그대로 따른다.

    매칭 없는 unmatched 행은 결과에서 뺀다(브리핑: "제안 파일은 짧을수록
    좋다"). 정렬은 paper_count 내림차순, 동률이면 term 오름차순(trend.
    unmatched.to_rows() 와 같은 tie-break 관례).
    """
    if lexicon_keys is None:
        entries, _alias_map = normalize.load_lexicon()
        lexicon_keys = set(entries)

    index = _index_ingredients(ingredients)

    rows = []
    for row in unmatched_rows:
        term = row.get("term") or ""
        normalized = normalize.normalize_term(term)
        hit = index.get(normalized) if normalized else None
        if hit is None:
            continue
        ingredient, matched_via = hit

        suggested_key = _suggested_key(ingredient.get("name_key") or "")
        if suggested_key in lexicon_keys:
            # 이미 사전에 있는 표준키다 — 새로 추가하라는 게 아니라, 이
            # term 을 그 표준키의 별칭(alias)으로 추가하라는 제안임을 표시.
            suggested_key = f"{suggested_key} (기존 키 있음)"

        try:
            paper_count = int(row.get("paper_count") or 0)
        except (TypeError, ValueError):
            paper_count = 0

        rows.append(
            {
                "term": term,
                "paper_count": paper_count,
                "matched_name_key": ingredient.get("name_key") or "",
                "matched_via": matched_via,
                "inci_name": ingredient.get("inci_name") or "",
                "cas": ingredient.get("cas") or "",
                "sources": ", ".join(ingredient.get("sources") or ()),
                "suggested_key": suggested_key,
                "verdict": "",  # 사람 몫 — 제안은 결정이 아니다.
            }
        )

    rows.sort(key=lambda r: (-r["paper_count"], r["term"]))
    return rows


def default_path(query_id, out_dir=OUT_DIR):
    """out/trend/{query_id}/lexicon_suggestions.csv."""
    return Path(out_dir) / query_id / "lexicon_suggestions.csv"


def run(query_id, db_path, out_dir=OUT_DIR):
    """파일 IO 조립: unmatched CSV 읽기 -> ingredient 테이블 전체 읽기 ->
    build_suggestions() -> lexicon_suggestions.csv 쓰기.

    ingredient 테이블이 비어 있으면(resolve/import-cosing 을 아직 안 돌린
    경우) 오류가 아니라 안내 상태로 돌아간다 — 빈 파일도 만들지 않는다
    (브리핑 지시: "빈 제안 파일 + 안내 메시지 — 오류가 아니다"를 CLI 가
    출력하려면 이 함수가 그 신호를 ingredient_table_empty 로 넘겨야 한다).
    """
    unmatched_path = unmatched.default_path(query_id, UNMATCHED_FIELD, out_dir=out_dir)
    with open(unmatched_path, encoding="utf-8-sig", newline="") as handle:
        unmatched_rows = list(csv.DictReader(handle))

    conn = repository.connect(db_path)
    try:
        ingredient_records = repository.list_ingredients(conn)
    finally:
        conn.close()

    if not ingredient_records:
        return {
            "reviewed": len(unmatched_rows),
            "suggested": 0,
            "target": None,
            "rows": [],
            "ingredient_table_empty": True,
        }

    ingredients = [asdict(record) for record in ingredient_records]
    rows = build_suggestions(unmatched_rows, ingredients)

    target = default_path(query_id, out_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    return {
        "reviewed": len(unmatched_rows),
        "suggested": len(rows),
        "target": target,
        "rows": rows,
        "ingredient_table_empty": False,
    }
