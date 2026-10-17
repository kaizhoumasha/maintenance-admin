#!/usr/bin/env bash
# 备份/恢复脚本的隔离验收:起一个一次性 Postgres 容器(不碰 compose 的数据),需要 docker。用法: scripts/test_backup_restore.sh
set -euo pipefail
cd "$(dirname "$0")/.."
img=$(grep -o 'timescale/timescaledb@sha256:[0-9a-f]*' docker-compose.yml | head -1)
rimg=$(grep -o 'image: redis:[^ ]*' docker-compose.yml | head -1 | cut -d' ' -f2)
name=pgbk-test-$$; rname=$name-redis; tmp=$(mktemp -d)
trap 'docker rm -f "$name" "$rname" >/dev/null 2>&1; rm -rf "$tmp"' EXIT
docker run -d --name "$rname" "$rimg" >/dev/null
docker run -d --name "$name" -e POSTGRES_USER=admin -e POSTGRES_PASSWORD=x -e POSTGRES_DB=app "$img" >/dev/null
until docker exec "$name" pg_isready -U admin -d app >/dev/null 2>&1; do sleep 1; done
sleep 3 # 官方镜像初始化阶段会重启一次,等它稳定
export PG_EXEC="docker exec -i $name" REDIS_EXEC="docker exec -i $rname" BACKUP_DIR="$tmp/b" CONFIRM=yes JWT_ROTATED=yes
q() { docker exec "$name" psql -U admin -d "${2:-app}" -tAc "$1"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# big 让恢复耗时足够长,下面"第二次改名失败"的场景才不会和占位连接抢跑
q "CREATE TABLE t1(id int); INSERT INTO t1 VALUES (1); CREATE TABLE big AS SELECT g FROM generate_series(1,300000) g"
good=$(scripts/backup_pg.sh)
# 0) 备份目录/文件只有当前用户可读
[ "$(ls -ld "$tmp/b" | cut -c1-10)" = drwx------ ] || fail "备份目录权限过宽"
[ "$(ls -l "$good" | cut -c1-10)" = -rw------- ] || fail "备份文件权限过宽"
# 1) 跨版本回退:备份后"升级"新增表、改了数据,恢复旧备份后新表不应残留,且能再次建同名表
q "CREATE TABLE t_new(id int); INSERT INTO t1 VALUES (2)"
scripts/restore_pg.sh "$good" >/dev/null
[ "$(q "SELECT count(*) FROM t1")" = 1 ] || fail "t1 未回到备份时的数据"
[ "$(q "SELECT to_regclass('t_new') IS NULL")" = t ] || fail "新版本的表残留"
q "CREATE TABLE t_new(id int)" || fail "无法再次升级"
# 1b) 没换 JWT 密钥就不许恢复
! JWT_ROTATED= scripts/restore_pg.sh "$good" >/dev/null 2>&1 || fail "未确认换密钥却恢复了"
# 1c) 切换阶段失败(旧库名被占用)时两次改名一起回滚:业务库名和数据都不变
q "INSERT INTO t1 VALUES (9)"
q "CREATE DATABASE app_old_x" postgres
! RESTORE_TAG=x scripts/restore_pg.sh "$good" >/dev/null 2>&1 || fail "改名冲突时应失败"
[ "$(q "SELECT count(*) FROM t1")" = 2 ] || fail "切换失败后业务库被改动"
q "DROP DATABASE app_old_x" postgres
q "DELETE FROM t1 WHERE id=9"
# 1d) 第二次改名失败(临时库被占用):事务回滚,原库名和数据不变,没有 _old 库;排除占用后直接重试成功
q "INSERT INTO t1 VALUES (8)"
olds=$(q "SELECT count(*) FROM pg_database WHERE datname LIKE 'app_old_%'" postgres)
( until docker exec "$name" psql -U admin -d app_restore -c "select pg_sleep(60)" >/dev/null 2>&1; do sleep 0.05; done ) & sq=$!
! scripts/restore_pg.sh "$good" >/dev/null 2>&1 || { kill $sq; fail "占用临时库时应切换失败(时序相关:若偶发请重跑)"; }
kill $sq 2>/dev/null || true
q "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='app_restore'" postgres >/dev/null
[ "$(q "SELECT count(*) FROM t1")" = 2 ] || fail "第二次改名失败后业务库被改动"
[ "$(q "SELECT count(*) FROM pg_database WHERE datname LIKE 'app_old_%'" postgres)" = "$olds" ] || fail "回滚后仍多出旧库"
scripts/restore_pg.sh "$good" >/dev/null || fail "排除占用后重试失败"
[ "$(q "SELECT count(*) FROM t1")" = 1 ] || fail "重试后数据不对"
# 1e) Redis:会话键、队列(含未确认消息)被清,其它键保留;Redis 故障时恢复报告未完成,清理可单独重试
r() { docker exec "$rname" redis-cli "$@" >/dev/null; }
seed() { r SET refresh:a 1; r SADD user_refresh:1 a; r LPUSH celery m; r HSET unacked t m; r ZADD unacked_index 1 t; r SET ratelimit:keep 1; }
seed
scripts/restore_pg.sh "$good" >/dev/null || fail "恢复失败"
[ "$(docker exec "$rname" redis-cli EXISTS refresh:a user_refresh:1 celery unacked unacked_index)" = 0 ] || fail "Redis 会话/队列未清"
[ "$(docker exec "$rname" redis-cli EXISTS ratelimit:keep)" = 1 ] || fail "误删其它 Redis 键"
seed
! REDIS_EXEC="docker exec -i no-such-redis" scripts/restore_pg.sh "$good" >/dev/null 2>&1 || fail "Redis 清理失败却报告成功"
scripts/clear_redis_after_restore.sh || fail "单独重试清理失败"
[ "$(docker exec "$rname" redis-cli EXISTS refresh:a celery unacked)" = 0 ] || fail "重试后仍有残留"
# 2) 恢复中途失败:损坏的备份不能破坏原库,也不留临时库
q "INSERT INTO t1 VALUES (3)"
head -c "$(( $(wc -c < "$good") / 2 ))" "$good" > "$tmp/bad.dump"
! scripts/restore_pg.sh "$tmp/bad.dump" >/dev/null 2>&1 || fail "损坏备份竟然恢复成功"
[ "$(q "SELECT count(*) FROM t1")" = 2 ] || fail "恢复失败后原库被改动"
[ "$(q "SELECT count(*) FROM pg_database WHERE datname='app_restore'" postgres)" = 0 ] || fail "残留临时库"
# 3) 备份失败不留 .dump
before=$(ls "$tmp"/b/db-*.dump | wc -l)
! PG_EXEC="docker exec -i no-such-container" scripts/backup_pg.sh >/dev/null 2>&1 || fail "备份应当失败"
[ "$(ls "$tmp"/b/db-*.dump | wc -l)" = "$before" ] || fail "失败后留下了 dump"
ls "$tmp"/b/*.tmp >/dev/null 2>&1 && fail "留下了 .tmp"
echo "OK"
