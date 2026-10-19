#!/usr/bin/env bash
# 数据库恢复后清理 Redis:登录会话(refresh:*、user_refresh:*)和 Celery 队列(celery*,含 kombu 的 unacked/unacked_index 未确认消息)。
# 其它键(如限流窗口)不动。失败即非零退出;可单独重复执行。REDIS_EXEC 仅供测试覆盖。
set -euo pipefail
cd "$(dirname "$0")/.."
${REDIS_EXEC:-docker compose exec -T redis} sh -c '
set -e
f=$(mktemp)
for p in "refresh:*" "user_refresh:*" "celery*"; do
  redis-cli --scan --pattern "$p" > "$f"
  xargs -r redis-cli DEL < "$f" > /dev/null
  left=$(redis-cli --scan --pattern "$p")
  [ -z "$left" ] || { echo "残留键: $p" >&2; exit 1; }
done
redis-cli DEL unacked unacked_index > /dev/null
[ "$(redis-cli EXISTS unacked unacked_index)" = 0 ] || { echo "unacked 残留" >&2; exit 1; }
rm -f "$f"'
