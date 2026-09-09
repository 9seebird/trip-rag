"""sql/ 안의 번호 붙은 마이그레이션을 순서대로 적용한다.

init.sql 은 컨테이너가 처음 뜰 때만 실행되므로, 그 뒤의 스키마 변경은 여기로 넣는다.
이미 적용한 것은 schema_migrations 에 남아 건너뛴다.

    docker compose run --rm ingest python -m ingest.migrate
"""
import pathlib

from .db import connect

SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"


def apply(conn, quiet=False):
    """마이그레이션 적용. 이미 적용된 것은 건너뛴다. 반환: 이번에 적용한 파일 이름들."""
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                      name TEXT PRIMARY KEY,
                      applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    applied = []
    for p in sorted(SQL_DIR.glob("[0-9]*.sql")):
        if p.name in done:
            if not quiet:
                print(f"  건너뜀 {p.name}")
            continue
        if not quiet:
            print(f"  적용   {p.name}")
        conn.execute(p.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (p.name,))
        applied.append(p.name)
    return applied


def main():
    with connect() as conn:
        if not list(SQL_DIR.glob("[0-9]*.sql")):
            print("적용할 마이그레이션 없음")
            return
        applied = apply(conn)
        print(f"완료 ({len(applied)}건 적용)")


if __name__ == "__main__":
    main()
