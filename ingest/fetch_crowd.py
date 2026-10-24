"""혼잡도 예측 — 한국관광공사 '관광지 집중률 방문자 추이 예측' (TatsCnctrRateService).

    python -m ingest.fetch_crowd --areas 39,6,32           # 법정동 코드 채우기 → 시군구별 예측 받기
    python -m ingest.fetch_crowd --probe                   # 원본 응답 한 번 찍어 보기 (호출 1회)

무엇을 주나
  관광지마다 앞으로 30일의 '집중률'. 그 관광지의 과거(2018~) 가장 붐빈 때를 100 으로 둔 상대값이다.
  KT 이동통신 데이터 기반 예측이라 '지금 몇 명' 이 아니라 '그날 평소보다 얼마나 붐빌지' 로 읽어야 한다.

두 가지 걸림돌
  1. 지역 코드가 다르다. 이 API 는 법정동 코드(시도 2자리 areaCd + 시군구 5자리 signguCd)만 받는다.
     우리 places 는 TourAPI 지역코드만 있었다 → 목록 응답의 lDongRegnCd / lDongSignguCd 를 받아 채운다.
     새로 받는 목록(fetch_list)은 이제 같이 저장하고, 예전에 받은 곳은 여기서 관광지·문화시설·레포츠 목록만
     다시 받아 코드만 채운다 (지역당 수 회~수십 회, TourAPI 한도를 같이 쓰므로 남은 몫만).
  2. 장소 id 가 없고 이름(tAtsNm)만 온다 → '같은 시군구 + 괄호·공백을 뗀 이름 완전일치' 로만 잇는다.
     포함관계까지 허용하면 '○○리조트' 에 '○○항' 값이 붙는 식으로 틀린 값이 섞여서, 못 잇는 쪽을 택한다.

예측은 30일치라 시군구마다 7일에 한 번만 다시 받는다 (crowd_lists).
공공데이터포털에서 활용신청을 따로 해야 하고, 신청 전이면 조용히 건너뛴다.
"""
import argparse
import json
import os
import re
import time
from datetime import date

from .config import TOUR_DAILY_LIMIT
from .db import connect, tour_used_today, used_today
from .tourapi import call, items, QuotaExceeded, TourApiError

SERVICE = "TatsCnctrRateService"
OP = "tatsCnctrRatedList"
API = "crowd"
LIMIT = int(os.getenv("CROWD_DAILY_LIMIT", "300"))
CODES_BUDGET = int(os.getenv("CROWD_CODES_BUDGET", "200"))     # 법정동 코드 채우기에 쓸 TourAPI 호출 상한/일
SIGHT_TYPES = (12, 14, 28)
# 시군구 5자리. 목록 응답의 lDongSignguCd 는 보통 3자리(시도 뒤 부분)라 시도 코드를 앞에 붙인다
SGG5 = "(CASE WHEN length({p}ldong_sgg) = 5 THEN {p}ldong_sgg ELSE {p}ldong_regn || lpad({p}ldong_sgg, 3, '0') END)"
NOT_REGISTERED = ("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "20", "SERVICE_ACCESS_DENIED_ERROR")


def norm(name):
    """'성산일출봉 (UNESCO 세계자연유산)' → '성산일출봉'. 괄호 안·공백·가운뎃점을 뗀다."""
    s = re.sub(r"[\(\[（【].*?[\)\]）】]", "", name or "")
    return re.sub(r"[\s·•.,'\"]", "", s).lower()


def sgg5(regn, sgg):
    regn, sgg = (regn or "").strip(), (sgg or "").strip()
    if not regn or not sgg:
        return None
    return sgg if len(sgg) == 5 else regn + sgg.zfill(3)


# ── 1) 법정동 코드 채우기 ─────────────────────────────────────────────────────
def fill_codes(conn, areas, budget):
    left = min(budget, max(0, TOUR_DAILY_LIMIT - tour_used_today(conn)))
    spent = 0
    for area in areas:
        for t in SIGHT_TYPES:
            missing = conn.execute("""SELECT count(*) c FROM places WHERE area_code = %s AND content_type = %s
                                      AND ldong_sgg IS NULL""", (area, t)).fetchone()["c"]
            if not missing:
                continue
            page, got = 1, 0
            while True:
                if spent >= left:
                    print(f"  코드 채우기: 오늘 몫({left}회)을 다 써서 내일 이어서")
                    return spent
                body = call(conn, "areaBasedList2", {"numOfRows": 100, "pageNo": page, "arrange": "C",
                                                     "areaCode": area, "contentTypeId": t})
                spent += 1
                rows = items(body)
                with conn.cursor() as cur:
                    for it in rows:
                        regn, sgg = it.get("lDongRegnCd"), it.get("lDongSignguCd")
                        if regn and sgg and str(it.get("contentid", "")).isdigit():
                            cur.execute("UPDATE places SET ldong_regn = %s, ldong_sgg = %s WHERE content_id = %s",
                                        (str(regn), str(sgg), int(it["contentid"])))
                            got += 1
                total = int(body.get("totalCount") or 0)
                if not rows or page * 100 >= total:
                    break
                page += 1
                time.sleep(0.2)
            print(f"  [지역 {area} 유형 {t}] 법정동 코드 {got}곳 채움")
    return spent


# ── 2) 시군구별 예측 받기 ─────────────────────────────────────────────────────
def fetch_sgg(conn, regn, code):
    rows, page = [], 1
    while True:
        body = call(conn, OP, {"areaCd": regn, "signguCd": code, "numOfRows": 1000, "pageNo": page},
                    service=SERVICE, api=API, limit=LIMIT)
        got = items(body)
        rows += got
        total = int(body.get("totalCount") or 0)
        if not got or len(rows) >= total or len(got) < 1000:
            return rows
        page += 1
        time.sleep(0.2)


def save(conn, code, rows):
    """저장하고, 같은 시군구 우리 장소와 이름으로 잇는다. 반환: (예측 행 수, 이어진 관광지 수)"""
    places = conn.execute("""SELECT content_id, title FROM places
                             WHERE content_type = ANY(%s) AND """ + SGG5.format(p="") + " = %s",
                          (list(SIGHT_TYPES), code)).fetchall()
    by_name = {}
    for p in places:
        by_name.setdefault(norm(p["title"]), p["content_id"])
    n, linked = 0, set()
    with conn.cursor() as cur:
        for it in rows:
            name, ymd, rate = it.get("tAtsNm"), str(it.get("baseYmd") or ""), it.get("cnctrRate")
            if not name or len(ymd) != 8:
                continue
            try:
                rate = float(rate)
            except (TypeError, ValueError):
                rate = None
            cid = by_name.get(norm(name))
            if cid:
                linked.add(cid)
            cur.execute("""INSERT INTO crowd_forecast (signgu_cd, name, ymd, rate, content_id)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (signgu_cd, name, ymd) DO UPDATE SET rate = EXCLUDED.rate,
                                                                            content_id = EXCLUDED.content_id""",
                        (code, name, date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:])), rate, cid))
            n += 1
    conn.execute("""INSERT INTO crowd_lists (signgu_cd, rows, matched) VALUES (%s, %s, %s)
                    ON CONFLICT (signgu_cd) DO UPDATE SET fetched_at = now(), rows = EXCLUDED.rows,
                                                          matched = EXCLUDED.matched""", (code, n, len(linked)))
    # 지난 날짜는 지운다 (예측이라 지난 값은 쓸 데가 없다)
    conn.execute("DELETE FROM crowd_forecast WHERE ymd < (now() AT TIME ZONE 'Asia/Seoul')::date - 1")
    return n, len(linked)


