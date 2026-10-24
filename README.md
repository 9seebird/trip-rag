# trip-rag

"비 오는 날 애기랑 갈 만한 제주 실내" 같은 말로 장소를 찾고, 고른 곳 근처로 반나절 코스를 짜주는 서비스.
한국관광공사 TourAPI 데이터를 PostgreSQL + pgvector 로 검색합니다.

설계는 `docs/설계문서.md` 참고.

## 시작하기

```powershell
copy .env.example .env      # 키 두 개 채우기 (TOUR_API_KEY, BIZROUTER_API_KEY)
docker compose up -d db
docker compose build

# 1) API 스펙 확인 — 호출 3회만 씁니다. 2000건 긁기 전에 이것부터.
docker compose run --rm ingest python -m ingest.probe

# 1-1) 건수만 먼저 세기 (전국 유형별 5회, --by-area 는 85회)
docker compose run --rm ingest python -m ingest.counts

# 2) 목록 수집 (음식점 포함 전체, 40회 남짓)
docker compose run --rm ingest python -m ingest.fetch_list

# 3) 상세(소개글) 수집 — 우선순위 순, 하루 예산만큼만
docker compose run --rm ingest python -m ingest.fetch_detail --budget 900

#    밤새 걸어두려면 (한도 차면 자고, 자정 지나면 이어서)
docker compose run --rm ingest python -m ingest.fetch_detail --budget 900 --loop

# 4) 소개정보 (운영시간·애완동물·유모차 등). 먼저 마이그레이션 한 번.
docker compose run --rm ingest python -m ingest.migrate
docker compose run --rm ingest python -m ingest.fetch_intro --budget 900

# 상태 보기
docker compose up -d api      # http://localhost:8080/api/stats
```

## 매일 자동 수집 (서버 cron)

서비스 서버(노트북 Ubuntu)에서 매일 10:00(KST)에 수집 → 실내/실외 판정 → 색인을 이어서 돌린다.
지역은 `scripts/areas.txt` (지금 전국 17개 시도). 설치·운영은 `docs/배포가이드_노트북.md` 5장.

```bash
bash scripts/daily_collect.sh     # 한 번 돌리기 (로그 logs/collect_YYYYMMDD.log)
bash scripts/install_cron.sh      # cron 등록
```

`scripts/*.ps1` 은 예전 Windows PC 용(작업 스케줄러)이다. 지금은 Linux 방식이 기본.

## 데이터 규모 — 재고 시작했다

시작 전에 `totalCount` 만 세어 봤다. 목록 조회는 `numOfRows=1` 로 불러도 총계를 주므로
유형별 전국 건수는 API 5회면 알 수 있다 (`ingest/counts.py`).

| 유형 | 전국 | 제주 |
|---|---:|---:|
| 관광지 | 12,623 | 309 |
| 문화시설 | 2,725 | 36 |
| 레포츠 | 3,773 | 65 |
| 축제공연행사 | 890 | 7 |
| 음식점 | 13,475 | 462 |
| **합계** | **33,486** | **879** |

제주만 하면 **879건**이라 상세 수집이 하루 한도(1,000회) 안에 들어온다.
전국으로 늘리면 33,486건 = 상세만 34일치다. 확장 시점에 운영계정 전환이 필요하다.

이 한 번의 계측이 일정 전체를 바꿨다. 처음 설계에서는 "며칠에 걸쳐 나눠 받는다"를 전제로
우선순위 큐를 만들었는데, 실제로 재 보니 제주는 하루면 끝나는 규모였다.

## 하루 1,000회 제한을 다루는 방법

상세 조회는 **건당 1회**다. 제주는 하루에 들어오지만 전국은 34일이 걸리므로,
수집기는 처음부터 "끊겨도 이어서 도는" 구조로 만들었다.

* 진행 상태를 DB(`places.detail_status`, `intro_status`)에 남긴다. 다시 돌리면 남은 것부터 간다.
* `priority` 순으로 간다 — 관광지·문화시설 → 레포츠 → 축제 → 음식점.
  음식점은 코스 짜기에 이름·좌표·분류만 있으면 되므로 소개글을 뒤로 미룬다.
* 실패는 `failed` 로 두고 `--retry-failed` 로 따로 재시도한다.
* 모든 호출을 `api_calls` 에 남기고, 한도는 **KST 자정 기준**으로 센다.
  (DB 는 UTC 라 그냥 세면 한국 시각 오전 9시에 리셋되는 것처럼 보인다.)
* `--loop` 을 주면 한도 소진 후 대기했다가 자정 넘어 이어서 돈다.

## API 를 쓰면서 걸린 것들

* `detailCommon2` 에 `contentTypeId` 를 보내면 `INVALID_REQUEST_PARAMETER_ERROR` 가 난다.
  `contentId` 하나만 보내야 한다. 반면 `detailIntro2` 는 `contentTypeId` 가 **필요하다**.
