# papers_trend — 논문 키워드 트렌드

화장품 분야에서 어떤 키워드가 떠오르고 어떤 것이 유지되는지 본다.
산출물은 `out/` 아래 CSV 5개다. 시각화는 이 모듈의 범위가 아니다.

`papers/` 와 목적이 다르다. `papers/` 는 특정 주장을 뒷받침할 논문을 찾아
검증한다(근거 확보). 이 모듈은 검색어에 걸리는 논문을 전수로 받아 분포를
본다(트렌드 관찰). 목적이 다르므로 모집단이 다르고, 그래서 별도 모듈이다.

---

## 읽기 전에 반드시 알아야 할 것 3가지

### 1. OpenAlex 키워드 어휘가 2025-10 에 교체됐다 — 가장 중요

`trend_metrics.csv` 의 `growth_ratio` 와 `trend_class` 를 **그대로 믿으면 안 된다.**

측정 2026-08-20, `sunscreen` 전수 7,623건:

| 키워드 | 2025-09 까지 | 2025-10 부터 |
|---|---|---|
| `dermatology` | 25개월 연속 월 21~61편 | **0** |
| `nanotechnology` | 25개월 연속 월 9~39편 | **0** |

어휘 교체 규모: 단절 이전에만 등장하는 키워드 **2,304개**, 이후에만 등장하는
키워드 **1,806개**, 양쪽 모두 **2,345개**. 논문당 키워드 개수는 오히려 늘었다
(5.3개 → 6.6개). 즉 부여를 줄인 것이 아니라 **다른 어휘로 바꿴 것**이다.

`trend_metrics.csv` 는 최근 12개월(2025-09~2026-08)과 그 이전 24개월을
비교한다. **이 경계가 어휘 단절 지점과 거의 겹친다.** 결과적으로

- 구 어휘 키워드는 실제와 무관하게 `declining` 으로 분류된다 (193개 중 다수)
- 신 어휘 키워드는 실제와 무관하게 `emerging` / `is_new_entrant` 가 된다

**사전에 등록된 키는 이 단절에 강하다.** 단절 이전에 등장한 키워드가 이후에도
살아남는 비율:

| | 생존율 |
|---|---|
| 사전 등록 키 (`is_in_lexicon = True`) | **96%** (25/26) |
| 통과된 원시 표현 (`is_in_lexicon = False`) | **50%** (2,320/4,623) |

사전이 여러 표면형을 한 키로 묶으므로, OpenAlex 가 표기를 바꿔도 별칭 중
하나에 걸리면 키가 유지된다. **따라서 시계열 해석은
`is_in_lexicon = True` 인 행으로 한정하는 것이 안전하다.**

### 2. 전수인지 표본인지 확인하라

`raw/{query_id}/_meta.json` 의 `is_census` 를 본다.

- `true` — 전수. 정렬 순서가 결과에 영향을 주지 않는다. `prevalence` 를
  모집단 비율로 읽어도 된다.
- `false` — 표본. OpenAlex 기본 정렬(`relevance_score`)이 어느 논문이
  포함됐는지를 결정한다. relevance 는 인용수를 반영하므로 최근 논문이 빠진다.
  `prevalence` 는 표본 내 비율일 뿐이다.

`aggregate.py` 가 `is_census: false` 면 실행을 거부한다. 알고 쓰려면
`--allow-sample` 을 붙인다.

현재 상태: `sunscreen` 전수 7,623건 완료. `cosmetics` 는 9,483/59,751건에서
중단(OpenAlex 일일 예산 소진).

### 3. `is_provisional` 은 진행 중인 당월만 표시한다

`config.json` 의 `provisional_months` 는 **1** 이다. 3 이 아니다.
"최근 3개월은 색인 미완" 가설을 측정해 보니 반증됐다. 자세한 근거는
`ASSUMPTIONS.md`.

---

## 파일 5개와 관계

```
raw/{query_id}/*.jsonl          OpenAlex 응답 원본 (무손실, gitignore)
   |
   v
out/monthly_denominator.csv     A. 월별 분모. 다른 파일의 비율을 검산하는 기준
out/keyword_monthly.csv         B. 주 산출물. keywords 시계열
out/topic_monthly.csv           B'. 같은 스키마로 topics 만. B 와 절대 합치지 않는다
out/trend_metrics.csv           C. B 를 키워드당 1행으로 요약
out/unmatched_*.csv             D. 사전에 없는 표현. 사람이 검수할 작업 목록
```

