-- 지역별 목록(areaBasedList2) 수집 완료 표시.
-- 전에는 "places 에 그 지역 행이 하나라도 있으면 목록은 받은 것" 으로 봤다.
-- 여러 지역을 한 번에 넣으면 목록 받다가 한도에 걸려 일부 유형만 들어갈 수 있는데,
-- 그러면 다음 날 그 지역 목록을 다시 받지 않아 나머지 유형이 영영 빠진다. 그래서 따로 적는다.
CREATE TABLE IF NOT EXISTS area_lists (
  area_code  INT PRIMARY KEY,
  done       BOOLEAN NOT NULL DEFAULT FALSE,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  done_at    TIMESTAMPTZ
);
