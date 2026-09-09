"""평가 정답 만들기 — pooled judgment.

정답을 손으로 적으면 두 가지가 문제다.
  * 879건을 다 아는 사람이 없다. 아는 곳만 적으면 정답 자체가 편향된다.
  * 지금 쓰는 검색으로 찾아서 적으면, 그 검색에 유리한 정답을 만드는 셈이다.

그래서 정보검색에서 쓰는 방식을 따른다.
  1. 여러 검색 방식(키워드 / 조건 없는 벡터 / 조건 있는 벡터)으로 후보를 넓게 모은다 = pooling
  2. 모델에게 후보를 하나씩 "이 질문의 답이 되는가" 판정시킨다
  3. 사람이 눈으로 훑고 고친다  ← 이 단계를 건너뛰지 말 것

    docker compose run --rm ingest python -m eval.build_pool
    docker compose run --rm ingest python -m eval.build_pool --only q01,q07
"""
import argparse
import json
import pathlib
import re

import yaml

from app.search import keyword_search, vector_candidates
from app.extract import extract
from ingest.bizrouter import chat
from ingest.db import connect

HERE = pathlib.Path(__file__).resolve().parent
QUESTIONS = HERE / "questions.yaml"
LABELS = HERE / "labels.yaml"

# 질문당 정답 상한. 정답이 k 보다 많으면 recall@k 의 최대값이 k/정답수 로 눌린다.
MAX_EXPECT = 8

PROMPT = """질문: "{q}"

아래는 제주도 장소 후보들이다. 각 장소가 이 질문에 대한 **좋은 답**인지 판정하라.

기준
- yes: 이 장소 하나만 추천받아도 질문자가 "이거면 됐다" 할 만한 곳
- no: 조건이나 상황에 안 맞거나, 맞긴 하는데 특별히 내세울 게 없는 곳
- 애매하면 no. **yes 는 이 묶음에서 최대 3개까지만** 고른다.
  비슷한 곳이 여럿이면 가장 대표적인 것만 남긴다.

JSON 배열로만 답하라. 형식: [{{"id": 123, "ok": true}}, ...]
다른 문장은 쓰지 마라.

후보:
{items}
"""


def judge(conn, q, cands, batch=15):
    good = []
    for i in range(0, len(cands), batch):
        part = cands[i:i + batch]
        items = "\n".join(
            f'{c["content_id"]} | {c["title"]} | {c["type"]} | '
            f'{"실내" if c.get("indoor") else "실외" if c.get("indoor") is False else "미정"} | '
            f'{(c.get("snippet") or "")[:130]}'
            for c in part)
        try:
            out = chat(conn, [{"role": "user",
                               "content": PROMPT.format(q=q, items=items)}], temperature=0)
            m = re.search(r"\[.*\]", out, re.S)
            for d in (json.loads(m.group(0)) if m else []):
                if d.get("ok"):
                    good.append(int(d["id"]))
        except Exception as e:
            print(f"    ! 판정 실패: {e}")
    return good


def pool(conn, q):
    """여러 방식으로 후보를 모은다. 한 방식에 유리하지 않게."""
    seen, out = {}, []
    ex = extract(q)
    sources = [
        ("keyword", lambda: keyword_search(conn, q, 20)),
        ("vector", lambda: vector_candidates(conn, q, 25)),
        ("vector+filter", lambda: vector_candidates(conn, q, 25, ex["filters"])),
    ]
    for name, fn in sources:
        try:
            for r in fn():
                cid = r["content_id"]
                if cid in seen:
                    seen[cid].add(name)
                    continue
                seen[cid] = {name}
                out.append(r)
        except Exception as e:
            print(f"    ! {name} 실패: {e}")
    return out, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="특정 질문만. 예: q01,q07")
    args = ap.parse_args()

    qs = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    if args.only:
        keep = {s.strip() for s in args.only.split(",")}
        qs = [x for x in qs if x["id"] in keep]

    old = {}
    if LABELS.exists():
        old = {x["id"]: x for x in (yaml.safe_load(LABELS.read_text(encoding="utf-8")) or [])}

    result = []
    with connect() as conn:
        for item in qs:
            qid, q = item["id"], item["q"]
            print(f"\n[{qid}] {q}")
            cands, sources = pool(conn, q)
            print(f"  후보 {len(cands)}개")
            good = judge(conn, q, cands)
            if len(good) > MAX_EXPECT:
                print(f"  정답 {len(good)}개 → 상한 {MAX_EXPECT}개로 자름"
                      f" (recall 이 구조적으로 눌리는 것을 막기 위해)")
                good = good[:MAX_EXPECT]
            titles = {c["content_id"]: c["title"] for c in cands}
            print(f"  정답 판정 {len(good)}개: " + ", ".join(titles[g] for g in good[:6])
                  + (" …" if len(good) > 6 else ""))
            result.append({
                "id": qid, "q": q, "kind": item.get("kind"),
                "pool_size": len(cands),
                "expect": [{"id": g, "title": titles[g]} for g in good],
            })

    # 기존 파일에 사람이 손댄 흔적(reviewed: true)이 있으면 덮어쓰지 않는다
    merged = []
    for r in result:
        prev = old.get(r["id"])
        if prev and prev.get("reviewed"):
            print(f"  {r['id']}: 검수 완료 표시가 있어 유지")
            merged.append(prev)
        else:
            r["reviewed"] = False
            merged.append(r)

    LABELS.write_text(
        "# 자동 생성 + 사람 검수용.\n"
        "# 눈으로 훑고 틀린 것을 지운 뒤 reviewed: true 로 바꾸세요.\n"
        "# 그러면 build_pool 을 다시 돌려도 그 질문은 덮어쓰지 않습니다.\n\n"
        + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    n = sum(len(r["expect"]) for r in merged)
    print(f"\neval/labels.yaml 에 질문 {len(merged)}개 · 정답 {n}개 저장")
    print("→ 파일 열어서 눈으로 확인하고, 맞으면 reviewed: true 로 바꾸세요.")


if __name__ == "__main__":
    main()