인코딩은 전부 `utf-8-sig` (Excel 에서 바로 열린다). 불리언은 `True` / `False` 문자열이다.

---

## A. `monthly_denominator.csv` — 36행

월별 논문 수. 다른 파일의 `total_papers` 와 `prevalence` 가 맞는지 검산하는 기준이다.

| 컬럼 | 뜻 |
|---|---|
| `month_bucket` | 집계의 시간 단위. `YYYY-MM`. `publication_date` 를 월 단위로 절삭한 값 |
| `query_id` | 어느 검색 프로파일인지. `config.json` 의 프로파일 이름 (`sunscreen` 등) |
| `paper_count` | 그 달에 발행된 논문 수. **모든 비율의 분모** |
| `is_low_sample` | `paper_count` 가 `low_sample_threshold`(기본 50) 미만이면 `True`. 그 달의 비율 지표는 산출은 되지만 신뢰하지 않는다 |
| `is_provisional` | 수집일 기준 진행 중인 당월이면 `True`. 달이 끝나지 않아 건수가 적다. `trend_metrics` 의 `prevalence_recent` 계산에서 제외된다 |

---

## B. `keyword_monthly.csv` — 24,584행 (주 산출물)

알갱이: **`(keyword_key, month_bucket, query_id)` 한 조합에 한 행.**

| 컬럼 | 뜻 |
|---|---|
| `month_bucket` | 시간 단위. A 와 같다 |
| `query_id` | 검색 프로파일 |
| `keyword_key` | **집계 식별자.** 사전에 있으면 대문자 스네이크 표준키(`ULTRAVIOLET`), 없으면 정규화된 표면형 소문자(`sun protection`). 조인할 때 이 컬럼을 쓴다 |
| `canonical_en` | 표시용 이름. `ULTRAVIOLET` → `ultraviolet radiation` |
| `category` | 사전에 등록된 키만 채워진다. `uv_filter` / `ingredient` / `mechanism` / `spec` / `enzyme` / `peptide` / `formulation` / `claim` / `anatomy` / `regulatory`. 원시 표현은 빈칸 |
| `is_in_lexicon` | 사전 등록 여부. **큐레이션된 키(32개)와 통과된 원시 표현(6,429개)을 구분한다.** 위 caveat 1 때문에 시계열 해석은 `True` 로 한정하는 것이 안전하다 |
| `paper_count` | 그 달에 그 키워드가 붙은 논문 수. 한 논문이 `ZnO` 와 `zinc oxide` 를 둘 다 가져도 `ZINC_OXIDE` 로 접혀 **1로 센다** |
| `total_papers` | 그 달 전체 논문 수. A 의 `paper_count` 와 같은 값 |
| `prevalence` | **핵심 지표.** `paper_count / total_papers`. 그 달 논문 중 몇 %가 이 키워드를 달았나. 절대 건수를 쓰지 않는 이유는 논문 발행량 자체가 해마다 늘어 절대 건수는 전부 우상향하기 때문이다 |
| `citation_sum` | 그 논문들의 원시 인용수 합. **가공하지 않은 값** |
| `citation_median` | 원시 인용수 중앙값. 합보다 이상치에 덜 흔들린다 |
| `weighted_prevalence` | 코호트 백분위 평균. 아래 설명 참조 |
| `is_low_sample` | A 와 같다 |
| `is_provisional` | A 와 같다 |

### `weighted_prevalence` 를 어떻게 읽나

**0.5 가 기준이다.** 계산은 두 단계이고 순서가 중요하다.

1. **논문 단위** — 같은 `month_bucket` 안의 논문들끼리만 인용수를 줄 세워
   백분위를 낸다 (`citation_percentile_in_cohort`). 같은 달 논문은 같은 시간
   동안 인용을 모았으므로, 이 순위는 순수하게 "동시대 논문 중 얼마나
   주목받았나"만 담는다. 연령 효과가 나눗셈으로 줄어드는 게 아니라 비교
   대상에서 아예 빠진다.
2. **키워드 단위** — 그 달 그 키워드가 붙은 논문들의 백분위 평균.

읽는 법:

- `= 0.5` — 그 키워드가 붙은 논문이 평균적인 주목도
- `> 0.5` — 동시대 논문 중 상위. 등장 빈도와 무관하게 **주목받는 주제**
- `< 0.5` — 동시대 논문 중 하위

