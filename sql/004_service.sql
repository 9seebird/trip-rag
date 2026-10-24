-- 서비스 운영용: 응답 캐시, 검색 기록, 축제 기간
--
-- api_cache  : 같은 검색·코스·날씨를 다시 부르지 않게 (Gemini 무료 한도 절약 + 빠른 응답).
--              무엇을 얼마나 오래 둘지는 app/cache.py 가 정한다.
-- search_log : 관리 화면(/admin)용. 질문·결과 수·응답 시간만 남긴다. IP·위치 같은 개인정보는 남기지 않는다.
-- event_*    : 축제(유형 15)의 기간. searchFestival2 로 채운다 (ingest/fetch_festival.py).
CREATE TABLE IF NOT EXISTS api_cache (
  key        TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,                 -- find | course | weather
  value      JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_cache_created_idx ON api_cache (created_at);

CREATE TABLE IF NOT EXISTS search_log (
  id       BIGSERIAL PRIMARY KEY,
  ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind     TEXT NOT NULL,                   -- find | course
  q        TEXT,                            -- 검색어 (코스면 기준 장소 id)
  area     INT,
  near     BOOLEAN NOT NULL DEFAULT FALSE,  -- '내 근처' 로 찾았는지 (좌표는 남기지 않는다)
  results  INT,
  took_ms  INT,
  cached   BOOLEAN NOT NULL DEFAULT FALSE,
  ok       BOOLEAN NOT NULL DEFAULT TRUE,
  note     TEXT
);
CREATE INDEX IF NOT EXISTS search_log_ts_idx ON search_log (ts);

ALTER TABLE places ADD COLUMN IF NOT EXISTS event_start DATE;
ALTER TABLE places ADD COLUMN IF NOT EXISTS event_end   DATE;
CREATE INDEX IF NOT EXISTS places_event_idx ON places (event_end) WHERE content_type = 15;
