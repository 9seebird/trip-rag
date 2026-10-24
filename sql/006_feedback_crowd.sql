-- 결과 피드백 · 혼잡도 예측(관광지 집중률)

-- 법정동 코드. 관광지 집중률 API 는 TourAPI 지역코드(areacode/sigungucode)가 아니라
-- 법정동 시도 2자리(areaCd) + 시군구 5자리(signguCd)를 받는다. 목록 응답의 lDongRegnCd / lDongSignguCd.
ALTER TABLE places ADD COLUMN IF NOT EXISTS ldong_regn TEXT;
ALTER TABLE places ADD COLUMN IF NOT EXISTS ldong_sgg  TEXT;     -- 시군구 3자리 (5자리 = regn || sgg)
CREATE INDEX IF NOT EXISTS places_ldong_idx ON places (ldong_regn, ldong_sgg);

-- 검색 결과 좋아요/별로. IP·위치는 남기지 않는다.
--   content_id 가 있으면 '이 장소가 이 질문에 맞았나', 없으면 '이 검색 결과 전체가 괜찮았나'.
--   좋아요가 쌓인 (질문, 장소) 는 평가 정답셋 후보가 된다 (eval/from_feedback.py).
CREATE TABLE IF NOT EXISTS feedback (
  id         BIGSERIAL PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  q          TEXT NOT NULL,
  area       INT,
  content_id BIGINT,
  rank       INT,
  vote       SMALLINT NOT NULL CHECK (vote IN (-1, 1))
);
CREATE INDEX IF NOT EXISTS feedback_ts_idx ON feedback (ts);
CREATE INDEX IF NOT EXISTS feedback_q_idx  ON feedback (q, content_id);

-- 관광지 집중률 예측 (TatsCnctrRateService). 관광지마다 앞으로 30일, 그 관광지의 과거 최고 혼잡을 100 으로 둔 상대값.
-- 이름(tAtsNm)으로만 오므로 우리 장소와는 '같은 시군구 + 정규화한 이름 완전일치' 로만 잇는다 (content_id).
CREATE TABLE IF NOT EXISTS crowd_forecast (
  signgu_cd  TEXT NOT NULL,
  name       TEXT NOT NULL,
  ymd        DATE NOT NULL,
  rate       REAL,
  content_id BIGINT,
  PRIMARY KEY (signgu_cd, name, ymd)
);
CREATE INDEX IF NOT EXISTS crowd_cid_idx ON crowd_forecast (content_id, ymd);

CREATE TABLE IF NOT EXISTS crowd_lists (
  signgu_cd  TEXT PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  rows       INT,
  matched    INT
);
