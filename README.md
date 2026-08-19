# cosmetics_research_paper

화장품 트렌드 분석 작업 공간.

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
python -m papers collect --query "cosmetic" --from 2016 --to 2026 --limit 100

# 연도별 논문 수
python -m papers trend --query "cosmetic retinol"

# 저장된 논문에서 근거 뽑기
python -m papers cite --keyword "skin barrier" --min-confidence 70
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
| `OPENALEX_EMAIL` | polite pool 연락처. 없으면 429가 잦아집니다 |
| `OPENALEX_API_KEY` | OpenAlex Premium 계정 키 |
| `SEMANTIC_SCHOLAR_API_KEY` | 있으면 rate limit이 완화됩니다 |

이메일은 `mailto=` 파라미터와 User-Agent로 각 API 서버에 전송됩니다.

### 테스트

```bash
# 단위 테스트. 네트워크를 쓰지 않습니다
python -m unittest discover -s papers/tests -t .

# 실제 API 응답 형태가 바뀌었는지 확인
python -m papers.tests.live_smoke
```

의존성은 `requests`와 `python-dotenv`뿐입니다. 나머지는 표준 라이브러리입니다.

설계 문서: [docs/superpowers/specs/2026-08-19-papers-evidence-collector-design.md](docs/superpowers/specs/2026-08-19-papers-evidence-collector-design.md)
