"""PubChem PUG-REST — 성분 실체 해소(name/CAS/CID/동의어를 한 실체로 묶는 백본).

"niacinamide / nicotinamide / vitamin B3 / CAS 98-92-0 / CID 936" 을 한 실체로
묶는다. keyword_lexicon.json 확장(T14)과 CosIng 조인(T13)의 기반이 되는 소스다.

API 사실 (2026-08 조사, 실측 CID 936 = niacinamide)
    - 이름→CID: GET .../compound/name/{name}/cids/JSON
      -> {"IdentifierList": {"CID": [936]}}. 모르는 이름 = 404.
    - CID→동의어: GET .../compound/cid/{cid}/synonyms/JSON
      -> {"InformationList": {"Information": [{"CID": 936, "Synonym": [...]}]}}
    - 한도: 무키 5req/s / 400req/min. 예산 헤더 없음(BudgetTracker 는 관측만
      하고 지나간다 — unpaywall.py 와 같은 사정).

두 단계 조회 — 이름→CID, CID→동의어
    이름만으로는 CAS/동의어를 못 얻는다. 먼저 name/{name}/cids 로 CID 를
    얻고, 그 CID 로 cid/{cid}/synonyms 를 다시 조회해야 한다(pubmed.py 의
    esearch→efetch 와 같은 결의 2단계 조회 — 순차 호출이라 페이스 제한이
    두 번 다 걸린다는 뜻이지, 병렬로 나가는 게 아니다).

동의어 절단 — 앞 50개만 저장
    일부 화합물은 동의어가 수백 개(상표명·여러 언어 표기 등)다. 상한 없이
    그대로 저장하면 DB 가 사실상 동의어 덤프가 된다 — PubChem 응답이
    대체로 관련성 순으로 오는 경향을 이용해 앞 50개만 IngredientRecord.
    synonyms 에 담는다. CAS 추출(extract_cas)은 이 절단 "전" 원본 전체
    목록에 대해 수행한다 — 절단 후 목록만 보면 CAS 가 51번째 이후에 있는
    경우를 놓칠 수 있다.

CAS 번호 추출
    동의어 목록 안에 CAS 등록번호가 순수 문자열로 섞여 있다(예: "98-92-0").
    정규식 ^\\d{2,7}-\\d{2}-\\d$ 로 첫 매치를 채택한다(브리핑 API 사실 절 실측).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import ClassVar
from urllib.parse import quote

from paper_radar.contract import Fetch, SourcePolicy
from paper_radar.models import IngredientRecord
from paper_radar.registry import register

BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/"

# PubChem 응답에 담긴 동의어 중 저장할 상한 — 모듈 docstring "동의어 절단" 참고.
SYNONYM_LIMIT = 50

# CAS 등록번호 형태: 2~7자리-2자리-1자리(체크디지트). 동의어 목록 안에서 이
# 패턴에 처음 매치하는 문자열을 CAS 로 채택한다.
_CAS_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")


@register
class PubChem:
    """레지스트리 등록용 얇은 표지 — 정책 선언 외에 상태를 갖지 않는다."""

    key: ClassVar[str] = "pubchem"
    policy: ClassVar[SourcePolicy] = SourcePolicy(
        host="pubchem.ncbi.nlm.nih.gov",
        # 무키 상한 5req/s(=400req/min) — 정확히 상한에 걸치지 않도록 0.2초
        # (정확히 5req/s)로 둔다.
        min_interval_s=0.2,
    )


def name_key(name: str) -> str:
    """이름 정규화: 앞뒤 공백 제거 + casefold + 내부 연속 공백을 하나로 축약.

    "Niacinamide", " niacinamide ", "Niacin  Amide" 처럼 표기가 다른 같은
    이름을 하나의 실체 키로 묶기 위한 조인 키다(ingredient 테이블 PK).
    casefold() 는 lower() 보다 더 적극적인 대소문자 무시라 비-ASCII 이름도
    안전하게 접는다. T13(CosIng 조인)·T14(사전 확장)가 그대로 재사용한다.
    """
    return " ".join(name.strip().casefold().split())


def extract_cas(synonyms: Iterable[str]) -> str | None:
    """동의어 목록에서 CAS 번호 형태(^\\d{2,7}-\\d{2}-\\d$)의 첫 매치를 돌려준다.

    여러 후보가 섞여 있어도(일부 화합물은 CAS 여러 개를 동의어로 나열한다)
    순서상 첫 매치를 채택한다. 순수 함수 — 네트워크 없음.
    """
    for synonym in synonyms:
        if isinstance(synonym, str) and _CAS_PATTERN.match(synonym):
            return synonym
    return None


def resolve(transport, name: str) -> IngredientRecord | None:
    """이름 -> IngredientRecord. 빈 이름은 조회 자체가 무의미하므로 None.

    이름→CID(name/{name}/cids)를 먼저 조회한다. 모르는 이름은 404 인데,
    여기서 흡수하지 않고 NotFound 를 그대로 전파한다 — "이 이름을 PubChem
    이 모른다"와 "네트워크가 실패했다"를 호출자가 구분해야 한다(unpaywall.py
    /crossref.py 와 같은 계약). CID 가 여러 개 오면 첫 값만 쓴다(가장
    관련성 높은 매치로 가정 — 실측상 니아신아마이드 같은 흔한 성분은 CID
    하나만 온다).

    CID 로 동의어(cid/{cid}/synonyms)를 조회한다. IngredientRecord.synonyms
    에는 앞 SYNONYM_LIMIT 개만 담고(모듈 docstring "동의어 절단" 참고), CAS
    는 절단 전 원본 전체에서 extract_cas() 로 뽑는다. inci_name 은 여기서
    항상 None — CosIng 조인(T13)이 채운다. fetched_at 은 동의어 응답에
    transport 가 스탬프한 Payload.captured_at(ISO UTC)을 쓴다.
    """
    if not name or not name.strip():
        return None

    cid_payload = transport.request(
        Fetch(url=BASE + f"compound/name/{quote(name, safe='')}/cids/JSON"),
        PubChem.policy,
    )
    cid_data = cid_payload.json_data()
    cids = ((cid_data or {}).get("IdentifierList") or {}).get("CID") or []
    if not cids:
        return None
    cid = cids[0]

    synonym_payload = transport.request(
        Fetch(url=BASE + f"compound/cid/{cid}/synonyms/JSON"),
        PubChem.policy,
    )
    synonym_data = synonym_payload.json_data()
    information = ((synonym_data or {}).get("InformationList") or {}).get("Information") or []
    all_synonyms = (information[0].get("Synonym") or []) if information else []

    return IngredientRecord(
        name_key=name_key(name),
        inci_name=None,
        cid=cid,
        cas=extract_cas(all_synonyms),
        synonyms=tuple(all_synonyms[:SYNONYM_LIMIT]),
        sources=("pubchem",),
        fetched_at=synonym_payload.captured_at,
    )
