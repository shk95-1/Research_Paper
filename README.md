# paper-radar

화장품 연구 논문을 수집·검증·집계하는 로컬 CLI 도구입니다. 화장품 트렌드
분석에서 나온 주장을 뒷받침할 논문 근거를 확보하고(evidence), 어떤 키워드가
떠오르고 유지되는지 논문 전수를 받아 월 단위로 관찰하며(trend), 그 근거를
임상시험(trials)·성분 실체(ingredient) 데이터로 보강합니다. 목적이 다르면
모집단도 다릅니다 — `evidence` 는 표적 검색 결과이고, `trend` 는 검색어에
걸리는 논문을 전수로 받습니다.

## 아키텍처

단일 패키지 `src/paper_radar/` 위에 8개 계층이 있습니다. 위에서 아래로
갈수록 "무엇을 하는가"에서 "어떻게 요청을 보내는가"로 내려갑니다.

| 계층 | 역할 |
|---|---|
| `contract.py`/`models.py`/`registry.py` | 소스가 지켜야 할 계약(`SourcePolicy`, `Fetch`)과 레코드 타입, 소스 등록 레지스트리 — 최상위 조립 계층 |
| `evidence/` | 논문 근거 수집·검증 파이프라인(옛 `papers/`) — `evidence/pipeline.py`(수집 오케스트레이션), `evidence/verify.py`(신뢰도 점수) |
| `trend/` | 논문 키워드 트렌드 파이프라인(옛 `papers_trend/`) — 전수 수집, 정규화, CSV 집계, 사전 확장 후보 제안 |
| `trials/` | ClinicalTrials.gov 임상시험 레코드 수집·검색 |
| `ingredients/` | PubChem/CosIng 성분 실체 해소(이름↔CAS↔CID↔INCI) |
| `sources/` | 실제 API 8개(+CosIng 파서)의 요청·파싱 — 각 소스는 파싱 순수 함수와 네트워크 드라이버를 분리 |
| `storage/` | SQLite 저장(`repository.py`), 스키마 마이그레이션(`schema.py` + `migrations/`), 실행 기록(`runlog.py`) |
| `transport/` | 공통 HTTP 계층 — 인터벌 페이싱, 예산 추적, 인증 자동 주입, 타입 있는 예외(`errors.py`) |

계층 방향(예: `sources/` 가 `trend/` 를 import 하면 안 됨)은 관례가 아니라
`tests/paper_radar/test_guards.py` 의 AST 기반 가드 테스트로 강제됩니다.

## CLI 명령

콘솔 스크립트 `paper-radar`(또는 `python -m paper_radar`) 아래 4개 그룹이
있습니다. "네트워크" 열은 해당 명령이 외부 API 를 호출하는지 여부입니다.

| 그룹 | 명령 | 네트워크 | 설명 |
|---|---|:---:|---|
| `evidence` | `collect` | O | 논문 수집(OpenAlex 주 소스 + Crossref DOI 검증 + Semantic Scholar/Europe PMC/PubMed 보강 3소스 + Unpaywall OA 링크), 신뢰도 점수와 함께 저장 |
| `evidence` | `trend` | O | 연도별 논문 수 히스토그램(OpenAlex) |
| `evidence` | `cite` | - | 저장된 논문에서 근거 인용용 검색 |
| `trend` | `collect` | O | OpenAlex/PubMed 전수 수집(`--provider`, 기본 openalex) |
| `trend` | `records` | - | 원본 JSONL 에서 집계용 필드 추출 요약 |
| `trend` | `normalize` | - | 사전(`keyword_lexicon.json`)을 적용해 표준키로 접기 |
| `trend` | `aggregate` | - | JSONL → CSV 집계(`--provider`, 기본 openalex) |
| `trend` | `unmatched` | - | 사전 미매칭 표현을 빈도순으로 뽑는다 |
| `trend` | `suggest` | - | unmatched 표현에 PubChem/CosIng 동의어 후보 제안(자동 등록 안 함) |
| `trend` | `overlap` | - | openalex/pubmed raw 를 DOI 로 대조하는 교차 진단(지표 아님, `--provider` 없음) |
| `trials` | `collect` | O | ClinicalTrials.gov v2 임상시험 레코드 수집 |
| `trials` | `list` | - | 저장된 임상시험 키워드 검색 |
| `ingredient` | `resolve` | O | PubChem 이름 → CID/CAS/동의어 조회·저장 |
| `ingredient` | `show` | - | 저장된 성분 실체 조회 |
| `ingredient` | `import-cosing` | - | `tool/fetch_cosing.py` 가 받아 둔 CosIng CSV 임포트 |

"네트워크 -" 인 명령은 전부 로컬 SQLite/파일만 읽고 씁니다.

## 소스

