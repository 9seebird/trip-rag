"""매일 자동 수집 — 서버의 cron 이 하루 한 번 부른다 (scripts/daily_collect.sh).

하는 일
  1. 지정한 지역(--areas)의 목록을 아직 다 받지 못했으면 목록부터 받는다 (지역당 수 회~수십 회).
     다 받았는지는 area_lists 표가 기억한다. 받다가 한도에 걸리면 다음 날 그 지역 목록을 다시 받는다.
  2. 기간 축제(searchFestival2)를 받아 축제 기간을 적는다.
  3. 상세(detailCommon2) → 소개정보(detailIntro2) 순으로 남은 한도만큼 이어서 받는다.
     어디까지 했는지는 DB(detail_status, intro_status)가 기억하므로 매일 돌리면 알아서 이어간다.
     순서는 유형 우선순위(관광지·문화시설 → 레포츠 → 축제 → 음식점)가 먼저, 같은 유형 안에서는 --areas 순서.
  4. 지역별로 남은 건수를 찍고 끝난다.

실내/실외 판정(enrich)과 색인(build_index)은 이 스크립트가 아니라 daily_collect.sh 가 이어서 부른다.

한도는 TOUR_DAILY_LIMIT 를 따른다. 스케줄 실행에서는 950 으로 낮춰서 부르므로
그날 손으로 테스트할 몫(50회)이 남는다.

    docker compose -f docker-compose.prod.yml --profile tools run --rm -T \\
        -e TOUR_DAILY_LIMIT=950 ingest python -m ingest.daily --areas 39,6,32
"""
import argparse
import subprocess
import sys

from .config import TOUR_DAILY_LIMIT
from .db import connect, tour_used_today
from .migrate import apply as migrate

AREA_LABEL = {1: "서울", 2: "인천", 3: "대전", 4: "대구", 5: "광주", 6: "부산", 7: "울산", 8: "세종",
              31: "경기", 32: "강원", 33: "충북", 34: "충남", 35: "경북", 36: "경남", 37: "전북",
              38: "전남", 39: "제주"}


def step(*args):
    print(f"\n$ python -m {' '.join(args)}", flush=True)
    return subprocess.run([sys.executable, "-m", *args], check=False).returncode


def lists_needed(conn, areas):
    """목록을 (다시) 받아야 하는 지역.

    area_lists 에 '완료' 가 없으면 받는다. 이 표가 생기기 전에 받아 둔 지역(제주·부산·강원)도
    한 번은 다시 받는다 — 지역당 10~20회라 싸고, 예전에 목록이 덜 받혔어도 여기서 메워진다.
    """
    done = {r["area_code"] for r in conn.execute(
        "SELECT area_code FROM area_lists WHERE done").fetchall()}
    return [a for a in areas if a not in done]


def status(conn, areas):
    rows = conn.execute("""
        SELECT area_code, count(*) total,
               count(*) FILTER (WHERE detail_status = 'pending') detail_left,
               count(*) FILTER (WHERE intro_status  = 'pending') intro_left
        FROM places GROUP BY area_code ORDER BY area_code""").fetchall()
    have = {r["area_code"] for r in rows}
    print("\n지역별 남은 건수")
    for r in rows:
        name = AREA_LABEL.get(r["area_code"], "?")
        print(f"  {r['area_code']:>3} {name:<3} · 전체 {r['total']:>5}"
              f" · 상세 남음 {r['detail_left']:>5} · 소개 남음 {r['intro_left']:>5}")
    waiting = [a for a in areas if a not in have]
    if waiting:
        print("  목록 대기: " + ", ".join(f"{a} {AREA_LABEL.get(a, '?')}" for a in waiting))
    left = sum(r["detail_left"] + r["intro_left"] for r in rows)
    print(f"  → 남은 호출 약 {left}회 (목록 대기 지역 제외), 하루 {TOUR_DAILY_LIMIT}회 기준 약"
          f" {-(-left // TOUR_DAILY_LIMIT)}일")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--areas", default="6", help="쉼표로. 6=부산 32=강원 39=제주 (scripts/areas.txt 참고)")
    args = ap.parse_args()
    areas = [int(a) for a in args.areas.split(",") if a.strip()]
    areas = list(dict.fromkeys(areas))          # 중복 제거, 순서 유지
    area_arg = ",".join(map(str, areas))

    with connect() as conn:
        migrate(conn, quiet=True)               # area_lists 등 새 표가 없으면 만든다
        print(f"오늘(KST) 사용 {tour_used_today(conn)}/{TOUR_DAILY_LIMIT} · 지역 {area_arg}")
        need_list = lists_needed(conn, areas)

        for a in need_list:
            conn.execute("""INSERT INTO area_lists (area_code) VALUES (%s)
                            ON CONFLICT (area_code) DO UPDATE SET started_at = now()""", (a,))
            code = step("ingest.fetch_list", "--area", str(a))
            if code != 0:
                print(f"\n지역 {a} 목록을 다 못 받음(종료코드 {code}) — 남은 지역 목록은 내일 이어서")
                break
            conn.execute("UPDATE area_lists SET done = TRUE, done_at = now() WHERE area_code = %s", (a,))

    # 기간 축제 — 목록 API 로는 축제 기간이 안 와서 따로 받는다 (지역마다 1~3회)
    step("ingest.fetch_festival", "--areas", area_arg)
    step("ingest.fetch_detail", "--budget", str(TOUR_DAILY_LIMIT), "--areas", area_arg)
    step("ingest.fetch_intro", "--budget", str(TOUR_DAILY_LIMIT), "--areas", area_arg)

    with connect() as conn:
        print(f"\n오늘(KST) 사용 {tour_used_today(conn)}/{TOUR_DAILY_LIMIT}")
        status(conn, areas)


if __name__ == "__main__":
    main()