**`prevalence` 와 `weighted_prevalence` 가 어긋나는 지점이 해석상 가장
중요하다.** 예: `nanotechnology` 는 점유율은 줄었는데(`declining`)
`weighted_prevalence` 가 0.787 이다 — 논문 수는 줄어도 나오는 것은
동시대 상위 21% 에 든다는 뜻이다.

인용수를 지표에 **그대로 곱하지 않는** 이유: 2023년 논문이 2026년 논문보다
인용이 많은 것은 중요해서가 아니라 시간이 지나서다. 최근 3년을 보는 분석에서
원시 인용수를 곱하면 "떠오르는 키워드 찾기"가 "오래된 키워드 찾기"로 뒤집힌다.

---

## B'. `topic_monthly.csv` — 8,540행

`keyword_monthly.csv` 와 **컬럼이 완전히 같다.** 다른 것은 무엇을 세는지다.

| | `keywords` | `topics` |
|---|---|---|
| 성격 | 세분화된 개념. 논문당 평균 6.6개 | 고정 분류 체계. 논문당 평균 2.8개 |
| 용도 | **주 분석 대상.** 해상도가 높다 | 맥락 파악. 해상도가 낮아 떠오르는 것을 못 잡는다 |
| 예 | `sun protection factor`, `nanoparticle` | `Skin Protection and Aging` |

**두 파일을 합치지 마세요.** 알갱이가 달라서 합치면 한 논문이 양쪽에서
중복 계수된다.

`topics` 의 실용적 쓸모 하나: `sunscreen` 프로파일은 `photoprotection`
검색어 때문에 식물 광생물학 논문이 약 19% 섞여 있다. `topic_monthly.csv` 에
`photosynthetic processes`, `algal biology`, `biocrusts` 같은 주제로 분리돼
있으므로 이걸로 걸러낼 수 있다.

---

## C. `trend_metrics.csv` — 6,455행

알갱이: **키워드당 1행.** B 를 요약한 것이므로 B 로 항상 검산할 수 있다.

| 컬럼 | 뜻 |
|---|---|
| `keyword_key` | B 와 같다. 조인 키 |
| `canonical_en` | 표시용 이름 |
| `category` | 사전 카테고리. 원시 표현은 빈칸 |
| `is_in_lexicon` | 사전 등록 여부. **caveat 1 때문에 이 컬럼으로 먼저 걸러라** |
| `query_id` | 검색 프로파일 |
| `first_seen_month` | 처음 등장한 달 |
| `total_papers` | 3년 누적 등장 논문 수 |
| `months_present` | 36개월 중 등장한 개월 수. **"떠오르는가 / 유지되는가"를 가르는 컬럼** |
| `months_observed` | 관측된 총 개월 수 (36). `months_present` 의 분모 |
| `prevalence_recent` | 최근 12개월 평균 `prevalence`. **등장하지 않은 달은 0으로 센다** (등장한 달만 평균하면 희소한 키워드가 과대평가된다). `is_provisional` 인 달은 제외 |
| `prevalence_prior` | 그 이전 24개월 평균. 같은 방식 |
| `growth_ratio` | `prevalence_recent / prevalence_prior`. 1보다 크면 상승 |
| `is_new_entrant` | 이전 24개월 0건 + 최근 12개월 5건 이상이면 `True` |
| `weighted_prevalence_recent` | 최근 12개월의 `weighted_prevalence` 평균. 여기서는 등장한 달만 평균한다 |
| `trend_class` | 아래 분류 |

### `trend_class` 분류 규칙

`presence = months_present / months_observed` 로 둔다.

| 값 | 조건 | 뜻 |
|---|---|---|
| `unrated` | `total_papers < 10` | **분류하지 않음.** 임계값 없이 `growth_ratio` 만 보면 3편→9편도 300% 가 된다. 6,455개 중 5,639개가 여기다 |
| `emerging` | `growth_ratio >= 1.5` 이고 `presence >= 0.25` | 급상승 |
| `steady` | `growth_ratio` 가 0.67~1.5 이고 `presence >= 0.5` | 유지 |
| `declining` | `growth_ratio <= 0.67` | 하락 |
| `sporadic` | 성장했지만 `presence < 0.25`, 또는 위에 안 걸리는 나머지 | **경계.** 특정 연구실이 한 번에 논문 몇 편 낸 것일 가능성이 크다 |

임계값 상수는 전부 `aggregate.py` 파일 상단 한 곳에 모여 있다.

현재 분포 (`sunscreen`): `unrated` 5,639 / `emerging` 298 / `sporadic` 211 /
`declining` 193 / `steady` 114.

