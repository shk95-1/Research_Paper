"""6단계(pubmed): MeSH 월별 집계. mesh_monthly.csv 산출. DB 를 쓰지 않는다.

T15 — trend/aggregate.py(openalex, keyword/topic 축)의 대칭짝이지만 별도
모듈이다. **대원칙: 프로바이더 간 병합 집계는 하지 않는다** — 이 모듈은
`data/raw/pubmed/{query_id}/` 만 읽고, openalex 의 raw·CSV 어느 것도 읽거나
쓰지 않는다(모집단이 다르면 분모가 다르다 — 기존 "keywords 와 topics 를
절대 합치지 않는다" 원칙의 프로바이더 차원 적용).

산출물 (out/trend/{query_id}/ 아래)
    mesh_monthly.csv   MeSH 용어 x 월 x 프로바이더. mesh_term, paper_count,
                        total_papers, prevalence, is_low_sample, is_provisional.

인용 가중 컬럼이 없는 이유
    trend/aggregate.py 의 keyword_monthly.csv/topic_monthly.csv 는
    citation_sum/citation_median/weighted_prevalence 를 함께 낸다(OpenAlex
    가 cited_by_count 를 준다). PubMed E-utilities 는 인용수를 제공하지
    않는다 — efetch 응답 어디에도 그 필드가 없다. 없는 데이터를 0 이나
    None 으로 채워 citation 계열 컬럼을 흉내 내지 않는다: 컬럼 자체가
    없으면 "이 소스는 인용수를 모른다"는 사실이 스키마에서 바로 드러나지만,
    0 으로 채우면 "인용이 0 인 논문들"처럼 읽혀 조용히 틀린 결론(예: 이
    MeSH 용어는 전혀 인용되지 않는다)을 부른다.

mesh_term 은 원문 표기를 소문자화만 한다 — keyword_lexicon 정규화를 태우지
않는 이유
    keyword_lexicon.json 은 OpenAlex 의 자유 텍스트 keywords/topics 표면형이
    서로 다른 표기(zno, ZnO, zinc oxide)로 흩어져 있는 문제를 정리하는
    사전이다. MeSH 는 이미 사람이 큐레이션하는 통제 어휘라 그 문제 자체가
    없다(같은 개념은 이미 하나의 DescriptorName 으로 수렴돼 있다). 여기에
    별칭 접기(사전 정규화)를 또 태우면, MeSH 축을 만든 이유(OpenAlex 토픽
    체계의 2025-10 어휘 단절 같은 문제에서 자유롭다는 것) 자체가 훼손된다
    — 사전이 잘못돼 있거나 낡으면 MeSH 용어를 엉뚱한 표준키로 접어버릴 수
    있고, 그러면 MeSH 도 OpenAlex 와 똑같이 "사전 유지보수에 종속된 축"이
    되어 대조군으로서의 가치를 잃는다. 소문자화만 하는 이유는 순수하게
    표기 통일(대소문자 차이로 같은 DescriptorName 이 다른 행으로 갈라지는
    것 방지)이지, 의미 정규화가 아니다.

census 가드
    trend.aggregate.census_guard()/CensusError 를 그대로 재사용한다(중복
    구현 금지) — collect_pubmed.py 가 _meta.json 에 openalex 와 같은 키
    이름(collected/expected_from_api/is_census)으로 쓰기 때문에 provider
    인자만 바꿔서 그대로 통한다.

is_low_sample/is_provisional 판정
    trend.aggregate.is_low_sample()/is_provisional_month()/
    provisional_months() 를 그대로 재사용한다(중복 구현 금지) — 셋 다
    순수 함수라 프로바이더를 모른다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from paper_radar.trend import records
from paper_radar.trend.aggregate import (
    CensusError,
    census_guard,
    is_low_sample,
    is_provisional_month,
    provisional_months,
    round_or_none,
    write_csv,
)
from paper_radar.trend.collect_pubmed import PROVIDER

__all__ = ["CensusError", "run"]

HERE = Path(__file__).resolve().parent
OUT_DIR = records.REPO_ROOT / "out" / "trend"
CONFIG_PATH = HERE / "config.json"

MESH_MONTHLY_FIELDS = [
    "month_bucket",
    "query_id",
    "mesh_term",
    "paper_count",
    "total_papers",
    "prevalence",
    "is_low_sample",
    "is_provisional",
]


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_pubmed_records(query_id):
    """data/raw/pubmed/{query_id}/*.jsonl 전체를 pmid 기준으로 중복 없이 읽는다.

    trend.records.iter_raw() 와 같은 아이디어(정렬된 glob, 중복 제거)이되
    그 함수는 openalex work dict 의 "id" 키를 가정한다 — collect_pubmed.py
    가 쓰는 레코드는 이미 평평한 dict({"pmid","doi","title","journal",
    "mesh_terms","month_bucket","provider"})라 "id" 키가 없다. 경로 계산
    (records.raw_dir, provider 파라미터화된 완전 범용 헬퍼)은 그대로
    재사용하고, 레코드 키 차이만 이 함수가 흡수한다.
    """
    directory = records.raw_dir(query_id, PROVIDER)
    if not directory.exists():
        raise FileNotFoundError(
            f"{directory} 가 없습니다. 먼저 `trend collect --provider pubmed` 를 실행하세요."
        )
    seen = set()
    result = []
    for path in sorted(directory.glob("*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                pmid = record.get("pmid")
                if not pmid or pmid in seen:
                    continue
                seen.add(pmid)
                result.append(record)
    return result


def mesh_monthly(record_list, query_id, config, provisional):
    """(mesh_term, month_bucket) 한 조합에 한 행.

    한 논문 안에서 같은 MeSH 용어가 소문자화 후 중복되면(대소문자 표기
    차이만 있던 경우) 그 논문에서는 한 번만 센다 — 안 그러면 term_monthly()
    (aggregate.py, keywords_norm 용)의 "한 논문 안의 중복" 원칙과 어긋나
    paper_count 가 부풀어 prevalence 가 조용히 틀린다.
    """
    threshold = config.get("low_sample_threshold", 50)
    totals = defaultdict(int)
    for record in record_list:
        month = record.get("month_bucket")
        if month:
            totals[month] += 1

    counts = defaultdict(int)
    for record in record_list:
        month = record.get("month_bucket")
        if not month:
            continue  # 월별 집계에서만 제외. 레코드 자체는 버리지 않는다(트렌드 전 관례와 동일)
        terms_in_this_paper = set()
        for term in record.get("mesh_terms") or ():
            key = (term or "").strip().lower()
            if not key or key in terms_in_this_paper:
                continue
            terms_in_this_paper.add(key)
            counts[(key, month)] += 1

    rows = []
    for (term, month), count in counts.items():
        total = totals.get(month, 0)
        rows.append(
            {
                "month_bucket": month,
                "query_id": query_id,
                "mesh_term": term,
                "paper_count": count,
                "total_papers": total,
                "prevalence": round_or_none(count / total if total else None),
                "is_low_sample": is_low_sample(total, threshold),
                "is_provisional": is_provisional_month(month, provisional),
            }
        )
    rows.sort(key=lambda row: (row["month_bucket"], -row["paper_count"], row["mesh_term"]))
    return rows


def run(query_id, config=None, out_dir=None, allow_sample=False):
    """query_id 하나를 집계해 out_dir/{query_id}/mesh_monthly.csv 를 쓴다.

    out_dir 생략 시 OUT_DIR(out/trend/) 아래 query_id 네임스페이스를 쓴다 —
    aggregate.run() 과 같은 네임스페이스 규칙(provider 는 파일명으로,
    query_id 는 디렉터리로 분리).
    """
    config = config or load_config()
    census_guard(query_id, allow_sample, PROVIDER)
    record_list = load_pubmed_records(query_id)

    collected_at = records.raw_meta(query_id, PROVIDER).get("collected_at")
    provisional = provisional_months(collected_at, config.get("provisional_months", 3))

    rows = mesh_monthly(record_list, query_id, config, provisional)
    all_months = sorted({row["month_bucket"] for row in rows})

    namespace = Path(out_dir) / query_id if out_dir else OUT_DIR / query_id
    written = {
        "mesh_monthly.csv": write_csv(namespace / "mesh_monthly.csv", MESH_MONTHLY_FIELDS, rows)
    }
    return {
        "out_dir": namespace,
        "written": written,
        "records": len(record_list),
        "months": len(all_months),
        "provisional": sorted(m for m in all_months if m in provisional),
        "low_sample": sorted({row["month_bucket"] for row in rows if row["is_low_sample"]}),
        "rows": rows,
    }
