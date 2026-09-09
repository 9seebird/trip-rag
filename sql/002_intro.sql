-- 소개정보(detailIntro2) 저장용.
-- 유형마다 필드 이름이 달라(usetime / usetimeculture / opentimefood …) 원본은 통째로 보관하고,
-- 자주 쓰는 몇 개만 공통 컬럼으로 뽑아 둔다.
ALTER TABLE places ADD COLUMN IF NOT EXISTS intro_raw   JSONB;
ALTER TABLE places ADD COLUMN IF NOT EXISTS usefee      TEXT;
ALTER TABLE places ADD COLUMN IF NOT EXISTS infocenter  TEXT;
ALTER TABLE places ADD COLUMN IF NOT EXISTS baby_ok     BOOLEAN;   -- 유모차 대여
ALTER TABLE places ADD COLUMN IF NOT EXISTS menu        TEXT;      -- 음식점 대표메뉴
