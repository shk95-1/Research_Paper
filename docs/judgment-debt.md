# judgment-debt — 이 저장소가 의도적으로 안 한 일, 비운 채로 둔 일, 미룬 일

trend-radar 교본의 judgment-debt 패턴(3분류: 결정하고 안 함 / 탐색했더니
비었음 / 알고 미룸)을 이 저장소에도 적용한다. 원료는 `.superpowers/sdd/
cosmic-wobbling-scone/progress.md` 원장의 Ruling·이연·minor 항목과 계획
파일(`plans/cosmic-wobbling-scone.md`)의 조사 결론이다. 각 항목은
**무엇을 · 왜 · 되돌리려면 무엇이 필요한가**로 적는다.

---

## 1. 결정하고 안 함 (decided against)

의도적으로 고르지 않은 선택지들이다. 다시 문제 제기하려면 아래 "되돌리려면"
을 먼저 읽을 것 — 대부분 근거가 이 저장소의 성격(단독 연구 배치 파이프라인,
처리량 비목표, 의존성 최소주의) 자체에서 나온다.

| 안 한 것 | 왜 | 되돌리려면 |
|---|---|---|
| PostgreSQL + SQLAlchemy + Alembic | trend-radar 교본의 Postgres 는 공유 DB·대시보드·다중 role 이 이유였다. 이 저장소는 단독 연구 배치 파이프라인이라 SQLite 로 충분 — 마이그레이션은 `PRAGMA user_version` + 번호 스크립트(`src/paper_radar/storage/migrations/`)로 경량 구현했다 | 여러 프로세스/사람이 동시에 같은 DB 에 쓰거나, 웹 대시보드가 실시간으로 붙는 요구가 생기면 재검토. `repository.py`/`schema.py` 를 SQLAlchemy 세션으로 바꾸고 마이그레이션 러너를 Alembic 으로 교체하는 규모의 작업 |
| Lane/Gate 동시성 (AIMD) | 처리량이 비목표(사용자 명시). 순차 요청 + 고정 인터벌(`SourcePolicy.min_interval_s`) + 예산 상한으로 충분하다고 판단. 429 는 지수 백오프로만 대응 | 소스가 지금보다 훨씬 많아지거나(수십 개) 하루 수집량이 지금의 자릿수를 넘어서면, `transport/` 계층에 동시 요청 풀을 넣는 재작업이 필요 |
| httpx / pydantic / typer / rich | 기존 `requests` + dataclass + argparse 조합이 이미 검증돼 있고, 의존성 최소주의가 이 저장소의 기록된 설계 결정이다. trend-radar 교본의 본질은 계층·계약이지 특정 라이브러리가 아니라고 판단 | CLI UX(진행바, 컬러 출력 등)나 비동기 I/O 가 실제로 필요해지면 그때 도입 여부를 다시 판단. 지금은 `argparse`/`requests` 로 요구사항을 전부 충족 |
| CHANGELOG 파일 | git 커밋 히스토리와 이 원장(`progress.md`)이 "무엇이 왜 바뀌었나"의 기록 역할을 이미 한다. 별도 파일을 유지하면 두 출처가 어긋날 위험만 생긴다 | 외부 소비자(예: 이 저장소를 라이브러리로 pip install 하는 다른 프로젝트)가 생겨 버전별 변경 요약이 필요해지면 그때 `CHANGELOG.md` 를 새로 시작 |
| PDF 파일 다운로드 | 계획 원문의 대용량 데이터 정책 — "PDF 등은 하이퍼링크로 대체"(일회성·소수·대용량 예외는 CosIng 처럼 별도 판단). Unpaywall 이 이 요구에 정확히 맞는 소스로 선택됨(`oa_location`, PDF 링크만 저장) | 로컬 아카이브(오프라인 열람, 링크 썩음 대비)가 필요해지면 `storage/oa_location` 테이블에 로컬 경로 컬럼을 추가하고 별도 다운로드 파이프라인을 붙이는 작업 |
| 사전(keyword_lexicon.json) 자동 등록 | `trend suggest`(T14)는 unmatched 표현에 PubChem/CosIng 동의어 후보를 **제안만** 한다 — 인간 검수 원칙(계획 원문 명시)을 지키기 위해 등록은 사람이 한다 | 후보 제안의 정밀도가 실측으로 충분히 검증되면(예: 오탐률 실측 후) 특정 조건(예: 정확 일치 + 카테고리 단일)에서만 반자동 등록을 검토 |
| 유사도(fuzzy) 매칭 — unmatched↔ingredient | T14 브리핑이 명시: 부분 일치·유사도 매칭은 하지 않는다 — 거짓 양성이 검수 신뢰를 무너뜨린다는 이유. `trend/normalize` 의 정규화 함수로 정규화한 뒤 정확 일치(dict.get)만 쓴다 | 정확 일치의 재현율이 실측으로 낮다고 확인되면(예: unmatched 상위권에 명백한 오타 변형이 쌓이면) 편집 거리 기반 후보 제시를 별도 검수 단계로 추가하는 것을 검토 |
| arXiv / Scopus·WoS / Lens 특허 / Google Patents BigQuery / RISS / S2 벌크 스냅샷 | 소스 조사 결론(계획 111~123행): arXiv 는 도메인 불일치, Scopus/WoS 는 유료, Lens 는 14일 체험만, BigQuery 는 과금 계정 필요, RISS 는 API 없음+ToS 위험, S2 벌크는 수백 GB 로 저장 정책과 충돌 | 예산이나 라이선스 상황이 바뀌면(예: 기관 구독) 재검토. 지금은 8개 무료/무인증 API + CosIng 참조파일로 요구 커버리지가 충분하다고 판단 |
| 특허 소스 (EPO OPS / KIPRIS) | 계획이 명시적으로 범위 밖으로 보류(계획 188행) — 필요해지면 별도 계획으로. KIPRIS 월 1,000콜 무료는 크롤링이 아니라 표적 조회용이라 이번 범위의 "전수 트렌드 관찰"과 성격이 다름 | "화장품 성분의 특허 동향"이 별도 요구로 명시되면, EPO OPS(무료 4req/s)나 KIPRIS 표적 조회를 새 소스로 별도 계획·태스크로 추가 |

