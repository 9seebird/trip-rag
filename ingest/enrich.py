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

# ── 규칙 (2026-11 수정) ───────────────────────────────
# 예전 규칙은 '실내 낱말'을 제목·소개글 어디서든 찾고, 실외 규칙보다 먼저 돌렸다.
# 그래서 소개글에 주변 시설이 한 번만 나와도 실외 장소가 실내로 굳었다.
#   해운대 관광특구  ← "관광특구에는 …미술관, 아쿠아리움 등도 있으며"
#   송도 구름산책로  ← "새로운 랜드마크"의 '랜드'
#   9.81 파크 제주   ← "레이싱 테마파크", "실내 게임도 많다"
# 바꾼 점
#   1) 규칙은 제목만 본다. 소개글의 낱말은 "해변 앞", "근처 미술관"처럼 위치 설명에
#      섞여 나오는 일이 많아서(소향씨어터·해수랜드가 '해변' 때문에 실외로 잡히는 등)
#      소개글 판단은 모델에 맡긴다.
#   2) 제목에 시설 낱말(박물관·갤러리…)이 있으면 실내가 먼저다 ("통일공원안보전시관").
#   3) 제목 끝 괄호는 떼고 본다 ("비봉산(양구)" → "비봉산").
#   4) 애매한 낱말(랜드·테마파크·아쿠아)은 뺐다.
INDOOR_TITLE = r"(박물관|미술관|전시관|아쿠아리움|수족관|과학관|기념관|체험관|아트센터|" \
               r"갤러리|영화관|공연장|도서관|실내|온실|뮤지엄)"
OUTDOOR_TITLE = r"(오름|해변|해수욕장|해안|숲길|올레|둘레길|폭포|계곡|포구|항구|공원묘지|" \
                r"자연휴양림|곶자왈|목장|해안도로|전망대|산책로|관광특구|공원|마을|" \
                r"릉원|고분|사지$|누각|대교$|[^학]교\s|^\S+(봉|악|산|섬|총|릉)$)"
# 끝 글자(봉·악·산·섬)는 띄어쓰기 없는 이름에만: "한화리조트 설악" 은 산이 아니다.
# '굴'은 뺐다 — 동굴·땅굴은 비 오는 날에도 괜찮은 곳이 많다.
# 제목 끝의 (지역) · [코스명] 같은 꾸밈을 뗀 제목
TITLE_N = r"regexp_replace(title, '\s*[\(\[][^\)\]]*[\)\]]\s*', ' ', 'g')"

# 규칙 판정 한 줄: TRUE/FALSE = 규칙으로 확정, NULL = 규칙으로 못 정함(→ 모델)
RULE_VERDICT = f"""
CASE
  WHEN content_type = 39 THEN TRUE                          -- 음식점은 사실상 실내
  WHEN trim({TITLE_N}) ~ %(in_t)s THEN TRUE
  WHEN trim({TITLE_N}) ~ %(out_t)s THEN FALSE
END"""  # 나머지는 NULL: 모델이 소개글을 읽고 판단
RULE_PARAMS = dict(in_t=INDOOR_TITLE, out_t=OUTDOOR_TITLE)


def apply_rules(conn):
    """아직 안 정한 곳에 규칙을 적용한다 (매일 수집 후 기본 동작)."""
    n = conn.execute(f"""
        UPDATE places p SET indoor = v.verdict, indoor_source = 'rule'
        FROM (SELECT content_id, {RULE_VERDICT} AS verdict FROM places WHERE indoor IS NULL) v
        WHERE p.content_id = v.content_id AND v.verdict IS NOT NULL""", RULE_PARAMS).rowcount
    print(f"규칙: {n}건")


def recheck_rules(conn, dry=False):
    """예전 규칙으로 정한 값(indoor_source='rule')을 새 규칙으로 다시 본다.
    모델(llm) 판정이나 손으로 고친 값은 건드리지 않는다.
    새 규칙이 NULL 이면 판정을 비워서 이어지는 모델 단계가 다시 보게 한다."""
    rows = conn.execute(f"""
        SELECT content_id, title, addr, indoor AS old, verdict AS new FROM (
          SELECT content_id, title, addr, indoor, {RULE_VERDICT} AS verdict
          FROM places WHERE indoor_source = 'rule') v
        WHERE verdict IS DISTINCT FROM indoor
        ORDER BY addr, title""", RULE_PARAMS).fetchall()
    label = {True: "실내", False: "실외", None: "미정(모델로)"}
    print(f"새 규칙으로 바뀌는 곳: {len(rows)}건")
    for r in rows[:300]:
        print(f"  {label[r['old']]:>4} → {label[r['new']]:<8} {r['title']}  ({(r['addr'] or '')[:20]})")
    if len(rows) > 300:
        print(f"  … 외 {len(rows) - 300}건")
    if dry or not rows:
        return
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("UPDATE places SET indoor = %s, indoor_source = %s"
                        " WHERE content_id = %s AND indoor_source = 'rule'",
                        (r["new"], "rule" if r["new"] is not None else None, r["content_id"]))
    print(f"{len(rows)}건 반영")

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
    ap.add_argument("--recheck", action="store_true",
                    help="예전 규칙으로 정한 값만 새 규칙으로 다시 판정 (--dry-run 과 같이 쓰면 바뀔 목록만 출력)")
    args = ap.parse_args()

    with connect() as conn:
        if args.reset and not args.dry_run:
            n = conn.execute("UPDATE places SET indoor=NULL, indoor_source=NULL").rowcount
            print(f"{n}건 초기화")

        if args.recheck:
            recheck_rules(conn, dry=args.dry_run)

        if not args.dry_run:
            apply_rules(conn)

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
