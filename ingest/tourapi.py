"""TourAPI 호출 공통 모듈.

여기서 신경 쓰는 것 세 가지
  1. 서비스키 인코딩 — 포털이 주는 Encoding 키를 그대로 쓰면 이중 인코딩되어 401 이 난다.
  2. 하루 호출 한도 — 매 호출을 DB(api_calls)에 남기고, KST 자정 기준으로 센다.
  3. 응답 껍데기 — 정상/오류가 같은 200 으로 오는 경우가 있어 resultCode 를 직접 본다.
"""
import time
import urllib.parse
import httpx

from .config import TOUR_API_KEY, TOUR_BASE, TOUR_APP_NAME, TOUR_DAILY_LIMIT
from .db import log_call, tour_used_today, used_today


class QuotaExceeded(RuntimeError):
    pass


class TourApiError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(f"[{code}] {msg}")
        self.code, self.msg = code, msg


def _service_key() -> str:
    """Encoding 키가 들어와도 Decoding 키로 되돌려서 쓴다."""
    key = TOUR_API_KEY.strip()
    if "%" in key:
        key = urllib.parse.unquote(key)
    return key


def call(conn, op: str, params: dict, *, timeout=20.0, retries=3, respect_quota=True,
         service=None, api="tour", limit=None) -> dict:
    """service 를 주면 같은 기관의 다른 서비스(KorPetTourService2, KorWithService2 …)를 부른다.
    그 경우 api 이름(호출 기록·한도 계산용)과 하루 한도(limit)도 따로 준다 — 포털 한도가 서비스마다 따로라서."""
    if not TOUR_API_KEY:
        raise RuntimeError(".env 의 TOUR_API_KEY 가 비어 있습니다.")

    if respect_quota:
        cap = limit if limit is not None else TOUR_DAILY_LIMIT
        used = tour_used_today(conn) if api == "tour" else used_today(conn, api)
        if used >= cap:
            raise QuotaExceeded(f"오늘(KST) {api} 이미 {used}회 호출. 한도 {cap}")

    q = {
        "serviceKey": _service_key(),
        "MobileOS": "ETC",
        "MobileApp": TOUR_APP_NAME,
        "_type": "json",
        **params,
    }
    base = TOUR_BASE if not service else f"{TOUR_BASE.rsplit('/', 1)[0]}/{service}"
    url = f"{base}/{op}"

    last = None
    for attempt in range(retries):
        t0 = time.time()
        try:
            r = httpx.get(url, params=q, timeout=timeout)
            ms = int((time.time() - t0) * 1000)
            body = None
            try:
                body = r.json()
            except Exception:
                # 오류일 때 XML 로 오는 경우가 있다
                log_call(conn, api, op, False, r.status_code, ms, note=r.text[:300])
                raise TourApiError("NON_JSON", r.text[:200])

            header = (body.get("response") or {}).get("header") or {}
            code = str(header.get("resultCode", ""))
            msg = header.get("resultMsg", "")

            # 게이트웨이 오류는 껍데기가 아예 다르다:
            #   {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": ...}}}
            if not code:
                alt = ((body.get("OpenAPI_ServiceResponse") or {}).get("cmmMsgHeader")) or {}
                if alt:
                    code = str(alt.get("returnReasonCode") or alt.get("errMsg") or "GW")
                    msg = f"{alt.get('errMsg','')} / {alt.get('returnAuthMsg','')}".strip(" /")
                else:
                    code, msg = "NO_HEADER", str(body)[:200]

            if code in ("0000", "00", "0"):
                log_call(conn, api, op, True, r.status_code, ms)
                return (body.get("response") or {}).get("body") or {}

            log_call(conn, api, op, False, r.status_code, ms, note=f"{code} {msg}")
            # 한도 초과 계열은 재시도해도 소용없다
            if code in ("22", "0022", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"):
                raise QuotaExceeded(f"API 한도 초과 응답: {code} {msg}")
            raise TourApiError(code, msg)

        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
            log_call(conn, api, op, False, None, int((time.time() - t0) * 1000), note=str(e)[:200])
            time.sleep(1.5 * (attempt + 1))
            continue

    raise TourApiError("NETWORK", str(last))


def items(body: dict) -> list:
    """response.body.items.item 을 항상 리스트로. 결과 0건이면 items 가 빈 문자열로 온다."""
    it = body.get("items")
    if not it or it == "":
        return []
    item = it.get("item") if isinstance(it, dict) else None
    if not item:
        return []
    return item if isinstance(item, list) else [item]
