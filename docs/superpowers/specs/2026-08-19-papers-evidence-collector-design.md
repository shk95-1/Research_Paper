# 화장품 트렌드 논문 근거 수집기 — 설계

작성일: 2026-08-19

## 1. 목적

화장품 트렌드 탐지 프로젝트에서 도출한 트렌드와 주장을 뒷받침할 논문 근거를
자동으로 수집하고 검증하는 로컬 툴을 만든다.

PDF 원문은 내려받지 않는다. 메타데이터, 초록, 요약까지만 확보한다.
따라서 Unpaywall은 사용하지 않는다.

## 2. 범위

기존 YouTube 수집·분석 파이프라인과 완전히 독립적으로 동작한다.
`papers/` 하위 패키지에 격리하며, 루트의 기존 스크립트는 수정하지 않는다.
검색 질의는 영문만 받는다. 한국어 키워드 매핑은 이번 범위에 없다.
`product_trend_matrix.csv` 등 기존 산출물과의 자동 대조도 범위 밖이다.

## 3. 사용 API

| # | API | Base URL | 용도 |
|---|-----|----------|------|
| 1 | OpenAlex | `https://api.openalex.org` | 메인 수집 + 연도별 집계 |
| 2 | Semantic Scholar | `https://api.semanticscholar.org/graph/v1` | tldr 요약, 인용수 |
| 3 | Europe PMC | `https://www.ebi.ac.uk/europepmc/webservices/rest` | 생명과학 도메인 보강 |
| 4 | Crossref | `https://api.crossref.org` | DOI/메타데이터 검증 |

네 API 모두 인증 키 없이 사용 가능하다.

## 4. 인증과 환경 변수

`.env`에서 읽는다. 어느 값도 코드에 하드코딩하지 않는다.

| 변수 | 필수 | 용도 |
|------|------|------|
| `OPENALEX_EMAIL` | 권장 | polite pool용 연락 이메일 |
| `OPENALEX_API_KEY` | 선택 | Premium 계정 키. 있으면 `api_key=` 파라미터로 전달 |
| `SEMANTIC_SCHOLAR_API_KEY` | 선택 | 있으면 `x-api-key` 헤더로 전달 |

`OPENALEX_EMAIL`이 없으면 polite pool 없이 동작한다. 경고를 출력하고 계속 진행한다.

이메일은 `mailto=` 쿼리 파라미터와 User-Agent 헤더로 외부 서버에 전송된다.
사용자가 `.env`에 직접 넣은 값만 사용한다.

## 5. 모듈 구조

```
papers/
  __main__.py           python -m papers 진입점
  cli.py                collect / trend / cite 서브커맨드. 인자 파싱과 출력만 담당
  pipeline.py           collect 파이프라인 조립. 소스 호출 순서와 실패 흡수
  http.py               공용 세션. polite 헤더, 호스트별 지연, 지수 백오프
  cache.py              (source, key) -> 원본 응답 캐시
  store.py              SQLite 스키마, upsert, JSON 덤프
  verify.py             title_match, confidence_score
  sources/
    openalex.py         search() + 역색인 초록 복원
    semantic_scholar.py fetch(doi)
    europepmc.py        fetch(doi, title)
    crossref.py         fetch(doi)
  tests/                fixture 기반. 네트워크 없이 실행
  out/                  papers.db, papers.json. gitignore 대상
```

### 인터페이스

소스 모듈은 두 가지 형태 중 하나만 노출한다.

```python
# 검색 소스
openalex.search(query, year_from, year_to, limit) -> list[dict]

# 보강 소스
semantic_scholar.fetch(doi=None, title=None) -> dict | None
europepmc.fetch(doi=None, title=None)        -> dict | None
crossref.fetch(doi=None, title=None)         -> dict | None
```

보강 소스 셋은 시그니처를 통일하지만 동작은 다르다. Crossref와 Semantic Scholar는
DOI만 사용하며 `doi`가 없으면 즉시 `None`을 반환한다. Europe PMC는 DOI를 먼저
시도하고 없으면 제목으로 검색한다.

보강 소스는 조회 실패나 미발견을 모두 `None`으로 반환한다. 예외를 밖으로 던지지 않는다.

모든 네트워크 접근은 `http.py`를 경유한다. 테스트는 이 한 지점만 대체하면 된다.

### 의존성

새 의존성 없음. `requests`(기존 설치됨)와 표준 라이브러리
`sqlite3`, `difflib`, `argparse`, `json`, `os`, `time`, `unittest`만 사용한다.
pytest는 설치되어 있지 않으므로 테스트는 표준 라이브러리 `unittest`로 작성하고
`python -m unittest discover -s papers/tests` 로 실행한다.
제목 유사도는 `difflib.SequenceMatcher`로 계산한다. `rapidfuzz`는 도입하지 않는다.

