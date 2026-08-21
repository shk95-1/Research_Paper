"""프로바이더 교차 진단(T15) — 지표가 아니라 진단이다.

**대원칙: 프로바이더 간 병합 집계는 하지 않는다.** 이 모듈은 mesh_monthly.csv
나 keyword_monthly.csv 처럼 트렌드 지표를 내지 않는다 — openalex 와 pubmed
raw 를 월별로 나란히 놓고 DOI 로 몇 편이 겹치는지만 센다. 겹침 자체가
지표가 아니라, "두 프로바이더가 같은 모집단을 보고 있는가"를 사람이 눈으로
확인할 자료다(예: 겹침이 아주 낮으면 어느 한쪽의 검색어가 잘못됐다는 신호일
수 있다 — 그 판단은 사람이 한다).

DOI 없는 PubMed 레코드를 pubmed_only 로 슬쩍 끼워 넣지 않는 이유
    PubMed 레코드에 DOI 가 없으면 openalex 와 매칭을 시도할 수 없다 —
    "openalex 에 없어서 pubmed_only" 인지 "DOI 가 없어서 애초에 매칭
    불가능" 인지가 다른 사실이다. 이를 구분 없이 pubmed_only 에 합치면
    "매칭을 시도했지만 못 찾았다"는 뜻으로 잘못 읽힌다 — 매칭 불가를
    숨기지 않기 위해 pubmed_doi_missing 이라는 별도 컬럼을 둔다.

한쪽 raw 가 없으면 파일을 만들지 않는다
    이 진단은 옵션이다(trend collect --provider pubmed 를 아직 돌리지 않은
    query_id 에서 `trend overlap` 을 불러도 실패로 취급하지 않는다) —
    어느 쪽이 없는지 메시지로 알리고 exit 0.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from paper_radar.trend import records
from paper_radar.trend.mesh_aggregate import load_pubmed_records

OUT_DIR = records.REPO_ROOT / "out" / "trend"

OVERLAP_FIELDS = [
    "month_bucket",
    "openalex_papers",
    "pubmed_papers",
    "both_by_doi",
    "openalex_only",
    "pubmed_only",
    "pubmed_doi_missing",
]


class MissingRawError(RuntimeError):
    """한쪽(또는 양쪽) provider 의 raw 가 없어 진단을 만들 수 없다. 호출자(CLI)
    조치: exit 0(진단은 옵션이므로 오류로 취급하지 않는다) + 안내 메시지."""


def overlap_rows(openalex_records, pubmed_records):
    """월별 openalex_papers/pubmed_papers/both_by_doi/openalex_only/pubmed_only/
    pubmed_doi_missing 을 계산한다. 순수 함수 — raw 읽기는 run() 이 한다.
    """
    openalex_papers = defaultdict(int)
    openalex_dois = defaultdict(set)
    for record in openalex_records:
        month = record.get("month_bucket")
        if not month:
            continue
        openalex_papers[month] += 1
        if record.get("doi"):
            openalex_dois[month].add(record["doi"])

    pubmed_papers = defaultdict(int)
    pubmed_dois = defaultdict(set)
    pubmed_doi_missing = defaultdict(int)
    for record in pubmed_records:
        month = record.get("month_bucket")
        if not month:
            continue
        pubmed_papers[month] += 1
        doi = record.get("doi")
        if doi:
            pubmed_dois[month].add(doi)
        else:
            pubmed_doi_missing[month] += 1

    months = sorted(set(openalex_papers) | set(pubmed_papers))
    rows = []
    for month in months:
        oa_dois = openalex_dois.get(month, set())
        pm_dois = pubmed_dois.get(month, set())
        both = oa_dois & pm_dois
        rows.append(
            {
                "month_bucket": month,
                "openalex_papers": openalex_papers.get(month, 0),
                "pubmed_papers": pubmed_papers.get(month, 0),
                "both_by_doi": len(both),
                "openalex_only": len(oa_dois - pm_dois),
                "pubmed_only": len(pm_dois - oa_dois),
                "pubmed_doi_missing": pubmed_doi_missing.get(month, 0),
            }
        )
    return rows


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OVERLAP_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def default_path(query_id, out_dir=OUT_DIR):
    return Path(out_dir) / query_id / "provider_overlap.csv"


def run(query_id, out_dir=None):
    """query_id 의 openalex/pubmed raw 를 대조해 provider_overlap.csv 를 쓴다.

    한쪽(또는 양쪽) raw 가 없으면 MissingRawError 를 던진다(CensusError 와
    같은 이유로 SystemExit 이 아니라 일반 예외 — 호출자가 종료 코드를
    결정한다). CLI 는 이를 잡아 exit 0 으로 옮긴다(진단은 옵션이라는
    모듈 docstring 참고 — 실패가 아니다).
    """
    missing = [
        provider
        for provider in ("openalex", "pubmed")
        if not records.raw_dir(query_id, provider).exists()
    ]
    if missing:
        hints = " / ".join(f"trend collect --provider {provider}" for provider in missing)
        raise MissingRawError(
            f"{query_id}: {', '.join(missing)} raw 가 없습니다. {hints} 를 먼저 실행하세요."
        )

    openalex_records = records.load_records(query_id, "openalex")
    pubmed_records = load_pubmed_records(query_id)
    rows = overlap_rows(openalex_records, pubmed_records)

    target = default_path(query_id, out_dir or OUT_DIR)
    written = write_csv(target, rows)
    return {"target": target, "rows": rows, "written": written}