def run(areas, codes_budget):
    with connect() as conn:
        try:
            spent = fill_codes(conn, areas, codes_budget)
            if spent:
                print(f"법정동 코드 채우기: TourAPI {spent}회")
        except QuotaExceeded as e:
            print(f"코드 채우기 한도 도달: {e}")

        targets = conn.execute(f"""
            SELECT DISTINCT p.ldong_regn regn, {SGG5.format(p="p.")} code
            FROM places p
            LEFT JOIN crowd_lists l ON l.signgu_cd = {SGG5.format(p="p.")}
            WHERE p.area_code = ANY(%s) AND p.content_type = ANY(%s) AND p.ldong_sgg IS NOT NULL
              AND (l.fetched_at IS NULL OR l.fetched_at < now() - interval '7 days')
            ORDER BY 2""", (areas, list(SIGHT_TYPES))).fetchall()
        if not targets:
            print("혼잡도: 다시 받을 시군구 없음 (7일 안에 받음)")
            return
        total_rows = total_linked = done = 0
        for t in targets:
            if used_today(conn, API) >= LIMIT:
                print(f"혼잡도: 오늘 한도({LIMIT}) 도달 — 남은 {len(targets) - done}곳은 내일")
                break
            try:
                rows = fetch_sgg(conn, t["regn"], t["code"])
            except QuotaExceeded as e:
                print(f"혼잡도 한도 도달로 중단: {e}")
                break
            except TourApiError as e:
                if e.code in NOT_REGISTERED or "NOT_REGISTERED" in str(e) or "ACCESS_DENIED" in str(e):
                    print(f"{SERVICE} 활용신청이 안 돼 있어 건너뜀 (공공데이터포털 15128555 신청) — {e}")
                    return
                if e.code in ("03", "NODATA_ERROR"):           # 그 시군구엔 대상 관광지가 없음
                    rows = []
                else:
                    print(f"  ! {t['code']}: {e}")
                    continue
            n, linked = save(conn, t["code"], rows)
            total_rows += n; total_linked += linked; done += 1
            time.sleep(0.2)
        cov = conn.execute("SELECT count(DISTINCT content_id) c FROM crowd_forecast WHERE content_id IS NOT NULL"
                           ).fetchone()["c"]
        print(f"혼잡도: 시군구 {done}곳 · 예측 {total_rows}행 · 이어진 관광지 {total_linked}곳 (전체 {cov}곳)")


def probe():
    """활용신청·파라미터 확인용. 우리 DB 의 첫 시군구로 한 번 부르고 원본을 찍는다."""
    with connect() as conn:
        r = conn.execute(f"""SELECT ldong_regn regn, {SGG5.format(p="")} code, title FROM places
                            WHERE ldong_sgg IS NOT NULL AND content_type = 12 LIMIT 1""").fetchone()
        regn, code = (r["regn"], r["code"]) if r else ("50", "50110")       # 없으면 제주시
        print(f"areaCd={regn} signguCd={code}")
        try:
            body = call(conn, OP, {"areaCd": regn, "signguCd": code, "numOfRows": 5, "pageNo": 1},
                        service=SERVICE, api=API, limit=LIMIT)
        except TourApiError as e:
            print("실패:", e)
            return
        print("totalCount", body.get("totalCount"))
        print(json.dumps(items(body)[:5], ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--areas", default="39")
    ap.add_argument("--codes-budget", type=int, default=CODES_BUDGET)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    if a.probe:
        probe()
        return
    run([int(x) for x in a.areas.split(",") if x.strip()], a.codes_budget)


if __name__ == "__main__":
    main()