## 6. 파이프라인

```
[입력: 검색 키워드 + 연도 범위 + 개수 상한]
   ↓
1) OpenAlex 검색 -> 논문 리스트 (doi, title, year, abstract, cited_by_count, is_retracted)
   - filter=title_and_abstract.search:<query> 를 사용한다 (아래 실측 근거 참조)
   - select= 로 필요한 필드만 요청해 응답 크기를 줄인다
   - cursor 페이지네이션으로 limit까지 수집한다
   - abstract_inverted_index를 일반 텍스트로 복원
   ↓
2) Semantic Scholar 조회 (DOI 기준) -> tldr, abstract, citationCount 보강
   ↓
3) Europe PMC 조회 (DOI 또는 제목 기준) -> 생명과학 논문 여부, abstract 보강
   ↓
4) Crossref 조회 (DOI 기준) -> DOI 유효성, 저널명, 출판사 검증
   ↓
5) verification 산출 -> 통합 레코드로 upsert + JSON 덤프
```

2~4단계는 DOI가 없으면 건너뛴다. Europe PMC만 제목으로도 조회를 시도한다.

각 단계는 독립적으로 실패할 수 있다. 실패한 단계의 필드는 `null`로 남기고
파이프라인은 다음 논문으로 계속 진행한다.

### 역색인 초록 복원

OpenAlex의 `abstract_inverted_index`는 `{"단어": [위치, ...]}` 형태다.
(위치, 단어) 쌍을 모두 펼쳐 위치 기준으로 정렬한 뒤 공백으로 join한다.

처리해야 할 경우:
- 필드 자체가 없거나 `null` -> `None` 반환
- 빈 dict -> `None` 반환
- 한 단어가 여러 위치에 등장 -> 각 위치에 모두 배치
- 위치 번호에 빈틈이 있음 -> 있는 것만 순서대로 이어붙인다

## 7. 검증 로직

각 논문에 대해 수집 시점에 한 번 계산하고, 그 논문 행에 함께 저장한다.
조회 시 재계산하지 않는다. `verify.py`는 저장된 행을 입력으로 받는 순수 함수다.

### 항목

- `crossref_verified` (bool): Crossref에서 DOI 조회 성공 여부
- `title_match` (bool): OpenAlex 제목과 Crossref 제목의 유사도가 0.85 이상인지.
  비교 전에 소문자화, 공백 정규화, 구두점 제거를 적용한다. 정확 일치가 아니다.
- `found_in_sources` (list): 발견된 소스 이름 목록. OpenAlex는 항상 포함된다.
- `is_retracted` (bool): OpenAlex의 `is_retracted` 필드
- `has_doi` (bool): DOI 보유 여부
- `confidence_score` (int, 0~100)

### 배점

| 항목 | 점수 |
|------|------|
| OpenAlex에 존재 (기본) | 5 |
| `crossref_verified` | 30 |
| `title_match` | 25 |
| 추가 소스 발견 (Semantic Scholar / Europe PMC, 개당 15) | 최대 30 |
| 초록 확보 | 10 |
| **`is_retracted` = true** | **총점을 0으로 강제** |

최대 100점.

2개 이상 소스에서 발견 + Crossref 검증 통과 + 철회되지 않음 + 초록 확보는
`5 + 30 + 25 + 15 + 10 = 85`점이 되어 높은 신뢰도로 판정된다.

DOI가 없는 논문은 제외하지 않는다. 저장하되 `has_doi: false`로 표시한다.
DOI 기반 조회를 건너뛰므로 자연히 낮은 점수를 받는다
(기본 5 + 초록 10 + Europe PMC 제목 조회 성공 시 15 = 최대 30).

## 8. 저장

### SQLite (`papers/out/papers.db`)

`papers` 테이블. primary key는 `key` 컬럼이다.

- `key`: DOI가 있으면 DOI, 없으면 `openalex_id`. 정규화된 소문자.
- `doi`: nullable
- `openalex_id`, `title`, `authors`(JSON), `year`, `journal`, `abstract`,
  `tldr`, `keywords`(JSON), `topics`(JSON), `citation_count`,
  `is_open_access`, `url`, `collected_at`
- verification 항목은 조회 가능하도록 개별 컬럼으로 펼친다:
  `crossref_verified`, `title_match`, `found_in_sources`(JSON),
  `is_retracted`, `has_doi`, `confidence_score`
- `raw`: 통합 레코드 전체 JSON

`cache` 테이블: `(source, key)` 복합 키, `response` JSON, `fetched_at`.