---

## 2. 탐색했더니 비었음 (explored and empty)

### 로컬 raw 303MB 부재 → 합성 골든으로 대체

계획 원문은 "골든 비교 — 로컬 raw 303MB 로 재수집 없이 검증"을 전제했다.
T6 착수 시점(2026-08-21)에 확인해 보니 **이 전제가 틀렸다** — `papers_trend/
raw/` 의 OpenAlex 응답 원본 JSONL(303MB)은 이 머신 어디에도 없었다(원본
체크아웃·이 worktree 모두 부재, `.gitignore` 로 애초에 커밋된 적도 없음).

대체 검증: T6 시작 시점에 구 `papers_trend/` 코드가 아직 살아 있었으므로,
합성 raw 픽스처(~수십 건, 엣지케이스 포함)를 만들어 **구 코드로 골든
CSV 를 생성·커밋**하고(`tests/fixtures/trend_golden/`), 신 `trend/` 코드가
바이트 단위로 동일한 출력을 내는지 테스트로 고정했다. 실데이터 CSV
(`out/trend/sunscreen/*.csv`)는 `git mv` 만 하고 내용은 무변경으로 옮겼다.

**틀리면 비용**: 합성 데이터가 못 잡는 실데이터 특이 케이스(예: 실제
OpenAlex 응답에만 있는 필드 조합)를 골든 테스트가 놓칠 수 있다 — 차후
재수집 시 실측 검증으로 보완해야 한다.

---

## 3. 알고 미룸 (deferred)

### 이연 항목 (개별)

