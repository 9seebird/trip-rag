#!/usr/bin/env bash
# trip-rag 매일 수집 — 서비스 서버(노트북 Ubuntu)에서 cron 이 부른다. 등록은 scripts/install_cron.sh
#
#   1) TourAPI 수집      ingest.daily        (scripts/areas.txt 의 지역, 하루 한도 950회)
#   2) 실내/실외 판정     ingest.enrich       (규칙 → 애매한 것만 Gemini)
#   3) 색인              ingest.build_index  (새로 들어온 곳만 임베딩)
#
# 셋을 이어서 돌려야 새 장소가 다음 날 검색에 나온다. 앞 단계가 실패해도 뒤 단계는 돈다
# (예: TourAPI 가 막혀도 어제 받아 둔 것 중 색인 안 된 게 있으면 색인은 한다).
# 로그: logs/collect_YYYYMMDD.log  (60일 지나면 지운다)
#
# 감시: .env 에 HC_PING_URL(healthchecks.io 체크 주소)을 넣으면 시작·성공·실패를 알린다.
#       하루 안에 성공 신호가 안 오면(서버가 꺼졌거나 cron 이 안 돌면) healthchecks 가 메일을 보낸다.
#
# 손으로 한 번 돌려보기:   bash scripts/daily_collect.sh
# 한도를 바꾸려면:         TOUR_LIMIT=5000 bash scripts/daily_collect.sh   (운영계정 승인 후)
set -u

# cron 은 PATH 가 짧다. docker 가 /usr/bin 이 아닌 곳에 있어도 찾게.
export PATH="${PATH:-/usr/bin:/bin}:/usr/local/bin:/usr/bin:/bin:/snap/bin"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

LIMIT="${TOUR_LIMIT:-950}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
DC=(docker compose -f "$COMPOSE_FILE")

mkdir -p logs
LOG="logs/collect_$(TZ=Asia/Seoul date +%Y%m%d).log"
exec >>"$LOG" 2>&1

log() { echo "[$(TZ=Asia/Seoul date +%H:%M:%S)] $*"; }

# healthchecks.io 핑 — 주소가 없거나 인터넷이 안 돼도 수집은 계속한다
HC="$(grep -E '^HC_PING_URL=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r"' )"
ping_hc() {   # ping_hc start | <종료코드>
    [ -n "$HC" ] || return 0
    curl -fsS -m 10 --retry 3 -o /dev/null --data-binary @<(tail -c 20000 "$LOG" 2>/dev/null) \
        "$HC/$1" || log "  (healthchecks 핑 실패 — 무시)"
}

# 두 번 겹쳐 돌지 않게 (손으로 돌리는 중에 cron 이 돌 때 등)
if command -v flock >/dev/null 2>&1; then
    exec 9>"logs/.collect.lock"
    if ! flock -n 9; then
        log "이미 다른 수집이 돌고 있어 건너뜀"
        exit 0
    fi
fi

# scripts/areas.txt → "39,6,32,..."  (# 주석, 빈 줄, 공백, 줄바꿈·쉼표 섞어 써도 됨)
AREAS="$(grep -v '^[[:space:]]*#' scripts/areas.txt 2>/dev/null \
         | tr '\n' ',' | tr -d ' \t\r' | sed 's/,,*/,/g; s/^,//; s/,$//')"
if [ -z "$AREAS" ]; then
    log "scripts/areas.txt 에 지역이 없어 중단"
    ping_hc 1; exit 1
fi

log "===== 시작 · 지역 $AREAS · 한도 $LIMIT ====="
ping_hc start

if ! docker info >/dev/null 2>&1; then
    log "docker 에 접근할 수 없음 (도커가 꺼졌거나, 이 계정이 docker 그룹이 아님) — 중단"
    ping_hc 1; exit 1
fi

# DB 가 떠 있는지 (평소엔 서비스와 함께 떠 있다)
"${DC[@]}" up -d --wait db || { log "DB 를 띄우지 못해 중단"; ping_hc 1; exit 1; }

run() {
    log "> $*"
    "${DC[@]}" --profile tools run --rm -T -e TOUR_DAILY_LIMIT="$LIMIT" ingest "$@"
    local code=$?
    log "  종료코드 $code"
    return $code
}

fail=0
run python -m ingest.daily --areas "$AREAS" || fail=1
run python -m ingest.enrich                  || fail=1
run python -m ingest.build_index             || fail=1

# 오래된 로그 정리
find logs -name 'collect_*.log' -mtime +60 -delete 2>/dev/null

log "===== 끝 (실패 단계 $([ $fail -eq 0 ] && echo 없음 || echo 있음 — 위 로그 확인)) ====="
ping_hc "$fail"
exit $fail
