"""3단계: 키워드 정규화. 이 모듈이 부실하면 나머지가 전부 무의미하다.

    python -m papers_trend.normalize --profile sunscreen

원칙: 사전과 파서를 분리한다.
    지표 산출은 사전에만 의존한다. 정규화 함수는 사전을 읽어 적용할 뿐,
    어떤 표현이 어떤 표준키인지를 코드에 적지 않는다.
    keyword_lexicon.json 과 stopwords.json 만 고쳐서 사전을 키운다.

정규화 순서 (고정)
    1. 소문자화
    2. 유니코드 NFKC 정규화, 앞뒤 공백 제거
    3. 하이픈·언더스코어·연속 공백 -> 단일 공백
    4. 화학식 표기 통일. TiO₂ 의 아래첨자는 2단계 NFKC 가 이미 '2' 로 바꾼다.
       TiO2 <-> titanium dioxide 같은 이름-화학식 대응은 사전 aliases 가 맡는다.
    5. 단복수 통일. aliases 에 양쪽을 다 넣는 방식이다. 스테머를 쓰지 않는다.
    6. 사전 조회 -> 표준키 치환
    7. 사전에 없으면 정규화된 문자열 그대로 통과. 버리지 않는다.

7번이 중요하다. 미매칭 표현을 버리면 unmatched.py 가 일할 재료가 없어진다.

사전과 불용어의 우선순위
    사전이 이긴다. 사전 조회를 먼저 하고, 매칭되지 않은 것만 불용어로 걸러낸다.
    'dermatology' 처럼 분야명이면서 의미 있는 신호를 사전에 일부러 등록했을 때
    불용어가 그것을 지워버리면 안 된다.

한 논문 안의 중복
    'zno' 와 'zinc oxide' 가 같은 논문에 둘 다 있으면 ZINC_OXIDE 하나로 접는다.
    접지 않으면 paper_count 가 부풀어 prevalence 가 조용히 틀린 값이 된다.
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from . import records

HERE = Path(__file__).resolve().parent
LEXICON_PATH = HERE / "keyword_lexicon.json"
STOPWORDS_PATH = HERE / "stopwords.json"

_SEPARATORS = re.compile(r"[-_/\s]+")
_KEYWORD_FIELDS = ("keywords", "topics", "concepts")


def normalize_term(text):
    """표면형 정규화. 1~5단계. 사전을 보지 않는다."""
    if not text:
        return ""
    lowered = str(text).lower()
    folded = unicodedata.normalize("NFKC", lowered).strip()
    return _SEPARATORS.sub(" ", folded).strip()


def load_lexicon(path=LEXICON_PATH):
    """(entries, alias_map) 를 돌려준다. alias_map: 정규화표면형 -> 표준키."""
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    entries = {key: value for key, value in raw.items() if not key.startswith("_")}

    alias_map = {}
    collisions = []
    for key, entry in entries.items():
        surfaces = [entry.get("canonical_en", ""), key.replace("_", " ")]
        surfaces += entry.get("aliases") or []
        for surface in surfaces:
            normalized = normalize_term(surface)
            if not normalized:
                continue
            existing = alias_map.get(normalized)
            if existing and existing != key:
                collisions.append((normalized, existing, key))
                continue
            alias_map[normalized] = key
    if collisions:
        # 같은 표현이 두 표준키를 가리키면 어느 쪽으로 집계되는지 알 수 없다.
        detail = "; ".join(f"{s!r} -> {a} / {b}" for s, a, b in collisions[:5])
        raise ValueError(
            f"사전에 중복 별칭이 {len(collisions)}건 있습니다. 먼저 고치세요: {detail}"
        )
    return entries, alias_map


def load_stopwords(path=STOPWORDS_PATH):
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return {normalize_term(word) for word in (raw.get("field_labels") or [])}


def resolve(term, alias_map, entries, stopwords):
    """표현 하나를 해석한다. 걸러낼 것이면 None.

    돌려주는 dict:
        keyword_key    집계 식별자. 사전에 있으면 표준키, 없으면 정규화 표면형
        canonical_en   표시용 이름
        category       사전에 있을 때만
        is_in_lexicon  사전 등록 여부. 큐레이션된 키와 통과된 원시 표현을 구분한다
    """
    normalized = normalize_term(term)
    if not normalized:
        return None
    key = alias_map.get(normalized)
    if key:  # 사전이 불용어보다 우선한다
        entry = entries[key]
        return {
            "keyword_key": key,
            "canonical_en": entry.get("canonical_en") or key,
            "category": entry.get("category") or "",
            "is_in_lexicon": True,
        }
    if normalized in stopwords:
        return None
    return {
        "keyword_key": normalized,
        "canonical_en": normalized,
        "category": "",
        "is_in_lexicon": False,
    }


def resolve_field(terms, alias_map, entries, stopwords):
    """한 논문의 한 필드. 같은 표준키로 접히는 것들을 하나로 만든다."""
    collapsed = {}
    for term in terms or []:
        resolved = resolve(term, alias_map, entries, stopwords)
        if resolved:
            collapsed.setdefault(resolved["keyword_key"], resolved)
    return list(collapsed.values())


def normalize_record(record, alias_map, entries, stopwords):
    """레코드에 keywords_norm / topics_norm / concepts_norm 을 붙인다.

    원본 필드는 그대로 남긴다. 사전을 고치고 다시 돌릴 때 원본이 필요하다.
    """
    result = dict(record)
    for field in _KEYWORD_FIELDS:
        result[f"{field}_norm"] = resolve_field(record.get(field), alias_map, entries, stopwords)
    return result


def normalize_all(record_list, alias_map=None, entries=None, stopwords=None):
    if alias_map is None or entries is None:
        entries, alias_map = load_lexicon()
    if stopwords is None:
        stopwords = load_stopwords()
    return [normalize_record(r, alias_map, entries, stopwords) for r in record_list]


def summarize(record_list, normalized):
    """정규화가 실제로 일했는지 보여주는 숫자들."""

    def unique(rows, field):
        return {term for row in rows for term in (row.get(field) or [])}

    def unique_norm(rows, field):
        return {item["keyword_key"] for row in rows for item in (row.get(field) or [])}

    report = {}
    for field in _KEYWORD_FIELDS:
        before = unique(record_list, field)
        after = unique_norm(normalized, f"{field}_norm")
        in_lexicon = {
            item["keyword_key"]
            for row in normalized
            for item in (row.get(f"{field}_norm") or [])
            if item["is_in_lexicon"]
        }
        report[field] = {
            "unique_before": len(before),
            "unique_after": len(after),
            "reduction": len(before) - len(after),
            "lexicon_keys_hit": len(in_lexicon),
        }
    return report


def top_terms(normalized, field="keywords_norm", limit=20):
    counter = Counter(item["canonical_en"] for row in normalized for item in (row.get(field) or []))
    return counter.most_common(limit)


def top_raw_terms(record_list, field="keywords", limit=20):
    counter = Counter(term for row in record_list for term in (row.get(field) or []))
    return counter.most_common(limit)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m papers_trend.normalize",
        description="사전을 적용해 키워드를 표준키로 접는다.",
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args(argv)

    entries, alias_map = load_lexicon()
    stopwords = load_stopwords()
    record_list = records.load_records(args.profile)
    normalized = normalize_all(record_list, alias_map, entries, stopwords)

    print(f"[{args.profile}] 레코드 {len(record_list):,}건")
    print(f"  사전 항목 {len(entries)}개, 별칭 {len(alias_map)}개, 불용어 {len(stopwords)}개\n")

    print("=== 정규화 전/후 고유 표현 수 ===")
    for field, stat in summarize(record_list, normalized).items():
        print(
            f"  {field:9} {stat['unique_before']:>6,} -> {stat['unique_after']:>6,}"
            f"  (감소 {stat['reduction']:,} / 사전 적중 {stat['lexicon_keys_hit']}개)"
        )

    print(f"\n=== 불용어·정규화 전 상위 {args.top} (keywords 원본) ===")
    for term, count in top_raw_terms(record_list, "keywords", args.top):
        print(f"  {count:>5}  {term}")

    print(f"\n=== 불용어·정규화 후 상위 {args.top} (keywords) ===")
    for term, count in top_terms(normalized, "keywords_norm", args.top):
        print(f"  {count:>5}  {term}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