| 무엇 | 왜 미뤘나 | 되돌리려면 |
|---|---|---|
| RunLog+observer 러너 3사본 공용화 | `evidence.pipeline.collect()`/`trend.collect.run()`/`trend.collect_pubmed.run()`/`trials.collect.run()`(T11 시점 3개, T15 로 pubmed 축까지 4개) 이 사실상 같은 골격(RunLog.start → observer 설치(host_to_source 매핑 + log_fetch) → 점진 소비 → 저장 → record_source → finish(status) → observer 원복)을 각자 손으로 반복 구현한다. T11 에서 공용 헬퍼 추출을 검토했으나, 손대면 evidence/trend 기존 모듈까지 함께 건드리는 다중 모듈 동시 수정이 되어 그 태스크 범위를 넘어선다고 판단해 보류했다 | `storage/runlog.py` 에 "observer 설치·해제 + host_to_source 매핑"을 뽑아내는 컨텍스트 매니저/데코레이터를 만들고, 4개 러너를 순서대로(회귀 테스트를 각 단계마다 통과시키며) 옮기는 리팩터 태스크 |
| CI 부재 | 이 저장소에 `.github/workflows/` 등 CI 설정이 없다 — 계획·태스크 브리핑 어디에도 CI 구성 요구가 없었고, 전 과정 검증은 사람(또는 에이전트)이 `uv run pytest`/`uv run ruff check .` 를 수동 실행하는 방식으로 진행됐다 | GitHub Actions(또는 동등 CI)에 `uv sync --extra dev && uv run pytest && uv run ruff check .` 3줄짜리 워크플로를 추가하는 정도로 시작 가능 — 네트워크를 쓰는 테스트가 없으므로(가드 테스트로 강제됨) CI 러너에서 그대로 동작할 것 |
| CosIng Function/Restriction 컬럼 미저장 | T13 범위에서 `reference/cosing.py` 는 INCI name/INN name/CAS No/Chem-IUPAC name 만 저장한다. Function(규제 기능)·Restriction(Annex 제한) 컬럼은 `IngredientRecord` 모델에 필드가 없어 저장하지 않는다 — 소비자가 없는 상태에서 스키마를 넓히는 것은 YAGNI 라고 판단(아무도 읽지 않는 컬럼을 저장만 하는 꼴) | "이 성분은 Annex III 제한 대상인가" 같은 구체적 소비 요구가 생기면, `IngredientRecord` 에 필드를 추가하고 `import-cosing` 이 그 컬럼을 함께 파싱하도록 확장 |
| `pubmed_query` 실측 미검증 | `config.json` 의 sunscreen 프로파일 `pubmed_query`(T15)는 PubMed esearch 문법으로 작성된 구성 초안이며, 실제로 수집해 건수가 OpenAlex 실측(7,647건, 2023-09~2026-08)과 자릿수가 맞는지 아직 확인하지 않았다 | `uv run paper-radar trend collect --profile sunscreen --provider pubmed --dry-run` 으로 건수를 먼저 확인하고, OpenAlex 대비 크게 어긋나면(예: 10배 차이) 쿼리 문법을 조정 |
| `cosmetics` 프로파일 수집 미완 | OpenAlex 일일 예산 소진으로 9,483/59,751건에서 중단된 상태다(`docs/trend-data.md` caveat 2 참고). `--max-pages` 로 여러 날에 나눠 받는 것이 설계된 해법이지만 아직 완주하지 않았다 | `uv run paper-radar trend collect --profile cosmetics --max-pages N` 을 예산이 초기화되는 UTC 자정마다 반복 실행 — 저장된 커서 덕분에 이어받기가 된다 |
| KCI/ScienceON 승인 대기 | 계획이 "승인 나기 전 착수하지 않는다"고 명시(P5, 한국 소스). 사용자가 신청서를 제출해야 하는 절차이고 승인까지 시간이 걸린다 | 사용자가 KCI(`articleSearch`/`articleDetail`)와 ScienceON(KISTI) 신청을 완료하고 API 키를 발급받으면, 별도 계획(P5)으로 소스 통합 착수 — `kr_colloquial` 다리가 여기서 처음 소비처를 얻는다 |
| PubMed 원문 XML 무손실 이탈 | `trend/collect_pubmed.py`(T15)는 저장 레코드에 파싱된 필드만 담는다 — OpenAlex 축(`trend/collect.py`)이 지키는 "원본 응답 무손실 보존" 원칙에서 의도적으로 벗어난 것이다(모듈 docstring 에 근거 기록) | 원문 XML 이 필요해지면(예: 파싱 로직을 사후에 바꿔서 재추출하고 싶을 때) `fetch_batch()` 가 이미 받은 원문 응답을 그대로 append 하는 사이드카를 추가하는 리팩터. PubMed 는 재조회가 가능한 안정적 공개 API 라 지금 당장의 손실 위험은 낮다고 판단해 미룸 |

