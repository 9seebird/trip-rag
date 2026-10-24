import psycopg
from psycopg.rows import dict_row
from .config import DB

def connect():
    return psycopg.connect(**DB, row_factory=dict_row, autocommit=True)

def log_call(conn, api, op, ok, status=None, ms=None, tokens=None, cost=None, note=None):
    conn.execute(
        "INSERT INTO api_calls (api, op, ok, status, ms, tokens, cost_krw, note)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (api, op, ok, status, ms, tokens, cost, note),
    )

def tour_used_today(conn) -> int:
    return conn.execute("SELECT used FROM tour_quota_today").fetchone()["used"]


def used_today(conn, api) -> int:
    """api 이름별 오늘(KST) 호출 수. 공공데이터포털은 API(서비스)마다 하루 한도가 따로다."""
    return conn.execute(
        """SELECT count(*) c FROM api_calls WHERE api = %s
           AND (ts AT TIME ZONE 'Asia/Seoul')::date = (now() AT TIME ZONE 'Asia/Seoul')::date""",
        (api,)).fetchone()["c"]
