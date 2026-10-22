"""파생값 채우기 — 원본에 없는 값을 만들어 검색에 쓴다.

TourAPI 에 '실내/실외' 필드는 없다. 그런데 "비 오는 날" 질문에는 이게 제일 중요하다.
그래서 두 단계로 채운다.

  1. 규칙   — 제목·소개글의 낱말로 확실한 것만. 공짜고 빠르다.
  2. 모델   — 규칙으로 못 정한 것만 LLM 에게 소개글을 읽히고 판단시킨다.
              한 번만 하면 되니 비용이 작다 (879건 기준 수십 원).

어느 쪽으로 정했는지 indoor_source 에 남긴다. 나중에 '규칙만 / 규칙+모델' 을
평가 표에서 비교할 수 있게.

    docker compose run --rm ingest python -m ingest.enrich --dry-run
    docker compose run --rm ingest python -m ingest.enrich
"""
import argparse
import json
import re

from .bizrouter import chat
from .db import connect

INDOOR_WORDS = r"(박물관|미술관|전시관|전시館|아쿠아리움|수족관|과학관|기념관|체험관|아트센터|" \
               r"갤러리|영화관|공연장|도서관|실내|온실|테마파크|뮤지엄|랜드|아쿠아)"
OUTDOOR_WORDS = r"(오름|해변|해수욕장|해안|숲길|올레|둘레길|폭포|계곡|포구|항구|" \
                r"공원묘지|자연휴양림|곶자왈|목장|해안도로|전망대|봉|악$|산$|섬$|굴$)"

RULE = f"""
UPDATE places SET indoor = %s, indoor_source = 'rule'
WHERE indoor IS NULL AND (title ~ %s OR coalesce(overview,'') ~ %s)
"""

PROMPT = """다음은 국내 관광지·시설 목록이다. 각 항목이 비 오는 날에도 문제없이 즐길 수 있는
실내 공간인지 판단하라.

- true: 건물 안에서 대부분의 활동이 이뤄짐 (박물관, 카페, 실내 체험관, 식당 등)
- false: 야외 활동이 중심 (오름, 해변, 산책로, 야외 공원 등)
- null: 실내외가 섞여 있거나 판단할 근거가 부족함

JSON 배열로만 답하라. 형식: [{"id": 123, "indoor": true}, ...]
설명이나 다른 문장은 쓰지 마라.

목록:
"""


def llm_pass(conn, rows, batch=20, dry=False):
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        listing = "\n".join(
            f'{r["content_id"]} | {r["title"]} | {(r.get("overview") or "")[:180]}'
            for r in part)
        if dry:
            print(f"  [dry] {len(part)}건 요청했을 것")
            continue
        try:
            out = chat(conn, [{"role": "user", "content": PROMPT + listing}], temperature=0)
            m = re.search(r"\[.*\]", out, re.S)
            data = json.loads(m.group(0)) if m else []
        except Exception as e:
            print(f"  ! 배치 실패: {e}")
            continue
        with conn.cursor() as cur:
            for d in data:
                v = d.get("indoor")
                if v is None:
                    continue
                cur.execute("UPDATE places SET indoor = %s, indoor_source = 'llm'"
                            " WHERE content_id = %s AND indoor IS NULL", (bool(v), d.get("id")))
                done += cur.rowcount
        print(f"  {min(i + batch, len(rows))}/{len(rows)}")
    return done


def report(conn):
    print("\n실내 판정 결과")
    for r in conn.execute("""
        SELECT content_type,
               count(*) FILTER (WHERE indoor IS TRUE) indoor,
               count(*) FILTER (WHERE indoor IS FALSE) outdoor,
               count(*) FILTER (WHERE indoor IS NULL) unknown
        FROM places GROUP BY content_type ORDER BY content_type""").fetchall():
        print(f"  유형 {r['content_type']:>3} · 실내 {r['indoor']:>4} · 실외 {r['outdoor']:>4}"
              f" · 미정 {r['unknown']:>4}")
    for r in conn.execute("SELECT indoor_source, count(*) n FROM places"
                          " WHERE indoor_source IS NOT NULL"
                          " GROUP BY indoor_source").fetchall():
        print(f"  판정 근거 {r['indoor_source']}: {r['n']}건")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="규칙만")
    ap.add_argument("--reset", action="store_true", help="기존 판정을 지우고 처음부터")
    args = ap.parse_args()

    with connect() as conn:
        if args.reset and not args.dry_run:
            n = conn.execute("UPDATE places SET indoor=NULL, indoor_source=NULL").rowcount
            print(f"{n}건 초기화")

        # 1) 음식점은 사실상 전부 실내
        if not args.dry_run:
            n = conn.execute("UPDATE places SET indoor = TRUE, indoor_source = 'rule'"
                             " WHERE content_type = 39 AND indoor IS NULL").rowcount
            print(f"규칙(음식점): {n}건")

            n = conn.execute(RULE, (True, INDOOR_WORDS, INDOOR_WORDS)).rowcount
            print(f"규칙(실내 낱말): {n}건")
            n = conn.execute(RULE, (False, OUTDOOR_WORDS, OUTDOOR_WORDS)).rowcount
            print(f"규칙(실외 낱말): {n}건")

        rest = conn.execute(
            "SELECT content_id, title, overview FROM places"
            " WHERE indoor IS NULL AND overview IS NOT NULL ORDER BY priority, content_id"
        ).fetchall()
        print(f"\n규칙으로 못 정한 {len(rest)}건")

        if rest and not args.no_llm:
            n = llm_pass(conn, rest, dry=args.dry_run)
            print(f"모델 판정: {n}건")

        report(conn)
        c = conn.execute("SELECT coalesce(sum(cost_krw),0) c FROM api_calls"
                         " WHERE api='bizrouter' AND op='chat'"
                         " AND ts > now() - interval '1 hour'").fetchone()
        print(f"\n이번 판정 비용 {float(c['c']):.2f}원")


if __name__ == "__main__":
    main()