### 원장의 minor 항목 전체 (요약, 태스크별)

아래는 각 태스크 리뷰에서 "구현자 자기공개·재리뷰 승인·향후 인지용"으로
남긴 minor 발견이다. 전부 정상 동작을 막지 않는 잠복 사항이라 별도 수정
없이 완료 처리됐다 — 다음에 그 코드를 건드릴 사람을 위한 기록이다.

| 태스크 | 요약 |
|---|---|
| T1 | 402/409 예산소진 경고가 host 별 dedup 없이 반복 출력될 수 있음 / `collect_profile`의 mailto 배관 잔존(T6 이식 때 정리됨) |
| T3 | `auth_kind` 리터럴("param"/"header") 오타 시 조용히 무인증으로 빠짐(미검증) / `BudgetTracker` 가 `x-ratelimit-cost-usd` 헤더는 미수집(실측은 존재) |
| T4 | `PRAGMA foreign_keys` 미설정 — `ON DELETE CASCADE` 가 장식(run 삭제 API 가 생기기 전까지는 무해) / `scrub_url` 의 비-api_key 이름 케이스 테스트 없음 |
| T5a | `(payload or {})` 사어 방어 코드(새 계약에서 payload 가 falsy 일 수 없음) / 200 응답+body null 조합이면 `AttributeError`(OpenAlex 실동작엔 나타나지 않음) |
| T5b | 점수 동등성 테스트 CASES 에 추가소스 3개 케이스 없음(상한 산식은 0/1/2 소스로도 충분히 검증됨) |
| T8 | 가드 테스트 메서드명이 "four sources" 로 남아 5소스 시대에 낡음(이름만의 문제) / `upsert_records` 의 미지 정책 `ValueError` 분기가 두 번째 정책이 생기기 전까지 커버리지 없음 |
| T9 | Crossref `relation`/`update-to` 배열의 비-dict 항목 방어 분기 미테스트 / 수정 전 캐시된 크로스레프 응답(role 키 없음)은 재생 시 안전 기본값 "retracted" 로 처리되어 캐시 갱신 전까지 notice-only 레코드가 과잉 플래그될 수 있음(의도된 안전 기본값) |
| T10 | esearch 성공+efetch 실패 조합의 캐시 단언 테스트 없음 / `BASE` 상수 명명이 esearch/efetch 두 엔드포인트 중 하나만 지칭(netloc 이 같아 기능 문제는 없음) |
| T11 | m0007 마이그레이션의 "NULL = 미기재" 주석이 정상 쓰기 경로에서는 도달 불능(항상 `"[]"`) / `trials collect` 진행 출력이 수집 종료 후 일괄 표시(UX 트레이드오프, 자기공개) |
| T12 | `IngredientRecord` 최초 삽입 시 빈 튜플 필드가 `NULL` 아닌 `"[]"` 로 저장(읽기 동작은 동일, 스키마 주석과 표기만 차이) |
| T13 | `_resolve_fetched_at` 이 dict 아닌 유효 JSON `_meta` 를 만나면 `AttributeError`(안전하게 failed 로 끝남, 미테스트) / `iter_records` 의 BOM 처리 테스트가 docstring 주장을 실검증하지 않음(코드 자체는 정상) / 완전히 다른 CSV 를 import 하면 0건+exit 0(문서화된 트레이드오프, 명시적 오류 아님) |
| T14 | `unmatched` CSV 가 없을 때 생 `FileNotFoundError`(다른 trend 서브커맨드와 동일 실패 양식 — 친절한 메시지로 감쌀 여지 있음) |
| T15 | `provider_overlap.csv` 의 `month_bucket` 도출이 프로바이더마다 다름(openalex=publication_date, pubmed=esearch 창) — **문서화됨**: `docs/trend-data.md` F절 caveat 5번째 항목. `census` 등식(`expected==collected`)이 월 경계를 넘는 pdat 재배정(epub→print) 시 `is_census` 를 영구 False 로 고착시킬 수 있음(안전하게 실패 — 수집 손실은 없고 집계만 차단됨) |