| 소스 | 역할 | 인증 | 요율 |
|---|---|---|---|
| [OpenAlex](https://api.openalex.org) | evidence 메인 검색 + trend 전수 수집(연도별 집계 포함) | `OPENALEX_API_KEY`(선택, 사실상 필수) | 무인증 하루 1,000크레딧(≈search 100건), 키 등록 시 100,000크레딧/일. 요청당 계량제 |
| [Crossref](https://api.crossref.org) | DOI 검증(30점) + 철회 공지 교차검증 | `OPENALEX_EMAIL`(polite 풀 mailto) | polite 풀 10req/s 상한의 절반(0.2초 간격) |
| [Semantic Scholar](https://api.semanticscholar.org) | tldr 한 줄 요약 보강 | `SEMANTIC_SCHOLAR_API_KEY`(권장) | 키 없으면 4초 간격(익명 풀 429 방지), 키 있으면 1초 간격(1req/s) |
| [Europe PMC](https://www.ebi.ac.uk/europepmc/) | 생명과학 초록 보강, DOI 없이도 조회 가능한 유일한 보강 소스 | 없음 | 0.2초 간격 |
| [Unpaywall](https://api.unpaywall.org) | DOI → 합법 OA 위치(PDF 직링크만, 파일은 안 받음) | `OPENALEX_EMAIL`(email 파라미터 필수) | 하루 10만 건, 예산 헤더 없음 |
| [PubMed E-utilities](https://eutils.ncbi.nlm.nih.gov) | evidence MeSH 보강 + trend 의 제2 프로바이더(월별 축) | `NCBI_API_KEY`(선택) | 무키 3req/s, 키 있으면 10req/s |
| [ClinicalTrials.gov v2](https://clinicaltrials.gov/api/v2/) | 임상시험 레코드(`trials`) | 없음 | 문서화된 상한 없음, 0.5초 간격(예의) |
| [PubChem PUG-REST](https://pubchem.ncbi.nlm.nih.gov/rest/pug/) | 성분 실체 해소(`ingredient`) — 이름↔CID↔CAS↔동의어 | 없음 | 무키 5req/s / 400req/min |
| CosIng(EU 화장품 성분 DB) | INCI 참조 테이블(`ingredient import-cosing`) | 없음(일회성 CSV 다운로드) | API 아님 — `tool/fetch_cosing.py` 로 사람이 미리 받아 둔 파일을 읽는다 |

## 환경 변수

`.env` 에 넣습니다(`.env.example` 참고). 어느 것도 필수는 아니지만 첫 번째는
사실상 필수입니다.

| 변수 | 용도 |
|---|---|
| `OPENALEX_API_KEY` | 사실상 필수. 없으면 무인증 예산(하루 1,000크레딧, search 기준 ~100건)에 묶입니다 |
| `OPENALEX_EMAIL` | **Crossref polite 풀 + Unpaywall 의 필수 email 파라미터용.** OpenAlex 자체는 2026-02-13부터 mailto/polite pool 을 폐지해 이 값을 받지 않습니다 |
| `SEMANTIC_SCHOLAR_API_KEY` | 강력 권장. 익명 풀이 포화 상태라 429 가 잦습니다 |
| `NCBI_API_KEY` | 선택. 있으면 PubMed E-utilities 요율이 3req/s → 10req/s 로 늘어납니다 |

## 신뢰도 점수

`evidence collect` 가 논문 1건마다 수집 시점에 계산해 함께 저장합니다.
배분은 고정입니다.

| 항목 | 점수 |
|---|---|
| OpenAlex 에 존재 | 5 |
| Crossref 에서 DOI 검증됨 | 30 |
| Crossref 제목이 일치(유사도 0.85 이상) | 25 |
| 추가 소스에서 발견(개당 15, 최대 30) | 30 |
| 초록 확보 | 10 |
| **철회된 논문** | **총점 0** |

DOI 가 없는 논문은 버리지 않고 `has_doi: false` 로 남깁니다. 철회 판정은
OpenAlex 의 `is_retracted` **또는** Crossref 의 relation(`retractions`) 교차
검증 어느 한쪽이라도 걸리면 철회로 봅니다(한쪽 색인 지연을 다른 쪽이 잡아냄).
**단, 철회 공지 문서 자체(notice-only — "이 논문이 철회됐다"고 알리는 문서)
는 철회'된' 논문이 아니므로 0점 처리 대상이 아닙니다** — 공지와 철회 대상을
role 로 구분해서 판정합니다.

## exit code

- `0` — 완전 성공
- `1` — 부분 성공(예산 소진으로 중단, 일부 소스 오류 등 — `stopped_reason`
  이 있거나 오류 카운트가 있으면 partial)
- `2` — 예약(argparse 자체의 사용법 오류가 이 코드를 씀 — 이 저장소 코드가
  직접 return 하지는 않습니다)

레거시(구 `papers`/`papers_trend`)와 의도적으로 다른 동작 2가지(원장 T6
Ruling):

1. `trend collect --profile <알 수 없는 프로파일>` 은 이제 warn + exit **1**
   입니다(레거시는 `parser.error`/exit 2였습니다).
2. 수집이 중간에 멈추면(`stopped_early`) exit **1** 입니다(레거시는 0이었습니다).

둘 다 새 exit code 규칙(0 완전/1 부분)에 맞춘 것입니다. 레거시 CLI 를 셸
스크립트로 감싸 종료 코드를 분기하던 경우, 이 저장소로 옮길 때 그 분기를
다시 확인하세요.

## 산출물 위치

- `papers/out/papers.db`(SQLite) + `papers/out/papers.json` — `evidence`
  파이프라인 결과. 경로는 레거시와 동일하게 유지했습니다.
- `out/trend/{query_id}/` — `trend` 파이프라인 CSV. 프로파일당 최대 7종:
  `monthly_denominator.csv`, `keyword_monthly.csv`, `topic_monthly.csv`,
  `trend_metrics.csv`, `unmatched_*.csv`(이상 openalex 축, 5종),
  `mesh_monthly.csv`, `provider_overlap.csv`(이상 pubmed 축이 있어야 생기는
  2종, T15). 컬럼 사전과 해석 caveat 은 [`docs/trend-data.md`](docs/trend-data.md)
  에 있습니다.
- `data/` — raw 원본(`data/raw/{provider}/{query_id}/*.jsonl` 등)과 참조
  다운로드(`data/reference/cosing/`). `.gitignore` 대상입니다 — 저장소만으로
  CSV 를 재생성할 수 없고, `trend collect`/`ingredient import-cosing` 부터
  다시 실행해야 합니다.

DB 스키마 마이그레이션은 `repository.connect()` 가 열 때마다 `PRAGMA
user_version` 을 보고 자동 적용합니다(현재 head: m0008, 8개 마이그레이션) —
따로 실행할 명령이 없습니다.

### 재생성 절차

`data/` 가 gitignore 대상이므로, 새 환경에서는 다음 순서로 처음부터 다시
채웁니다(각 명령의 상세 옵션은 위 CLI 명령 표, trend CSV 는
[`docs/trend-data.md`](docs/trend-data.md) 의 "재생성 방법" 참고).

```bash
# evidence: DB 는 재수집할 때마다 DOI 기준으로 갱신되므로 몇 번이든 다시 실행 가능
uv run paper-radar evidence collect --query "cosmetic retinol" --from 2016 --to 2026

# trend: collect(네트워크, 커서 저장) -> records/normalize/aggregate/unmatched(로컬)
uv run paper-radar trend collect   --profile sunscreen
uv run paper-radar trend aggregate --profile sunscreen

# trials / ingredient: 각자 독립된 저장 표면(trial/ingredient 테이블)
uv run paper-radar trials collect --query "niacinamide"
uv run paper-radar ingredient resolve --name "niacinamide"
uv run paper-radar ingredient import-cosing --path data/reference/cosing/cosing.csv
```

## 읽기 전에

`trend` 산출물을 열기 전에 [`docs/trend-data.md`](docs/trend-data.md) 의
caveat 1(OpenAlex 키워드 어휘가 2025-10 에 교체됨 — `trend_metrics.csv` 의
`growth_ratio`/`trend_class` 를 그대로 믿으면 안 됩니다)를 반드시 읽으세요.
가설·반증조건의 측정 근거는 [`docs/trend-assumptions.md`](docs/trend-assumptions.md)
에, 이 저장소가 의도적으로 안 했거나 미룬 것들은
[`docs/judgment-debt.md`](docs/judgment-debt.md) 에 있습니다.

## 설치·테스트

[uv](https://docs.astral.sh/uv/) 로 관리합니다.

```bash
uv sync --extra dev            # .venv 구성 (requests, python-dotenv + pytest, ruff)
uv run pytest                  # tests/paper_radar 전부 (네트워크 안 씀)
uv run ruff check .            # 린트

# 실제 API 응답 형태가 바뀌었는지 확인 (네트워크를 씁니다, 사람이 수동 실행)
uv run python tool/live_smoke.py

# CosIng CSV 다운로드 (일회성, 분기 갱신 시 재실행)
uv run python tool/fetch_cosing.py --url <CosIng CSV 다운로드 URL>
```

의존성은 `requests` 와 `python-dotenv` 뿐입니다. 나머지는 표준 라이브러리입니다.

## 사용자 액션

- **키 발급**(무료, 코드와 무관하게 병행 가능): OpenAlex(openalex.org),
  Semantic Scholar, NCBI(선택). 키 없이도 모든 명령이 동작하지만 예산이
  좁아집니다 — 위 환경 변수 표 참고.
- **KCI·ScienceON 신청**: 한국 논문(대한화장품학회지 등, 국제 색인의 사각
  지대) 소스는 승인 절차가 필요합니다. 신청은 지금 넣어 둘 수 있지만
  **통합은 이번 범위 밖**입니다(계획이 "승인 나기 전 착수하지 않는다"고
  명시) — 승인 후 별도 태스크로 진행합니다. 자세한 사정은
  [`docs/judgment-debt.md`](docs/judgment-debt.md) 의 "알고 미룸" 절 참고.

## 설계 문서

[docs/superpowers/specs/2026-08-19-papers-evidence-collector-design.md](docs/superpowers/specs/2026-08-19-papers-evidence-collector-design.md)
— 2026-08-19 작성된 초기 설계입니다. 구 `papers/` 패키지 시대의 문서이고
본문은 수정하지 않았습니다. 현재 구조는 이 README 를 기준으로 보세요.
