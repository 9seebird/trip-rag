#!/usr/bin/env bash
# DB 백업을 cron 에 등록한다 (서버에서 한 번만). 다시 돌려도 줄이 겹치지 않는다.
#
#   bash scripts/install_backup_cron.sh            # 매일 04:30 (한국 시각)
#   bash scripts/install_backup_cron.sh 03:10      # 시각 바꾸기
#   bash scripts/install_backup_cron.sh --remove   # 등록 해제
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARK="# trip-rag DB 백업"
CURRENT="$(crontab -l 2>/dev/null | grep -vF "$MARK" || true)"
if [ "${1:-}" = "--remove" ]; then
    printf '%s\n' "$CURRENT" | sed '/^$/d' | crontab -
    echo "해제 완료"; exit 0
fi
AT="${1:-04:30}"
LOCAL="$(date -d "TZ=\"Asia/Seoul\" today $AT" +'%H:%M')"
H="${LOCAL%%:*}"; M="${LOCAL##*:}"; H=$((10#$H)); M=$((10#$M))
LINE="$M $H * * * /usr/bin/env bash $ROOT/scripts/backup_db.sh $MARK"
mkdir -p "$ROOT/logs"; chmod +x "$ROOT/scripts/backup_db.sh"
{ printf '%s\n' "$CURRENT" | sed '/^$/d'; echo "$LINE"; } | crontab -
echo "등록 완료 — 한국 시각 매일 $AT (서버 시각 $LOCAL)"
echo "  $LINE"
echo "바로 한 번:  bash $ROOT/scripts/backup_db.sh && tail -n 20 $ROOT/logs/backup.log"
