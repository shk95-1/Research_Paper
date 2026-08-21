# 논문 키워드 트렌드 — 컬럼 사전

> 복원 문서(T16). 원문: `git show a36ebda:papers_trend/README.md`(구 `papers_trend/`
> 패키지 시대 — T7 에서 패키지 삭제에 딸려 나갔다). **측정값·caveat 문장은
> 원문 그대로 보존했고, 바뀐 것은 경로·명령·모듈 경로뿐이다.** 끝에 신규
> CSV 2종(`mesh_monthly.csv`, `provider_overlap.csv`, T15) 섹션을 추가했다.

화장품 분야에서 어떤 키워드가 떠오르고 어떤 것이 유지되는지 본다.
산출물은 `out/trend/{query_id}/` 아래 CSV 5개다(+ PubMed 축을 수집하면
2개 더 — 맨 아래 E/F 절 참고). 시각화는 이 파이프라인의 범위가 아니다.

`evidence` 파이프라인(옛 `papers/`)과 목적이 다르다. `evidence` 는 특정
주장을 뒷받침할 논문을 찾아 검증한다(근거 확보). 이 `trend` 파이프라인
(옛 `papers_trend/`)은 검색어에 걸리는 논문을 전수로 받아 분포를 본다
(트렌드 관찰). 목적이 다르므로 모집단이 다르다 — 지금은 둘 다 같은
`paper_radar` 패키지 안의 다른 CLI 그룹(`evidence`/`trend`)일 뿐이다.

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
(5.3개 → 6.6개). 즉 부여를 줄인 것이 아니라 **다른 어휘로 바꾼 것**이다.

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

`data/raw/openalex/{query_id}/_meta.json` 의 `is_census` 를 본다.

- `true` — 전수. 정렬 순서가 결과에 영향을 주지 않는다. `prevalence` 를
  모집단 비율로 읽어도 된다.
- `false` — 표본. OpenAlex 기본 정렬(`relevance_score`)이 어느 논문이
  포함됐는지를 결정한다. relevance 는 인용수를 반영하므로 최근 논문이 빠진다.
  `prevalence` 는 표본 내 비율일 뿐이다.

`trend aggregate` 가 `is_census: false` 면 실행을 거부한다. 알고 쓰려면
`--allow-sample` 을 붙인다.

현재 상태: `sunscreen` 전수 7,623건 완료. `cosmetics` 는 9,483/59,751건에서
중단(OpenAlex 일일 예산 소진).

### 3. `is_provisional` 은 진행 중인 당월만 표시한다

`config.json` 의 `provisional_months` 는 **1** 이다. 3 이 아니다.
"최근 3개월은 색인 미완" 가설을 측정해 보니 반증됐다. 자세한 근거는
`docs/trend-assumptions.md`.

---

## 파일 5개와 관계

```
data/raw/openalex/{query_id}/*.jsonl   OpenAlex 응답 원본 (무손실, gitignore)
   |
   v
out/trend/{query_id}/monthly_denominator.csv     A. 월별 분모. 다른 파일의 비율을 검산하는 기준
out/trend/{query_id}/keyword_monthly.csv         B. 주 산출물. keywords 시계열
out/trend/{query_id}/topic_monthly.csv           B'. 같은 스키마로 topics 만. B 와 절대 합치지 않는다
out/trend/{query_id}/trend_metrics.csv           C. B 를 키워드당 1행으로 요약
out/trend/{query_id}/unmatched_*.csv             D. 사전에 없는 표현. 사람이 검수할 작업 목록
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

임계값 상수는 전부 `src/paper_radar/trend/aggregate.py` 파일 상단 한 곳에 모여 있다.

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
(원본 JSONL 이 그대로 있다). `trend suggest`(T14)를 쓰면 PubChem/CosIng
동의어와 정확히 일치하는 `unmatched` 표현에 `lexicon` 후보를 사람이 검토하기
쉽게 표시해 준다 — 자동으로 사전에 등록하지는 않는다.

이 단계를 자동화하지 않는다. 빈도만 높고 트렌드 신호가 아닌 것
(`population`, `product (mathematics)`, `medline`)이 표준키가 되면 되돌리기 어렵다.

---

## 재생성 방법

```bash
# 1. 수집 (네트워크. OpenAlex 일일 예산을 쓴다)
uv run paper-radar trend collect --profile sunscreen
uv run paper-radar trend collect --profile cosmetics --max-pages 40  # 나눠 받기
uv run paper-radar trend collect --profile all --dry-run             # 건수만

