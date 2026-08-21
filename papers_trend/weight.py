"""4단계: 인용 가중치. 순수 함수만 있다. 네트워크도 파일 쓰기도 없다.

papers/verify.py 와 같은 이유다. 나중에 배점을 바꾸면 재수집 없이 재계산만 하면 된다.

절대 하지 말 것: 인용수를 지표에 그대로 곱하기
    2023년 논문이 2026년 논문보다 인용이 많은 것은 중요해서가 아니라 시간이
    지나서다. 최근 3년을 보는 분석에서 원시 인용수를 곱하면
    '떠오르는 키워드 찾기'가 '오래된 키워드 찾기'로 뒤집힌다.

코호트 백분위 — 계산 순서를 뒤집으면 코호트가 깨진다
    1단계: 논문 단위. 같은 month_bucket 안의 논문들끼리만 citation_count 를
           줄 세운다 -> citation_percentile_in_cohort (0.0 ~ 1.0)
    2단계: 키워드 단위. 그 달 그 키워드가 붙은 논문들의 백분위 평균
           -> weighted_prevalence

    반드시 논문 단위 백분위가 먼저다. 같은 달 안에서는 모든 논문이 같은 시간
    동안 인용을 모았으므로, 그 안의 순위는 순수하게 '동시대 논문 중 얼마나
    주목받았나'만 담는다. 연령 효과가 나눗셈으로 줄어드는 게 아니라 비교
    대상에서 아예 빠진다.

원시값은 절대 덮어쓰지 않는다. 가중치는 별도 컬럼으로 나란히 둔다.
"""

from datetime import datetime

MIN_AGE_YEARS = 0.5  # 갓 나온 논문의 분모가 0 에 가까워 값이 폭발하는 것을 막는다
DAYS_PER_YEAR = 365.25


def _parse(text):
    if not text:
        return None
    try:
        return datetime.strptime(str(text)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def age_in_years(publication_date, collected_at):
    """발행 후 경과 연수. MIN_AGE_YEARS 로 클리핑한다."""
    published = _parse(publication_date)
    collected = _parse(collected_at)
    if published is None or collected is None:
        return None
    years = (collected - published).days / DAYS_PER_YEAR
    return max(years, MIN_AGE_YEARS)


def citation_per_year(citation_count, publication_date, collected_at):
    """연간 인용수. 연령 보정의 가장 단순한 형태. 참고용으로만 쓴다.

    이 값도 완전하지는 않다. 인용은 발행 직후 선형으로 쌓이지 않는다.
    주 지표는 코호트 백분위이고 이 컬럼은 나란히 두어 비교하는 용도다.
    """
    age = age_in_years(publication_date, collected_at)
    if age is None:
        return None
    return (citation_count or 0) / age


def cohort_percentiles(counts):
    """1단계. 값 목록 -> 같은 순서의 백분위 목록.

    동점은 midrank 로 처리한다: (더 작은 것 수 + 같은 것 수 / 2) / 전체.
    표본이 1건이면 0.5 다. 동점이 많은 (인용 0회가 흔하다) 분포에서
    한쪽으로 쏠리지 않는다.
    """
    values = [c or 0 for c in counts]
    total = len(values)
    if total == 0:
        return []
    below = {}
    equal = {}
    for value in values:
        equal[value] = equal.get(value, 0) + 1
    ordered = sorted(equal)
    running = 0
    for value in ordered:
        below[value] = running
        running += equal[value]
    return [(below[v] + equal[v] / 2) / total for v in values]


def assign_paper_weights(record_list):
    """1단계를 레코드에 붙인다. month_bucket 별로 따로 줄 세운다.

    돌려주는 것은 새 리스트다. 입력을 바꾸지 않는다.
    citation_count 원시값은 그대로 남는다.
    """
    by_month = {}
    for index, record in enumerate(record_list):
        by_month.setdefault(record.get("month_bucket"), []).append(index)

    percentile_by_index = {}
    for month, indexes in by_month.items():
        if month is None:
            # month_bucket 이 없는 레코드는 코호트를 정의할 수 없다.
            # 백분위를 주지 않는다. 추정하면 조용히 틀린 값이 된다.
            for index in indexes:
                percentile_by_index[index] = None
            continue
        counts = [record_list[i].get("citation_count") or 0 for i in indexes]
        # strict=False: counts 는 indexes 에서 직접 만들어 길이가 항상 같지만,
        # 기존 zip 동작(길이 불일치 시 조용히 자름)을 그대로 유지한다.
        for index, percentile in zip(indexes, cohort_percentiles(counts), strict=False):
            percentile_by_index[index] = percentile

    result = []
    for index, record in enumerate(record_list):
        enriched = dict(record)
        enriched["citation_percentile_in_cohort"] = percentile_by_index[index]
        enriched["citation_per_year"] = citation_per_year(
            record.get("citation_count"),
            record.get("publication_date"),
            record.get("collected_at"),
        )
        enriched["cohort_size"] = len(by_month.get(record.get("month_bucket")) or [])
        result.append(enriched)
    return result


def weighted_prevalence(percentiles):
    """2단계. 그 달 그 키워드가 붙은 논문들의 백분위 평균.

    백분위가 없는 논문(month_bucket 없음)은 평균에서 제외한다.
    전부 없으면 None.
    """
    usable = [p for p in percentiles if p is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)
