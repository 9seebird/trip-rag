#!/usr/bin/env bash
# trip-rag DB 백업 — 서버(노트북 Ubuntu)에서 cron 이 매일 부른다. 등록은 scripts/install_backup_cron.sh
#
#   1) db 컨테이너에서 pg_dump (압축된 -Fc 형식, 복원은 pg_restore)
#   2) 파일이 멀쩡한지 pg_restore -l 로 목차를 읽어 확인
#   3) 노트북 안 BACKUP_DIR 에 두고 14일 지난 것은 지운다
#   4) .env 에 BACKUP_NAS_DIR 이 있고 그 폴더가 마운트돼 있으면 NAS 로도 복사 (60일 보관)
#
# 노트북 디스크가 죽어도 남도록 NAS 복사까지 해 두는 게 목적이다.
# 감시: .env 에 HC_BACKUP_URL(healthchecks.io 체크 주소)을 넣으면 성공·실패를 알린다.
#
# 손으로 한 번:   bash scripts/backup_db.sh && tail -n 20 logs/backup.log
# 복원:           docker compose -f docker-compose.prod.yml exec -T db \
#                     pg_restore -U triprag -d triprag --clean --if-exists < 백업파일.dump
set -u
export PATH="${PATH:-/usr/bin:/bin}:/usr/local/bin:/usr/bin:/bin:/snap/bin"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
DC=(docker compose -f "$COMPOSE_FILE")

# .env 값 읽기 (따옴표·줄 끝 # 설명·앞뒤 공백 제거)
envv() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r"' | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//'; }
DB_USER="$(envv POSTGRES_USER)"; DB_USER="${DB_USER:-triprag}"
DB_NAME="$(envv POSTGRES_DB)";   DB_NAME="${DB_NAME:-triprag}"
DIR="${BACKUP_DIR:-$(envv BACKUP_DIR)}"; DIR="${DIR:-$HOME/backups/trip-rag}"
NAS="${BACKUP_NAS_DIR:-$(envv BACKUP_NAS_DIR)}"
HC="$(envv HC_BACKUP_URL)"
KEEP_LOCAL=14; KEEP_NAS=60

mkdir -p logs "$DIR"
exec >>logs/backup.log 2>&1
log() { echo "[$(TZ=Asia/Seoul date '+%F %H:%M:%S')] $*"; }
ping_hc() { [ -n "$HC" ] && curl -fsS -m 10 --retry 3 -o /dev/null "$HC${1:+/$1}" || true; }
fail() { log "실패: $*"; ping_hc fail; exit 1; }

exec 9>"$DIR/.lock"; flock -n 9 || { log "다른 백업이 돌고 있어 건너뜀"; exit 0; }

NAME="triprag_$(TZ=Asia/Seoul date +%Y%m%d_%H%M).dump"
TMP="$DIR/.$NAME.part"
log "시작 → $DIR/$NAME"
"${DC[@]}" exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc >"$TMP" || { rm -f "$TMP"; fail "pg_dump"; }
[ -s "$TMP" ] || { rm -f "$TMP"; fail "빈 파일"; }
"${DC[@]}" exec -T db pg_restore -l <"$TMP" >/dev/null || { rm -f "$TMP"; fail "pg_restore -l 로 읽히지 않음 (깨진 백업)"; }
mv "$TMP" "$DIR/$NAME"
SIZE="$(du -h "$DIR/$NAME" | cut -f1)"
log "완료 $NAME ($SIZE)"
find "$DIR" -maxdepth 1 -name 'triprag_*.dump' -mtime +$KEEP_LOCAL -print -delete | sed 's/^/  지움 /'

if [ -n "$NAS" ]; then
    # 마운트가 풀려 있으면 노트북 디스크에 그냥 써 버리므로, NAS 폴더가 루트·홈과 다른 마운트인지 본다
    T="$(df --output=target "$NAS" 2>/dev/null | tail -1)"
    if [ -n "$T" ] && [ "$T" != "/" ] && [ "$T" != "$(df --output=target "$HOME" | tail -1)" ] && [ -w "$NAS" ]; then
        if cp "$DIR/$NAME" "$NAS/.$NAME.part" && mv "$NAS/.$NAME.part" "$NAS/$NAME"; then
            log "NAS 복사 → $NAS/$NAME"
            find "$NAS" -maxdepth 1 -name 'triprag_*.dump' -mtime +$KEEP_NAS -print -delete | sed 's/^/  NAS 지움 /'
        else
            rm -f "$NAS/.$NAME.part"; fail "NAS 복사 ($NAS)"
        fi
    else
        fail "NAS 폴더가 마운트돼 있지 않거나 쓸 수 없음: $NAS"
    fi
else
    log "BACKUP_NAS_DIR 이 비어 있어 NAS 복사는 건너뜀 (노트북에만 보관)"
fi
ping_hc