`INSERT ... ON CONFLICT(key) DO UPDATE`로 upsert한다. 같은 논문을 두 번
수집하면 1행이 갱신된다.

### JSON 백업 (`papers/out/papers.json`)

`collect` 실행이 끝날 때마다 DB 전체를 레코드 배열로 덮어쓴다.

### 레코드 스키마

```json
{
  "doi": "10.xxxx/xxxx",
  "openalex_id": "https://openalex.org/W...",
  "title": "...",
  "authors": ["...", "..."],
  "year": 2024,
  "journal": "...",
  "abstract": "...",
  "tldr": "...",
  "keywords": ["retinol", "skin barrier"],
  "topics": ["Dermatology", "Cosmetic Science"],
  "citation_count": 12,
  "is_open_access": true,
  "url": "https://doi.org/...",
  "verification": {
    "crossref_verified": true,
    "title_match": true,
    "found_in_sources": ["openalex", "semantic_scholar"],
    "is_retracted": false,
    "has_doi": true,
    "confidence_score": 85
  },
  "collected_at": "2026-08-19T10:00:00Z"
}
```

## 9. 레이트 리밋과 재시도

호스트별 최소 요청 간격:

| 호스트 | 간격 |
|--------|------|
| OpenAlex | 0.1s |
| Crossref | 0.1s |
| Europe PMC | 0.2s |
| Semantic Scholar | 1.2s (키 없음 기준) |

`SEMANTIC_SCHOLAR_API_KEY`가 설정되면 Semantic Scholar 간격을 0.2s로 낮춘다.

429와 5xx는 지수 백오프로 재시도한다: 2s, 4s, 8s, 16s. 최대 5회.
응답에 `Retry-After` 헤더가 있으면 **그 값을 우선한다**. 실측에서 OpenAlex가
`Retry-After: 39` ~ `40`을 반환했으므로 짧은 고정 백오프만으로는 복구되지 않는다.
대기 시간은 60초로 상한을 둔다.
5회 모두 실패하면 해당 소스에 대해 `None`을 반환하고 진행한다.

캐시는 DOI 단위로 동작하므로 재실행 시 보강 단계가 거의 즉시 끝난다.
100건 수집 시 첫 실행은 Semantic Scholar 지연 때문에 2~3분이 걸린다.

## 10. 에러 처리 원칙

- 한 API가 실패해도 파이프라인 전체가 멈추지 않는다. 해당 필드만 `null`로 둔다.
- API 스키마는 변경될 수 있다. 응답 필드 접근은 전부 `.get()`으로 방어한다.
- 네트워크 예외, JSON 파싱 실패, 예상과 다른 응답 형태를 모두 소스 모듈 내부에서
  흡수하고 `None`을 반환한다.
- 실패는 stderr에 어느 소스의 어느 키에서 무엇이 실패했는지 한 줄로 기록한다.

## 11. CLI

```bash
# 수집
python -m papers collect --query "cosmetic retinol" --from 2020 --to 2025 --limit 100

# 트렌드 집계 (연도별 논문 수)
python -m papers trend --query "cosmetic retinol"

# 저장된 논문 검색 (근거 인용용)
python -m papers cite --keyword "skin barrier" --min-confidence 70
```

- `collect`: 파이프라인 실행. `--limit` 기본값 25.
- `trend`: OpenAlex `group_by=publication_year`로 연도별 개수를 집계해 출력한다.
  논문 본문을 수집하지 않으므로 호출 1회로 끝난다.
- `cite`: 로컬 DB만 조회한다. 네트워크를 쓰지 않는다.
  `title`, `abstract`, `keywords`에서 키워드를 대소문자 구분 없이 부분 일치로 찾고
  `confidence_score` 하한을 적용한다. `--min-confidence` 기본값 0.
  `confidence_score` 내림차순, 동점이면 `year` 내림차순으로 출력한다.
  철회된 논문은 기본적으로 제외한다.

## 12. 테스트

`papers/tests/`에 각 API의 실제 응답을 fixture JSON으로 저장하고
네트워크 없이 실행한다.

커버 대상:

- 역색인 초록 복원: 정상, 필드 없음, 빈 dict, 중복 위치, 위치 빈틈
- 제목 유사도: 0.85 경계값, 대소문자·구두점·공백 차이
- 배점 계산: 각 항목 조합, 철회 시 0점 강제, DOI 없는 경우
- upsert 멱등성: 같은 키 두 번 저장하면 1행
- 백오프: `Retry-After` 헤더 존중, 최대 재시도 후 `None` 반환
- 소스 모듈이 깨진 응답에 예외를 던지지 않는지
- CLI 인자 파싱

