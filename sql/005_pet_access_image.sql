-- 반려동물 동반(KorPetTourService2) · 무장애(KorWithService2) · 추가 사진(detailImage2)
--
-- pet_allowed  : 원래 소개정보(chkpet)로 채우려던 칸인데 KorService2 가 값을 안 줘서 늘 비어 있었다.
--                이제 반려동물 동반여행 목록에 있으면 TRUE.
-- pet_info     : detailPetTour2 원본(동반 구분·동반 가능 동물·필요 사항 …)
-- access_listed: 무장애 여행 목록에 있으면 TRUE
-- access_info  : detailWithTour2 원본(휠체어·유모차·엘리베이터·화장실 …)
-- stroller_ok / wheelchair_ok : access_info 에서 뽑은 표시용 값 (유모차 대여·이동, 휠체어 접근)
-- image_status : detailImage2 로 사진을 찾아봤는지 (done = 찾음, none = 없음)
ALTER TABLE places ADD COLUMN IF NOT EXISTS pet_info      JSONB;
ALTER TABLE places ADD COLUMN IF NOT EXISTS access_listed BOOLEAN;
ALTER TABLE places ADD COLUMN IF NOT EXISTS access_info   JSONB;
ALTER TABLE places ADD COLUMN IF NOT EXISTS stroller_ok   BOOLEAN;
ALTER TABLE places ADD COLUMN IF NOT EXISTS wheelchair_ok BOOLEAN;
ALTER TABLE places ADD COLUMN IF NOT EXISTS image_status  TEXT;
CREATE INDEX IF NOT EXISTS places_pet_idx    ON places (pet_allowed) WHERE pet_allowed;
CREATE INDEX IF NOT EXISTS places_access_idx ON places (access_listed) WHERE access_listed;

-- 부가 목록(반려동물·무장애)을 지역별로 언제 받았는지. 목록은 자주 안 바뀌어 일주일에 한 번만 다시 받는다
CREATE TABLE IF NOT EXISTS extra_lists (
  service   TEXT NOT NULL,       -- pet | access
  area_code INT  NOT NULL,
  done_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (service, area_code)
);