**다시 강조: `declining` 193개 중 상당수는 caveat 1 의 어휘 교체 때문이다.**
`is_in_lexicon = True` 로 걸러서 보라.

---

## D. `unmatched_{profile}_{field}.csv` — 200행

지표가 아니다. **사람이 처리할 작업 목록**이다. 정규화 후에도 사전에 없는
표현을 빈도순으로 뽑은 것이다.

| 컬럼 | 뜻 |
|---|---|
| `rank` | 빈도 순위 |
| `term` | 정규화된 표면형 |
| `paper_count` | 등장 논문 수 |
| `first_seen_month` / `last_seen_month` | 등장 구간. **`last_seen_month` 가 2025-09 이면 caveat 1 의 구 어휘일 가능성이 높다** |
| `months_present` | 등장한 개월 수 |
| `example_title_1~3` | 예시 논문 제목. 어떤 맥락의 말인지 봐야 판단할 수 있다 |
| `verdict` | **비어 있다. 사람이 채운다** |

`verdict` 에 넣을 값 세 가지:

| 값 | 뜻 | 다음 행동 |
|---|---|---|
| `lexicon` | 표준화할 실제 주제어 | `keyword_lexicon.json` 에 표준키와 별칭 추가 |
| `stopword` | 분야명·분류 라벨 | `stopwords.json` 의 `field_labels` 에 추가 |
| `keep` | 판단 보류 | 아무것도 안 함. 정규화 표면형으로 계속 집계된다 |

사전을 고친 뒤에는 **집계만 다시 돌리면 된다.** 재수집이 필요 없다
(원본 JSONL 이 그대로 있다).

이 단계를 자동화하지 않는다. 빈도만 높고 트렌드 신호가 아닌 것
(`population`, `product (mathematics)`, `medline`)이 표준키가 되면 되돌리기 어렵다.

---

## 재생성 방법

```bash
# 1. 수집 (네트워크. OpenAlex 일일 예산을 쓴다)
python -m papers_trend.collect_openalex --profile sunscreen
python -m papers_trend.collect_openalex --profile cosmetics --max-pages 40  # 나눠 받기
python -m papers_trend.collect_openalex --profile all --dry-run             # 건수만

# 2~5. 네트워크를 쓰지 않는다. 사전을 고칠 때마다 반복 실행한다
python -m papers_trend.records   --profile sunscreen      # 추출 요약
python -m papers_trend.normalize --profile sunscreen      # 정규화 전/후 비교
python -m papers_trend.aggregate --profile sunscreen      # CSV 5개 중 4개
python -m papers_trend.unmatched --profile sunscreen      # 사전 확장 후보

# 테스트 (네트워크 없음)
python -m unittest discover -s papers_trend/tests -t .
```

`collect` 는 한 번, 나머지는 반복 실행이 전제다. 사전과 불용어를 계속 키우게
되므로 2~5단계를 수십 번 다시 돌린다. 그래서 단계마다 파일을 읽어 파일을 쓴다.

### OpenAlex 일일 예산

요청 1건당 `$0.001` 이고 하루 한도가 있다. 응답 헤더
`X-RateLimit-Remaining` 으로 잔량을 볼 수 있고, 소진되면 **UTC 자정에
초기화**된다. `per_page` 는 200(OpenAlex 최대)이므로 배치 크기로 아낄 여지는
없다. 큰 프로파일은 `--max-pages` 로 여러 날에 나눠 받는다.

---

## 다른 데이터와 조인하기

`keyword_lexicon.json` 의 `kr_colloquial` 이 유튜브 쪽과 이어지는 유일한
다리다. 지금은 쓰이지 않지만 전 항목에 채워져 있다.

```
papers_trend  keyword_lexicon.json  kr_colloquial: ["시카", "센텔라", "병풀"]
                        |
                        v
youtube       seeds/topics_v0.1.csv  youtube_terms
```

**주의** 팀 문서(`TEAM_DECISIONS_v0.1.md`)의 시간 단위는 분기(`2025Q4`)이고
이 모듈은 월이다. 월 → 분기 절삭은 가능하지만 반대는 불가능하므로 월을
유지했다. 조인 전에 단위를 맞춰야 한다.

"학술 키워드 증가가 소비 트렌드보다 선행한다"는 **아직 검증되지 않았다.**
지금 산출물은 선행 여부에 대해 아무것도 말하지 않는다. 리포트에서 사실처럼
쓰지 말 것.