# 2~5. 네트워크를 쓰지 않는다. 사전을 고칠 때마다 반복 실행한다
uv run paper-radar trend records   --profile sunscreen      # 추출 요약
uv run paper-radar trend normalize --profile sunscreen      # 정규화 전/후 비교
uv run paper-radar trend aggregate --profile sunscreen      # CSV 5개 중 4개
uv run paper-radar trend unmatched --profile sunscreen      # 사전 확장 후보

# 테스트 (네트워크 없음)
uv run pytest tests/paper_radar
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
paper_radar trend  keyword_lexicon.json  kr_colloquial: ["시카", "센텔라", "병풀"]
                        |
                        v
youtube       seeds/topics_v0.1.csv  youtube_terms
```

**주의** 팀 문서(`TEAM_DECISIONS_v0.1.md`)의 시간 단위는 분기(`2025Q4`)이고
이 파이프라인은 월이다. 월 → 분기 절삭은 가능하지만 반대는 불가능하므로 월을
유지했다. 조인 전에 단위를 맞춰야 한다.

"학술 키워드 증가가 소비 트렌드보다 선행한다"는 **아직 검증되지 않았다.**
지금 산출물은 선행 여부에 대해 아무것도 말하지 않는다. 리포트에서 사실처럼
쓰지 말 것.

---

## E. `mesh_monthly.csv` (PubMed 축, T15 신설)

`out/trend/{query_id}/mesh_monthly.csv`. PubMed MeSH(통제 어휘) 용어의
월별 유병률(prevalence). 인코딩 utf-8-sig, 불리언은 문자열 `"True"`/`"False"`
(기존 CSV 규약과 동일). `--provider pubmed` 로 수집·집계해야 생긴다
(`uv run paper-radar trend collect --profile sunscreen --provider pubmed` →
`uv run paper-radar trend aggregate --profile sunscreen --provider pubmed`).

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `month_bucket` | `YYYY-MM` | 발행월. |
| `query_id` | 문자열 | 프로파일 이름(예: `sunscreen`). |
| `mesh_term` | 문자열 | PubMed MeSH DescriptorName 원문 표기를 **소문자화만** 한 것(사전 정규화 미적용). |
| `paper_count` | 정수 | 그 달, 그 용어를 가진 논문 수(한 논문 안에서 대소문자만 다른 중복은 한 번만 센다). |
| `total_papers` | 정수 | 그 달의 분모(pubmed raw 전체 건수 — MeSH 유무와 무관). 같은 달의 모든 행에서 동일한 값. |
| `prevalence` | 실수\|빈칸 | `paper_count / total_papers`(반올림 6자리). `total_papers=0` 이면 빈칸. |
| `is_low_sample` | `True`/`False` | `total_papers < low_sample_threshold`(config, 기본 50)면 `True` — 그 달의 비율 지표를 신뢰하지 않는다. |
| `is_provisional` | `True`/`False` | 수집일 기준 최근 N개월(config `provisional_months`)이면 `True` — 색인이 아직 안 찼을 수 있다. |

**해석 caveat**

- **citation 계열 컬럼이 없다** — PubMed E-utilities 는 인용수를 제공하지
  않는다. `keyword_monthly.csv`(openalex)의 `citation_sum`/`citation_median`/
  `weighted_prevalence` 에 대응하는 컬럼이 이 파일에는 존재하지 않는다.
  0 이나 빈 값으로 채워 흉내 내지 않았다 — 없는 데이터를 0 으로 채우면
  "인용이 0 인 논문들"로 잘못 읽힌다.
- **`mesh_term` 은 keyword_lexicon 정규화를 거치지 않는다** — MeSH 는 이미
  사람이 큐레이션하는 통제 어휘라 별칭 접기(zno ↔ zinc oxide 같은) 문제
  자체가 없다. 여기에 사전 정규화를 또 태우면, 이 축을 만든 이유(OpenAlex
  토픽 체계처럼 2025-10 어휘 교체 같은 단절이 없다는 것) 자체가 훼손된다.
- **`keyword_monthly.csv`/`topic_monthly.csv`(openalex)와 절대 합치지
  않는다** — 모집단이 다르다(OpenAlex 쿼리 vs `pubmed_query`, 서로 다른
  색인 시스템). 같은 `query_id` 라도 `total_papers` 값이 두 프로바이더
  사이에 다를 수 있다 — 이는 버그가 아니라 서로 다른 모집단을 반영한다.
- **전수 여부는 `data/raw/pubmed/{query_id}/_meta.json` 의 `is_census` 가
  결정한다** — `false` 인데 집계했다면(`--allow-sample`) prevalence 는
  표본 내 비율일 뿐 모집단 비율이 아니다.
- **`mesh_term` 이 없는 논문도 `total_papers`(분모)에는 포함된다** — MeSH
  색인이 아직 안 된 최신 논문일 수 있어, 분자(용어별 `paper_count`)에서만
  빠지고 분모에는 남는다.

## F. `provider_overlap.csv` (진단, T15 신설)

`out/trend/{query_id}/provider_overlap.csv`. openalex/pubmed 두 raw 를
DOI 로 대조하는 **진단**(트렌드 지표가 아니다). 인코딩 utf-8-sig.
`uv run paper-radar trend overlap --profile sunscreen` (두 프로바이더 raw 가
모두 있어야 한다 — 정의상 `--provider` 를 받지 않는다).

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `month_bucket` | `YYYY-MM` | 두 프로바이더 중 한쪽에라도 그 달 레코드가 있으면 등장. |
| `openalex_papers` | 정수 | 그 달 openalex raw 논문 수(DOI 유무 무관). |
| `pubmed_papers` | 정수 | 그 달 pubmed raw 논문 수(DOI 유무 무관). |
| `both_by_doi` | 정수 | 두 프로바이더 모두에서 같은(정규화된) DOI 로 발견된 논문 수. |
| `openalex_only` | 정수 | openalex 에는 DOI 가 있고 pubmed 쪽 DOI 집합에는 없는 논문 수. |
| `pubmed_only` | 정수 | pubmed 에는 DOI 가 있고 openalex 쪽 DOI 집합에는 없는 논문 수. |
| `pubmed_doi_missing` | 정수 | pubmed 레코드인데 DOI 자체가 없어 애초에 매칭을 시도할 수 없었던 건수. |

**해석 caveat**

- **지표가 아니라 진단이다** — 이 CSV 의 어떤 컬럼도 트렌드(성장/쇠퇴)
  판정에 쓰지 않는다. "두 프로바이더가 같은 모집단을 보고 있는가"를
  사람이 눈으로 확인하는 자료다. 예: 겹침(`both_by_doi`)이 아주 낮으면
  `pubmed_query`/openalex `query` 중 한쪽이 잘못 좁거나 넓다는 신호일 수
  있지만, 그 판단과 조정은 사람이 한다.
- **`pubmed_only` 와 `pubmed_doi_missing` 을 합치지 않는다** — 전자는
  "매칭을 시도했지만 openalex 에 없었다", 후자는 "DOI 가 없어 애초에
  매칭을 시도할 수 없었다"로 서로 다른 사실이다. 합치면 매칭 불가 건수가
  숨어 "openalex 가 놓친 논문"으로 잘못 읽힐 수 있다.
- **DOI 정규화 기준은 OpenAlex 쪽(`bare_doi()`)을 따른다** — 소문자화,
  `https://doi.org/`/`doi:` 접두어 제거. pubmed 레코드의 DOI 는
  PubMed efetch 응답의 원문 표기 그대로 저장되므로(별도 정규화 없음),
  대소문자나 접두어 표기 차이로 인한 미스매치 가능성이 이론상 있다(현재
  실측으로 검증되지 않음 — 실제 수집 후 both_by_doi 가 기대보다 낮으면
  가장 먼저 의심할 지점).
- **한쪽(또는 양쪽) provider 를 아직 수집하지 않았으면 파일 자체가
  없다** — `trend overlap` 을 그 상태로 실행하면 CSV 를 만들지 않고
  안내 메시지 + exit 0 을 낸다(오류 아님, `MissingRawError`).
- **`month_bucket` 의 도출 방식이 프로바이더마다 다르다**(원장 T15 minor,
  T15 요약에는 없던 추가 caveat) — openalex 는 논문의 `publication_date`
  파싱값을 쓰고, pubmed 는 그 레코드를 가져온 esearch 질의 창(월별
  `mindate`/`maxdate`) 스탬프를 쓴다. 보통 두 값은 일치하지만 개념적으로
  다른 값이다(하나는 "논문에 적힌 날짜", 다른 하나는 "이 달로 검색했을 때
  걸린 날짜"). 그래서 이 CSV 의 월 단위 비교는 **근사**이지, 두 프로바이더가
  같은 정의의 월로 집계됐다는 보장이 아니다.
