"""로컬 참조 파일 파서 — API 소스가 아니다.

`paper_radar.sources.*` 는 전부 네트워크 저편의 API 를 호출해 매번 최신
데이터를 받아 온다(SourcePolicy/registry.register 로 정책·페이스를 선언하는
것도 그래서다). CosIng 은 다르다 — EU 집행위원회가 API 없이 CSV/Excel
내보내기 파일만 제공한다(Task 13 브리핑의 "용량은 크지만 일회성이고 수가
매우 적은 경우는 다운로드" 조항). 그래서 이 패키지는:

    - 네트워크를 만지지 않는다(그건 tool/fetch_cosing.py 의 몫 — 사람이
      분기마다 한 번씩 실행한다).
    - SourcePolicy/registry.register 를 쓰지 않는다 — 페이스·예산 정책은
      "매 실행마다 API 를 때리는" 소스에만 의미가 있고, 이미 로컬 디스크에
      받아 둔 파일을 읽는 데는 해당하지 않는다.
    - 순수 파서만 담는다: 로컬 CSV(향후 다른 참조 파일도 이 패키지에
      추가될 수 있다) -> IngredientRecord(models.py) 이터레이터.

`paper_radar.ingredients.import_cosing` 이 이 패키지를 호출해 조회 결과를
DB 에 저장하고 RunLog 에 자기기록한다(오케스트레이션은 여기가 아니라
ingredients/ 의 몫 — sources/ 계층과 ingredients/ 계층의 관계와 같은 분업).
"""
