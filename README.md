# cosmetics_research_paper

화장품 트렌드 분석 작업 공간. 논문 쪽 모듈 두 개가 들어 있습니다.

| 모듈 | 목적 | 산출물 |
|---|---|---|
| [`papers/`](#papers--논문-근거-수집기) | **근거 확보.** 특정 주장을 뒷받침할 논문을 찾아 검증 | SQLite + JSON |
| [`papers_trend/`](#papers_trend--논문-키워드-트렌드) | **트렌드 관찰.** 어떤 키워드가 떠오르고 유지되는지 | CSV 5개 |

목적이 다르면 모집단이 다릅니다. `papers/` 는 표적 검색 결과라 모집단이 아니고,
`papers_trend/` 는 검색어에 걸리는 논문을 전수로 받습니다. 그래서 별도 모듈입니다.

## 설치 및 테스트

[uv](https://docs.astral.sh/uv/) 로 관리합니다. 두 모듈 모두 `src/paper_radar/`
아래(`paper-radar` 패키지)로 이식되어 있고, `paper-radar` 콘솔 스크립트(또는
`python -m paper_radar`) 하위 명령으로 씁니다 — 아래 각 절의 명령은 이
기준입니다. (구 `papers`/`papers_trend` 패키지와 `python -m papers ...` 실행법은
제거됐습니다. 상세 재작성은 T16 예정입니다.)

```bash
uv sync --extra dev   # .venv 구성 (requests, python-dotenv + pytest, ruff)
uv run pytest         # tests/paper_radar 전부 (네트워크 안 씀)
```

## papers — 논문 근거 수집기

화장품 트렌드에서 도출한 주장을 뒷받침할 논문을 수집하고 검증합니다.
PDF 원문은 내려받지 않고 메타데이터, 초록, 요약까지만 확보합니다.

무료 API 4개를 조합합니다. 인증 키는 필요하지 않습니다.

| API | 역할 |
|-----|------|
| [OpenAlex](https://api.openalex.org) | 메인 검색, 연도별 집계 |
| [Semantic Scholar](https://api.semanticscholar.org) | tldr 한 줄 요약 |
| [Europe PMC](https://www.ebi.ac.uk/europepmc/) | 생명과학 초록 보강 |
| [Crossref](https://api.crossref.org) | DOI 검증 |

### 사용법

```bash
# 수집 (기본 기간은 최근 10년)
uv run paper-radar evidence collect --query "cosmetic" --from 2016 --to 2026 --limit 100

# 연도별 논문 수
uv run paper-radar evidence trend --query "cosmetic retinol"

# 저장된 논문에서 근거 뽑기
uv run paper-radar evidence cite --keyword "skin barrier" --min-confidence 70
```

결과는 `papers/out/papers.db`(SQLite)와 `papers/out/papers.json`에 쌓입니다.
같은 논문을 다시 수집하면 DOI 기준으로 갱신됩니다.

### 신뢰도 점수

논문 1건마다 수집 시점에 계산해서 함께 저장합니다.

| 항목 | 점수 |
|------|------|
| OpenAlex에 존재 | 5 |
| Crossref에서 DOI 검증됨 | 30 |
| Crossref 제목이 일치 (유사도 0.85 이상) | 25 |
| 추가 소스에서 발견 (개당 15, 최대 30) | 30 |
| 초록 확보 | 10 |
| **철회된 논문** | **총점 0** |

DOI가 없는 논문은 버리지 않고 `has_doi: false`로 남깁니다.

### 검색 범위에 대한 주의

`--query`는 영문만 받습니다. OpenAlex는 제목과 초록에서 검색하며
(`title_and_abstract.search`), 정렬은 OpenAlex 기본값인 `relevance_score`입니다.
relevance는 인용수를 크게 반영하므로 **최근 논문이 구조적으로 밀려납니다.**
최근 동향을 보려면 연도 범위를 좁히세요 (`--from 2024`).

### 환경 변수

`.env`에 넣습니다. 어느 것도 필수는 아닙니다.

| 변수 | 용도 |
|------|------|
| `OPENALEX_API_KEY` | 사실상 필수. 없으면 무인증 예산(하루 ~100 search)에 묶입니다 |
| `OPENALEX_EMAIL` | Crossref polite 풀 연락처 (OpenAlex 폐지, 2026-02) |
| `SEMANTIC_SCHOLAR_API_KEY` | 강력 권장. 익명 풀이 포화 상태입니다 |

이메일은 User-Agent로 전송됩니다. OpenAlex는 2026-02-13부터 mailto/polite pool을
폐지해 더 이상 이 값을 받지 않고, Crossref의 polite 풀(단건 DOI 조회)에만 유효합니다.

### 테스트

```bash
# 단위 테스트. 네트워크를 쓰지 않습니다
uv run pytest tests/paper_radar

# 실제 API 응답 형태가 바뀌었는지 확인 (네트워크를 씁니다)
uv run python tool/live_smoke.py
```

의존성은 `requests`와 `python-dotenv`뿐입니다. 나머지는 표준 라이브러리입니다.

설계 문서: [docs/superpowers/specs/2026-08-19-papers-evidence-collector-design.md](docs/superpowers/specs/2026-08-19-papers-evidence-collector-design.md)

---

## papers_trend — 논문 키워드 트렌드

화장품 분야에서 어떤 키워드가 떠오르고 어떤 것이 유지되는지 봅니다.
OpenAlex 단독으로 전수 수집하고, 월 단위로 집계해 CSV 5개를 냅니다.
시각화는 이 모듈의 범위가 아닙니다.

현재 데이터: **`sunscreen` 프로파일 전수 7,623건** (2023-09 ~ 2026-08, 36개월).

```bash
uv run paper-radar trend collect   --profile sunscreen   # 네트워크
uv run paper-radar trend aggregate --profile sunscreen   # CSV 생성
uv run paper-radar trend unmatched --profile sunscreen   # 사전 확장 후보
```

산출물은 [`out/trend/sunscreen/`](out/trend/sunscreen/) 에 있습니다.

| 파일 | 알갱이 | 행 |
|---|---|---|
| `monthly_denominator.csv` | 월 | 36 |
| `keyword_monthly.csv` | 키워드 x 월 | 24,584 |
| `topic_monthly.csv` | 주제 x 월 | 8,540 |
| `trend_metrics.csv` | 키워드 | 6,455 |
| `unmatched_sunscreen_keywords.csv` | 미매칭 표현 | 200 |

### 읽기 전에

컬럼별 설명·caveat(OpenAlex 키워드 어휘가 2025-10 에 교체되어 `trend_class`
의 `declining` 상당수가 인공물이라는 것 포함)과 가설·반증조건 문서는 구
`papers_trend/README.md`, `papers_trend/ASSUMPTIONS.md` 에 있었습니다. T7 이
구 `papers_trend/` 패키지를 제거하면서 이 문서들도 git 이력으로만 남았습니다
(예: `git show 24444fe:papers_trend/README.md`) — 새 위치로의 재작성은 T16
예정입니다. 그때까지는 시계열을 `is_in_lexicon = True` 로 걸러서 보세요.

### 원본 데이터는 저장소에 없습니다

OpenAlex 응답 원본(JSONL 303MB)은 올리지 않았습니다. 따라서 **CSV 를 이
저장소만으로 재생성할 수 없습니다.** 다시 만들려면 `trend collect` 부터
실행해야 하고, OpenAlex 일일 예산을 씁니다(2026-02-13부터 계량제: 목록/페이지
호출 10크레딧, search 호출 $0.001, 무인증 1,000크레딧/일, 무료 키 등록 시
100,000크레딧/일. UTC 자정 초기화).
