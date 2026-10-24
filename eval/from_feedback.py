"""사용자 피드백 → 평가 질문·정답 후보.

화면에서 받은 좋아요/별로(feedback 표)를 질문별로 모아, labels.yaml 과 같은 모양의 후보 파일을 만든다.
바로 정답셋에 넣지 않는다 — 한두 사람의 클릭이라 사람이 한 번 훑고 옮겨야 한다.

    docker compose -f docker-compose.prod.yml --profile tools run --rm -T ingest \\
        python -m eval.from_feedback --min-up 1
    → eval/feedback_candidates.yaml

  * expect  : 좋아요가 별로보다 많은 장소 (정답 후보)
  * reject  : 별로가 더 많은 장소 (오답 — 검색이 상위에 올리면 안 되는 곳)
  * 이미 questions.yaml 에 있는 질문은 표시만 한다 (already: true)
"""
import argparse
import pathlib

import yaml

from ingest.db import connect

HERE = pathlib.Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-up", type=int, default=1, help="정답 후보로 둘 최소 순 좋아요 수")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=str(HERE / "feedback_candidates.yaml"))
    a = ap.parse_args()

    known = set()
    qf = HERE / "questions.yaml"
    if qf.exists():
        known = {" ".join(str(x.get("q", "")).split()) for x in (yaml.safe_load(qf.read_text(encoding="utf-8")) or [])}

    with connect() as conn:
        rows = conn.execute("""
            SELECT f.q, f.content_id, p.title, sum(f.vote) net, count(*) n, min(f.rank) best_rank
            FROM feedback f JOIN places p ON p.content_id = f.content_id
            WHERE f.ts > now() - %s::interval
            GROUP BY f.q, f.content_id, p.title
            ORDER BY f.q, net DESC""", (f"{a.days} days",)).fetchall()

    by_q = {}
    for r in rows:
        d = by_q.setdefault(r["q"], {"q": r["q"], "expect": [], "reject": []})
        item = {"id": r["content_id"], "title": r["title"], "votes": int(r["n"]), "shown_rank": r["best_rank"]}
        if r["net"] >= a.min_up:
            d["expect"].append(item)
        elif r["net"] < 0:
            d["reject"].append(item)
    out = []
    for i, (q, d) in enumerate(sorted(by_q.items()), 1):
        if not d["expect"] and not d["reject"]:
            continue
        out.append({"id": f"fb{i:03d}", "q": q, "kind": "feedback", "already": q in known,
                    "expect": d["expect"], "reject": d["reject"], "reviewed": False})

    head = ("# 사용자 피드백에서 뽑은 평가 후보 (eval/from_feedback.py 가 만듦)\n"
            "# 훑어보고 맞는 것만 questions.yaml / labels.yaml 로 옮기세요.\n\n")
    pathlib.Path(a.out).write_text(head + yaml.safe_dump(out, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"질문 {len(out)}개 · 정답 후보 {sum(len(x['expect']) for x in out)}곳 · 오답 {sum(len(x['reject']) for x in out)}곳"
          f" → {a.out}")


if __name__ == "__main__":
    main()