실제 API를 호출하는 스모크 테스트는 `papers/tests/live_smoke.py`로 분리한다.
`unittest discover`의 기본 패턴이 `test*.py`이므로 이 파일은 자동 수집되지 않는다.
직접 실행할 때만 네트워크를 사용한다.

## 13. API 실측 결과 (2026-08-19 확인)

설계 근거가 된 실제 응답이다. 구현 시 이 필드 경로를 그대로 쓴다.

### 검색 모드 선택

| filter | `cosmetic` 결과 수 |
|--------|------------------|
| `fulltext.search` (= 기본 `search=`) | 496,202 |
| `title_and_abstract.search` | 137,680 |

기본 `search=`는 본문 전문을 뒤지므로 화장품이 스쳐 지나가는 논문까지 잡힌다.
근거 수집이 목적이므로 `title_and_abstract.search`를 쓴다.

### 초록 확보율

`title_and_abstract.search:cosmetic` 상위 50건 중 `abstract_inverted_index`가
있는 건은 **36건(72%)**, DOI가 있는 건은 50건(100%)이었다.
나머지 28%는 Semantic Scholar와 Europe PMC 보강으로 채워야 한다.
즉 보강 단계는 선택이 아니라 초록 확보의 필수 경로다.

### 필드 경로

OpenAlex (`select` 파라미터 동작 확인, `cursor` 페이지네이션 동작 확인):

- `id` -> `openalex_id` (예: `https://openalex.org/W2618188783`)
- `doi` -> **`https://doi.org/10.1016/...` 형태의 전체 URL**.
  Crossref/Semantic Scholar 조회 전에 접두사를 떼어 bare DOI로 정규화해야 한다.
- `title`, `publication_year`, `is_retracted`, `cited_by_count`, `type`, `language`
- `open_access.is_oa` -> `is_open_access`
- `primary_location.source.display_name` -> `journal`
- `primary_location.source.host_organization_name` -> 출판사
- `topics[].display_name` -> `topics`
- `keywords[].display_name` -> `keywords`
- `authorships[].author.display_name` -> `authors`
- `abstract_inverted_index` -> `{"단어": [위치, ...]}`. 없으면 `null`

Crossref (`https://api.crossref.org/works/<bare DOI>`):

- **`message.title`은 리스트다.** `[0]`을 꺼내 써야 한다.
- **`message.container-title`도 리스트다.** 저널명.
- `message.publisher`, `message.type`, `message.issued.date-parts`
- 존재하지 않는 DOI는 **404**를 반환한다. 이것이 `crossref_verified=false`의 근거다.
- `message.abstract`는 대체로 없다. Crossref는 초록 보강용이 아니다.

Semantic Scholar (`/paper/DOI:<bare DOI>?fields=...`):

- **`tldr`은 문자열이 아니라 `{"model": ..., "text": ...}` 객체다.**
  `tldr.text`를 꺼내야 한다.
- `abstract`는 `tldr`이 있어도 `null`일 수 있다. 서로 독립이다.
- `citationCount`가 OpenAlex `cited_by_count`와 다르다 (실측 1341 vs 1805).
  `citation_count`는 **OpenAlex 값을 우선**하고, 없을 때만 S2 값으로 채운다.
- `venue`, `year`, `externalIds`, `isOpenAccess`

Europe PMC (`/search?query=DOI:"<bare DOI>"&format=json&resultType=core`):

- `resultType=core`가 없으면 초록이 오지 않는다.
- `resultList.result[].abstractText` -> 초록
- `resultList.result[].journalInfo.journal.title` -> 저널명
- **`resultList.result[].keywordList.keyword`는 리스트다.**
- `hitCount`가 0이면 미발견

### 레이트 리밋 실측

OpenAlex가 연속 호출에서 429를 반환하며 `Retry-After: 39`, `Retry-After: 40`을
줬다. 고정 백오프 1/2/4/8초로는 복구되지 않는다. `Retry-After`를 반드시 존중한다.

응답 `meta`에 `cost_usd` 필드가 있다 (실측 `0.001`). OpenAlex가 사용량을
계량한다는 뜻이므로 `select`로 응답을 줄이고 캐시를 적극적으로 쓴다.

## 14. 구현 순서

1. `http.py`
2. `store.py` (papers + cache 테이블)
3. `cache.py`
4. `sources/openalex.py` (역색인 복원 포함)
5. `sources/crossref.py`
6. `sources/semantic_scholar.py`
7. `sources/europepmc.py`
8. `verify.py`
9. `pipeline.py`
10. `cli.py` + `__main__.py`

각 단계는 테스트를 먼저 작성한다.