* 옛 버전의 `overviewYN=Y` 같은 플래그는 KorService2 에서 거부된다. 플래그 없이 전부 온다.
* 서비스키는 포털이 Encoding/Decoding 두 벌을 준다. Encoding 키를 그대로 파라미터에 넣으면
  이중 인코딩돼서 인증이 깨진다. 코드에서 `%` 가 보이면 되돌려 쓴다.
* 게이트웨이 오류는 응답 껍데기가 아예 다르다(`OpenAPI_ServiceResponse.cmmMsgHeader`).
  `response.header.resultCode` 만 보면 원인을 놓친다.
* `overview` 에 `<br>`, `homepage` 에 `<a href=...>` 가 섞여 온다. 저장 전에 턴다 (`ingest/textclean.py`).

## 소개정보로 조건 필터 만들기

`detailIntro2` 는 운영시간·휴무일·주차·애완동물 동반·유모차 대여를 구조화된 값으로 준다.
유형마다 필드 이름이 다르다 (`usetime` / `usetimeculture` / `opentimefood` …).
원본은 `intro_raw`(JSONB)에 통째로 넣고 공통으로 쓸 것만 컬럼으로 뽑는다.

"애기랑 갈 만한" 을 소개글 해석에 맡기지 않고 `baby_ok` 로 정확히 거를 수 있다.

## 공공데이터를 더 엮은 것

| 데이터 | 쓰는 곳 | 어려웠던 점 |
|---|---|---|
| 관광지 집중률 예측 (`TatsCnctrRateService`) | 카드 '붐빔/여유 예상', 상세 이번 주 막대, 첫 화면 '주말에 여유로울 곳', 코스 경고 | 지역 코드가 법정동 체계라 목록의 `lDongRegnCd`·`lDongSignguCd` 를 따로 채움. 장소 id 없이 이름만 와서 같은 시군구 + 이름 완전일치로만 이음 |
| 기상청 단기예보 | 비 예보면 '실내로 다시 찾기' | 위경도 → 격자 변환 |
| 반려동물·무장애 여행 | 조건 필터·뱃지·상세 | 서비스마다 활용신청·한도가 따로 |
| 소개정보의 운영시간·쉬는 날 (글) | '오늘 휴무', 코스 도착 시각 경고 | 사람이 쓴 글이라 확실한 것만 읽음 (`app/hours.py`) |

사용자 피드백(좋아요/별로)은 `/admin` 에서 보고, `eval/from_feedback.py` 로 평가 정답 후보를 뽑는다.
검색 품질을 고치는 고리가 '평가셋 → 숫자 → 바꿈' 에서 '실제 사용자 → 평가셋' 으로 이어진다.

## 검색 평가 — 하이브리드를 기본으로 둔 근거

제주 질문 19개에 정답 장소를 직접 달아 두고(`eval/questions.yaml`, `eval/labels.yaml`) 방식별로 비교했다.
운영 서버 DB(전국 수집본)에서 돌린 결과다.

| 방식 | S@5 | recall@5 | MRR | 응답 |
|---|---|---|---|---|
| 키워드만 | 0.368 | 0.118 | 0.177 | 425ms |
| 벡터만 | 0.632 | 0.225 | **0.499** | 528ms |
| 벡터 + 조건필터 | 0.632 | 0.225 | 0.499 | 568ms |
| **하이브리드 (RRF)** | **0.737** | **0.228** | 0.466 | 978ms |
| 하이브리드 + 조건필터 | 0.737 | 0.228 | 0.466 | 842ms |

* S@5 = 상위 5개 안에 정답이 하나라도 있는 질문 비율, MRR = 첫 정답 순위의 역수 평균.
* 하이브리드는 벡터 대비 19문항 중 2문항을 더 맞혔다. 고유명사·지명처럼 의미 임베딩이 흐리게 잡는 낱말을 키워드 쪽이 건져 온다.
* 대신 MRR 이 조금 내려간다. 키워드 쪽 1위가 정답 앞에 끼어드는 경우가 있다.
* 조건필터는 이 질문 세트에 실내·반려동물 같은 조건이 거의 없어 차이가 없었다.

키워드 가중치(`HYBRID_KW_WEIGHT`)를 바꿔 본 결과:

| 가중치 | S@5 | MRR |
|---|---|---|
| 0.3 | 0.684 | 0.455 |
| 0.5 | 0.737 | 0.457 |
| **0.7 (기본)** | **0.737** | **0.466** |

가중치를 낮추면 MRR 이 오를 거라 예상했지만 오히려 둘 다 떨어졌다. 0.7 을 기본값으로 유지한다.

```bash
docker compose -f docker-compose.prod.yml --profile tools run --rm -T \
  -e HYBRID_KW_WEIGHT=0.5 ingest python -m eval.run_eval --configs 벡터만,하이브리드
```
