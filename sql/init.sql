-- trip-rag 초기 스키마
CREATE EXTENSION IF NOT EXISTS vector;

-- ─────────────────────────────────────────────────────────────
-- 장소
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS places (
  content_id     BIGINT PRIMARY KEY,          -- TourAPI 고유 ID
  content_type   INT  NOT NULL,               -- 12 관광지 14 문화시설 15 축제 28 레포츠 39 음식점
  title          TEXT NOT NULL,
  addr           TEXT,
  addr_detail    TEXT,
  area_code      INT,
  sigungu_code   INT,
  cat1           TEXT,
  cat2           TEXT,
  cat3           TEXT,
  lat            DOUBLE PRECISION,            -- mapy
  lon            DOUBLE PRECISION,            -- mapx
  image_url      TEXT,
  thumb_url      TEXT,
  tel            TEXT,

  overview       TEXT,                        -- 상세 소개글 (임베딩 대상)
  overview_hash  TEXT,                        -- 바뀐 것만 다시 임베딩하려고
  homepage       TEXT,

  -- 파생값 (원본에 없음 / ingest.enrich 가 채움)
  indoor         BOOLEAN,
  indoor_source  TEXT,                        -- 'cat' | 'llm' | NULL

  -- 소개정보(detailIntro) 에서 오는 것들
  usetime        TEXT,
  restdate       TEXT,
  parking        TEXT,
  pet_allowed    BOOLEAN,

  -- 수집 진행 상태 ───────────────────────────────
  priority       INT  NOT NULL DEFAULT 5,     -- 낮을수록 먼저. 1=관광지/문화시설 … 5=음식점
  detail_status  TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | skipped
  detail_tries   INT  NOT NULL DEFAULT 0,
  detail_error   TEXT,
  intro_status   TEXT NOT NULL DEFAULT 'pending',  -- 소개정보는 2차. pending | done | failed | skipped
  intro_tries    INT  NOT NULL DEFAULT 0,

  list_modified  TEXT,                        -- API 의 modifiedtime. 갱신 감지용
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  fetched_at     TIMESTAMPTZ
);

-- 상세 수집기가 "다음에 뭐 할지" 고를 때 쓰는 인덱스
CREATE INDEX IF NOT EXISTS places_detail_queue_idx
  ON places (detail_status, priority, content_id);
CREATE INDEX IF NOT EXISTS places_intro_queue_idx
  ON places (intro_status, priority, content_id);
CREATE INDEX IF NOT EXISTS places_filter_idx
  ON places (area_code, content_type);
CREATE INDEX IF NOT EXISTS places_geo_idx
  ON places (lat, lon);

-- ─────────────────────────────────────────────────────────────
-- 청크 + 임베딩
--   모델을 바꾸면 벡터 공간이 달라 섞어 쓸 수 없다.
--   그래서 어떤 모델/차원으로 만든 벡터인지 같이 남긴다. (실험 비교용)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chunks (
  id          BIGSERIAL PRIMARY KEY,
  content_id  BIGINT NOT NULL REFERENCES places(content_id) ON DELETE CASCADE,
  seq         INT    NOT NULL,
  text        TEXT   NOT NULL,
  model       TEXT   NOT NULL,
  dim         INT    NOT NULL,
  embedding   VECTOR(1024),                   -- HNSW 인덱스 한계가 2000차원. 1024로 받아 저장
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (content_id, seq, model)
);

CREATE INDEX IF NOT EXISTS chunks_hnsw_idx
  ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_model_idx
  ON chunks (model);

-- 키워드 검색 (비교 보기의 "왼쪽" 화면). 한국어라 형태소 분석기 대신 trigram 으로 간다.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS places_title_trgm_idx
  ON places USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS places_overview_trgm_idx
  ON places USING gin (overview gin_trgm_ops);

-- ─────────────────────────────────────────────────────────────
-- 외부 API 호출 기록 — 쿼터 관리 + 비용 표의 근거
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_calls (
  id         BIGSERIAL PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  api        TEXT NOT NULL,        -- 'tour' | 'bizrouter'
  op         TEXT NOT NULL,        -- 'areaBasedList2' | 'embeddings' | ...
  ok         BOOLEAN NOT NULL,
  status     INT,
  ms         INT,
  tokens     INT,
  cost_krw   NUMERIC(12,4),        -- 비즈라우터 usage.cost
  note       TEXT
);

CREATE INDEX IF NOT EXISTS api_calls_kst_day_idx
  ON api_calls (api, ((ts AT TIME ZONE 'Asia/Seoul')::date));

-- 오늘(KST) 몇 번 썼는지
CREATE OR REPLACE VIEW tour_quota_today AS
SELECT count(*) AS used
FROM api_calls
WHERE api = 'tour'
  AND (ts AT TIME ZONE 'Asia/Seoul')::date = (now() AT TIME ZONE 'Asia/Seoul')::date;

-- 수집 진행 현황 한눈에
CREATE OR REPLACE VIEW ingest_progress AS
SELECT content_type,
       count(*)                                        AS total,
       count(*) FILTER (WHERE detail_status = 'done')  AS detail_done,
       count(*) FILTER (WHERE detail_status = 'pending') AS detail_pending,
       count(*) FILTER (WHERE detail_status = 'failed')  AS detail_failed,
       count(*) FILTER (WHERE overview IS NOT NULL)    AS has_overview
FROM places
GROUP BY content_type
ORDER BY content_type;
