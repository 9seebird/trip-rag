#!/usr/bin/env bash
# 매일 수집을 cron 에 등록한다 (서버에서 한 번만). 다시 돌려도 줄이 겹치지 않는다.
#
#   bash scripts/install_cron.sh            # 매일 10:00 (한국 시각)
#   bash scripts/install_cron.sh 13:10      # 시각 바꾸기
#   bash scripts/install_cron.sh --remove   # 등록 해제
#
# 서버 시간대가 한국이 아니어도(UTC 등) 한국 시각 10:00 에 맞춰 환산해서 넣는다.
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARK="# trip-rag 매일 수집"
CURRENT="$(crontab -l 2>/dev/null | grep -vF "$MARK" || true)"

if [ "${1:-}" = "--remove" ]; then
    printf '%s\n' "$CURRENT" | sed '/^$/d' | crontab -
    echo "해제 완료"
    exit 0
fi

AT="${1:-10:00}"

# 한국 시각 AT → 서버 시각 (GNU date)
LOCAL="$(date -d "TZ=\"Asia/Seoul\" today $AT" +'%H:%M')"
H="${LOCAL%%:*}"; M="${LOCAL##*:}"
H=$((10#$H)); M=$((10#$M))

LINE="$M $H * * * /usr/bin/env bash $ROOT/scripts/daily_collect.sh >>$ROOT/logs/cron.log 2>&1 $MARK"

mkdir -p "$ROOT/logs"
chmod +x "$ROOT/scripts/daily_collect.sh"
{ printf '%s\n' "$CURRENT" | sed '/^$/d'; echo "$LINE"; } | crontab -

echo "등록 완료"
echo "  서버 시간대 : $(cat /etc/timezone 2>/dev/null || date +%Z) (지금 $(date '+%F %H:%M'))"
echo "  한국 시각   : 매일 $AT  →  서버 시각 $LOCAL"
echo "  등록된 줄   : $LINE"
echo
docker info >/dev/null 2>&1 || echo "주의: 이 계정으로 docker 를 못 씁니다. sudo usermod -aG docker \$USER 후 다시 로그인하세요."
echo "확인:        crontab -l"
echo "바로 한 번:  bash $ROOT/scripts/daily_collect.sh && tail -n 40 $ROOT/logs/collect_\$(TZ=Asia/Seoul date +%Y%m%d).log"
