#!/usr/bin/env bash
# 备份 PostgreSQL(单机 Compose):scripts/backup_pg.sh
# 环境变量: BACKUP_DIR(默认 ./backups)、BACKUP_KEEP_DAYS(默认 7)。建议用宿主机 cron 每天执行,并把目录同步到另一台机器。
# PG_EXEC 仅供测试覆盖(默认 docker compose exec -T postgres)。
set -euo pipefail
umask 077 # 备份含密码哈希、邮箱、审计:仅当前用户可读
cd "$(dirname "$0")/.."
dir="${BACKUP_DIR:-./backups}"
mkdir -p "$dir"
chmod 700 "$dir" # 目录已存在时也收紧;历史备份文件需自行检查 chmod 600
out="$dir/db-$(date +%Y%m%d-%H%M%S).dump"
${PG_EXEC:-docker compose exec -T postgres} sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$out.tmp" \
  || { rm -f "$out.tmp"; echo "备份失败" >&2; exit 1; }
mv "$out.tmp" "$out"
find "$dir" -name 'db-*.dump' -mtime +"${BACKUP_KEEP_DAYS:-7}" -delete
echo "$out"
