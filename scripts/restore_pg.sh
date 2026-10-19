#!/usr/bin/env bash
# 从备份恢复:JWT_ROTATED=yes CONFIRM=yes scripts/restore_pg.sh <备份文件>
# 流程见 docs/deploy.md §4。前提:已停 api/worker/beat,且已在 .env 换了新的 JWT_SECRET_KEY(使恢复前签发的 access/refresh token 全部失效,
# 否则用户 ID 被复用后旧令牌会冒用新用户)。
# 做法:恢复到全新的临时库(单事务,失败整体回滚并丢弃临时库,原库不动);成功后在同一事务里把原库改名为 <库>_old_<时间>、临时库改为原库名
# (要么都成功要么都不变);随后调用 clear_redis_after_restore.sh 清掉 Redis 里的登录会话和待执行任务。确认无误后手动 DROP 旧库。
# PG_EXEC / REDIS_EXEC / RESTORE_TAG 仅供测试覆盖。
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -eq 1 ] && [ -f "$1" ] || { sed -n '2,3p' "$0"; exit 1; }
[ "${CONFIRM:-}" = yes ] || { echo "会替换当前数据库,确认请加 CONFIRM=yes" >&2; exit 1; }
[ "${JWT_ROTATED:-}" = yes ] || { echo "请先在 .env 换新的 JWT_SECRET_KEY,再加 JWT_ROTATED=yes" >&2; exit 1; }
${PG_EXEC:-docker compose exec -T postgres} sh -c '
set -e
new="${POSTGRES_DB}_restore"; old="${POSTGRES_DB}_old_'"${RESTORE_TAG:-\$(date +%Y%m%d%H%M%S)_\$\$}"'"
sql() { psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 "$@"; }
sql -c "DROP DATABASE IF EXISTS \"$new\"" -c "CREATE DATABASE \"$new\""
pg_restore -U "$POSTGRES_USER" -d "$new" --single-transaction --exit-on-error --no-owner \
  || { sql -c "DROP DATABASE IF EXISTS \"$new\""; echo "恢复失败,原库未改动" >&2; exit 1; }
sql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '"'"'$POSTGRES_DB'"'"' AND pid <> pg_backend_pid()"
# 两次改名在一个事务里:失败则都回滚,原库保持原名;可修复原因后直接重跑(原库完好,重跑会重新恢复)
sql -1 -c "ALTER DATABASE \"$POSTGRES_DB\" RENAME TO \"$old\"" -c "ALTER DATABASE \"$new\" RENAME TO \"$POSTGRES_DB\"" \
  || { echo "切换失败,原库未改动,临时库 $new 保留" >&2; exit 1; }
echo "已恢复;旧库保留为 $old"
' < "$1"
# 令牌已因换密钥失效;这里清掉 Redis 里的会话键和恢复前的待执行/未确认任务。失败不重新恢复数据库,只重试清理
if ! scripts/clear_redis_after_restore.sh; then
  echo "数据库已切换,但 Redis 清理未完成:保持 api/worker/beat 停止,排除故障后单独执行 scripts/clear_redis_after_restore.sh,不要重新恢复数据库" >&2
  exit 1
fi
echo "数据库恢复和 Redis 清理已完成;应用仍须保持停止。请选择目标发布目录,按 docs/deploy.md 的固定镜像流程启动并核对新密钥生效;不要使用开发镜像或重新构建。"
