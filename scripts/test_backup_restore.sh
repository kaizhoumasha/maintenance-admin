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
guard_field() {
  q "SELECT obj_description(oid, 'pg_namespace')::jsonb ->> '$1'
     FROM pg_namespace WHERE nspname = 'restore_guard_' || md5('app')" postgres
}
drop_guard() { q "DROP SCHEMA restore_guard_$(q "SELECT md5('app')" postgres)" postgres >/dev/null; }
stage_names() { q "SELECT datname FROM pg_database WHERE left(datname, 9) = '_restore_' ORDER BY 1" postgres; }
wait_for_file() {
  local attempt
  for ((attempt = 0; attempt < 200; attempt++)); do
    [ ! -e "$1" ] || return 0
    sleep 0.05
  done
  fail "等待超时：$1"
}
restart_postgres() {
  local attempt
  docker restart "$name" >/dev/null
  for ((attempt = 0; attempt < 200; attempt++)); do
    docker exec "$name" pg_isready -U admin -d app >/dev/null 2>&1 && return 0
    sleep 0.05
  done
  fail '重启后的 PostgreSQL 未就绪'
}
export -f q fail guard_field wait_for_file restart_postgres
export name tmp RESTORE_TEST_PG_EXEC="$PG_EXEC" RESTORE_TEST_REDIS_EXEC="$REDIS_EXEC"

cat > "$tmp/pause-restore" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "$0" in
  */pause-redis) next=$RESTORE_TEST_REDIS_EXEC ;;
  */pause-restore)
    next=$RESTORE_TEST_PG_EXEC
    [[ ${*: -2} == '-f -' ]] || exec $next "$@" ;;
esac
touch "$0.ready"
wait_for_file "$0.continue"
exec $next "$@"
SH
cp "$tmp/pause-restore" "$tmp/pause-redis"
chmod +x "$tmp/pause-restore" "$tmp/pause-redis"

# big 让截断的备份仍有可读目录和大量未读数据。
q "CREATE TABLE t1(id int); INSERT INTO t1 VALUES (1); CREATE TABLE big AS SELECT g FROM generate_series(1,300000) g"
# 原转授权限改由另一个角色提供后，旧 ACL 条目的顺序仍不变。
q "CREATE ROLE withheld LOGIN; CREATE ROLE reporter LOGIN; CREATE ROLE delegated LOGIN; CREATE ROLE alternate;
REVOKE CONNECT ON DATABASE app FROM PUBLIC; GRANT CONNECT ON DATABASE app TO reporter WITH GRANT OPTION;
SET ROLE reporter; GRANT CONNECT ON DATABASE app TO delegated; RESET ROLE;
GRANT CONNECT ON DATABASE app TO alternate WITH GRANT OPTION;
SET ROLE alternate; GRANT CONNECT ON DATABASE app TO reporter WITH GRANT OPTION; RESET ROLE;
REVOKE GRANT OPTION FOR CONNECT ON DATABASE app FROM reporter CASCADE;
GRANT TEMPORARY ON DATABASE app TO reporter WITH GRANT OPTION;
SET ROLE reporter; GRANT TEMPORARY ON DATABASE app TO delegated; RESET ROLE;
GRANT TEMPORARY ON DATABASE app TO alternate WITH GRANT OPTION;
GRANT alternate TO reporter;
REVOKE TEMPORARY ON DATABASE app FROM reporter CASCADE;
ALTER DATABASE app CONNECTION LIMIT 42;
ALTER DATABASE app SET statement_timeout TO '42s';
ALTER ROLE reporter IN DATABASE app SET search_path TO audit, public"
good=$(scripts/backup_pg.sh)
# 0) 备份目录/文件只有当前用户可读
[ "$(ls -ld "$tmp/b" | cut -c1-10)" = drwx------ ] || fail "备份目录权限过宽"
[ "$(ls -l "$good" | cut -c1-10)" = -rw------- ] || fail "备份文件权限过宽"
# 1) 跨版本回退:备份后"升级"新增表、改了数据,恢复旧备份后新表不应残留,且能再次建同名表
q 'CREATE DATABASE app_restore' postgres
q 'CREATE TABLE peer_probe(id int); INSERT INTO peer_probe VALUES (77)' app_restore
peer_oid=$(q "SELECT oid FROM pg_database WHERE datname = 'app_restore'" postgres)
q "CREATE TABLE t_new(id int); INSERT INTO t1 VALUES (2)"
scripts/restore_pg.sh "$good" >/dev/null
[ "$(q "SELECT oid FROM pg_database WHERE datname = 'app_restore'" postgres)" = "$peer_oid" ] || fail '恢复替换了独立数据库 app_restore'
[ "$(q 'SELECT id FROM peer_probe' app_restore)" = 77 ] || fail '恢复改动了独立数据库 app_restore 的数据'
[ "$(q "SELECT count(*) FROM t1")" = 1 ] || fail "t1 未回到备份时的数据"
[ "$(q "SELECT has_database_privilege('withheld', 'app', 'CONNECT')")" = f ] || fail "恢复后放宽了 CONNECT 权限"
[ "$(q "SELECT has_database_privilege('reporter', 'app', 'CONNECT')")" = t ] || fail "恢复后丢失了 CONNECT 授权"
[ "$(q "SELECT count(*) FROM pg_database,
    LATERAL aclexplode(datacl) WHERE datname = 'app'
    AND grantor = 'reporter'::regrole AND grantee = 'delegated'::regrole
    AND privilege_type IN ('CONNECT', 'TEMPORARY')")" = 2 ] || fail "恢复后丢失了授权来源"
[ "$(q "SELECT datconnlimit FROM pg_database WHERE datname = 'app'")" = 42 ] || fail "恢复后丢失了连接数限制"
[ "$(q "SHOW statement_timeout")" = 42s ] || fail "恢复后丢失了数据库会话设置"
[ "$(docker exec "$name" psql -U reporter -d app -tAc "SHOW search_path")" = "audit, public" ] || fail "恢复后丢失了用户在该库的设置"
[ "$(q "SELECT to_regclass('t_new') IS NULL")" = t ] || fail "新版本的表残留"
q "CREATE TABLE t_new(id int)" || fail "无法再次升级"
# 不同备份的恢复相互排斥，直到数据库切换和 Redis 清理全部完成。
q 'UPDATE t1 SET id = 2'
second_backup=$(scripts/backup_pg.sh)
PG_EXEC="$tmp/pause-restore" REDIS_EXEC="$tmp/pause-redis" RESTORE_TAG=first scripts/restore_pg.sh "$good" >"$tmp/first.log" 2>&1 &
first_restore=$!
wait_for_file "$tmp/pause-restore.ready"
stage=$(guard_field stage)
stage_oid=$(q "SELECT oid FROM pg_database WHERE datname = '$stage'" postgres)
[ -n "$stage_oid" ] || fail '找不到首次恢复的临时库'
if RESTORE_TAG=second scripts/restore_pg.sh "$second_backup" >"$tmp/second.log" 2>&1; then
  touch "$tmp/pause-restore.continue" "$tmp/pause-redis.continue"
  wait "$first_restore" || cat "$tmp/first.log"
  fail "并发恢复在首次切换前成功，两份备份可交叉切换"
fi
grep -q '已有恢复' "$tmp/second.log" || { cat "$tmp/second.log"; fail '并发恢复未被明确拒绝'; }
[ "$(q "SELECT oid FROM pg_database WHERE datname = '$stage'" postgres)" = "$stage_oid" ] || fail '并发恢复替换了临时库'
touch "$tmp/pause-restore.continue"
wait_for_file "$tmp/pause-redis.ready"
! RESTORE_TAG=second scripts/restore_pg.sh "$second_backup" >"$tmp/second.log" 2>&1 \
  || fail 'Redis 清理未完成时并发恢复仍能切换'
grep -q '已有恢复' "$tmp/second.log" || { cat "$tmp/second.log"; fail 'Redis 清理期间的并发恢复未被明确拒绝'; }
touch "$tmp/pause-redis.continue"
wait "$first_restore" || { cat "$tmp/first.log"; fail '首次恢复失败'; }
[ "$(q 'SELECT id FROM t1')" = 1 ] || fail '首次恢复装入了其他备份的数据'
RESTORE_TAG=second scripts/restore_pg.sh "$second_backup" >/dev/null
[ "$(q 'SELECT id FROM t1')" = 2 ] || fail '首次恢复结束后，第二份备份无法恢复'
q 'UPDATE t1 SET id = 1'
# 临时库名称在切换前被重新使用时，恢复只能拒绝，数据库身份保持原状。
rm "$tmp/pause-restore.ready" "$tmp/pause-restore.continue"
PG_EXEC="$tmp/pause-restore" RESTORE_TAG=replaced_stage scripts/restore_pg.sh "$good" >"$tmp/replaced-stage.log" 2>&1 &
replaced_restore=$!
wait_for_file "$tmp/pause-restore.ready"
stage=$(guard_field stage)
original_oid=$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)
q "ALTER DATABASE $stage RENAME TO app_restore_replaced" postgres
q "CREATE DATABASE $stage" postgres
replacement_oid=$(q "SELECT oid FROM pg_database WHERE datname = '$stage'" postgres)
touch "$tmp/pause-restore.continue"
! wait "$replaced_restore" || { cat "$tmp/replaced-stage.log"; fail '临时库被替换后仍成功切换'; }
grep -q '身份发生改变' "$tmp/replaced-stage.log" || { cat "$tmp/replaced-stage.log"; fail '未明确拒绝临时库替换'; }
[ "$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)" = "$original_oid" ] || fail '临时库被替换后原库也被切换'
[ "$(q "SELECT oid FROM pg_database WHERE datname = '$stage'" postgres)" = "$replacement_oid" ] || fail '切换失败时删除了占用同名的库'
q "DROP DATABASE $stage" postgres
q 'DROP DATABASE app_restore_replaced' postgres
# 切换事务已提交但客户端未收到结果时，按原库和临时库的身份确认，再做收尾。
cat > "$tmp/lose-swap-result" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${*: -2} == '-f -' ]]; then
  $RESTORE_TEST_PG_EXEC "$@" >/dev/null
  if [ "${RESTORE_TEST_INTERRUPT:-no}" = yes ]; then
    kill -TERM "$RESTORE_TEST_PID"
  fi
  exit 71
fi
exec $RESTORE_TEST_PG_EXEC "$@"
SH
chmod +x "$tmp/lose-swap-result"
PG_EXEC="$tmp/lose-swap-result" RESTORE_TAG=lost_swap_result scripts/restore_pg.sh "$second_backup" >"$tmp/lost-swap-result.log" 2>&1 \
  || { cat "$tmp/lost-swap-result.log"; fail '切换结果丢失后无法确认已提交并收尾'; }
[ "$(q 'SELECT id FROM t1')" = 2 ] || fail '切换结果丢失后数据不正确'
[ "$(q 'SHOW timescaledb.restoring')" = off ] || fail '切换结果丢失后未完成 TimescaleDB 收尾'
q 'UPDATE t1 SET id = 1'
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
rm "$tmp/pause-restore.ready" "$tmp/pause-restore.continue"
PG_EXEC="$tmp/pause-restore" scripts/restore_pg.sh "$good" >"$tmp/occupied-stage.log" 2>&1 &
busy_restore=$!
wait_for_file "$tmp/pause-restore.ready"
stage=$(guard_field stage)
docker exec -e PGAPPNAME=restore_fixture "$name" psql -U admin -d "$stage" -c 'SELECT pg_sleep(60)' >/dev/null 2>&1 &
busy_client=$!
for ((attempt = 0; attempt < 200; attempt++)); do
  [ "$(q "SELECT count(*) FROM pg_stat_activity WHERE datname='$stage' AND application_name='restore_fixture'" postgres)" = 0 ] || break
  sleep 0.05
done
[ "$attempt" -lt 200 ] || fail '占用临时库的 PostgreSQL 连接未建立'
touch "$tmp/pause-restore.continue"
! wait "$busy_restore" || fail '占用临时库时应切换失败'
q "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='restore_fixture'" postgres >/dev/null
wait "$busy_client" || true
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
# 客户端退出时，等待同步复制的 COMMIT 仍可能在服务端完成。
cat > "$tmp/lose-swap-client" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[[ ${*: -2} == '-f -' ]] || exec $RESTORE_TEST_PG_EXEC "$@"
awk '{print} /^BEGIN/ {print "SET LOCAL synchronous_commit TO remote_apply;"}' > "$tmp/sync-wait-swap.sql"
docker exec -e PGAPPNAME=restore_sync_wait -i "$name" "$@" < "$tmp/sync-wait-swap.sql" &
client=$!
for ((attempt = 0; attempt < 200; attempt++)); do
  pid=$(q "SELECT pid FROM pg_stat_activity WHERE application_name = 'restore_sync_wait' AND wait_event = 'SyncRep'" postgres)
  if [ -n "$pid" ]; then
    echo "$pid" > "$tmp/sync-wait-pid"
    kill -KILL "$client"
    wait "$client" || true
    exit 71
  fi
  sleep 0.05
done
fail '切换提交未进入同步复制等待'
SH
chmod +x "$tmp/lose-swap-client"
seed
q "ALTER SYSTEM SET synchronous_commit TO local" postgres
q "ALTER SYSTEM SET synchronous_standby_names TO 'FIRST 1 (restore_missing_replica)'" postgres
q 'SELECT pg_reload_conf()' postgres
for ((attempt = 0; attempt < 200; attempt++)); do
  [ "$(q 'SHOW synchronous_commit' postgres)" = local ] &&
    [ "$(q 'SHOW synchronous_standby_names' postgres)" = 'FIRST 1 (restore_missing_replica)' ] && break
  sleep 0.05
done
[ "$attempt" -lt 200 ] || fail '同步复制配置未加载'
original_oid=$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)
! PG_EXEC="$tmp/lose-swap-client" RESTORE_TAG=sync_wait scripts/restore_pg.sh "$second_backup" >"$tmp/sync-wait.log" 2>&1 \
  || fail '切换事务仍在等待同步复制时报告成功'
[ -s "$tmp/sync-wait-pid" ] || { cat "$tmp/sync-wait.log"; fail '未在服务端提交等待期间断开客户端'; }
swap_pid=$(cat "$tmp/sync-wait-pid")
[ "$(q "SELECT wait_event FROM pg_stat_activity WHERE pid = $swap_pid" postgres)" = SyncRep ] \
  || fail '断开客户端后服务端切换事务已退出'
[ "$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)" = "$original_oid" ] \
  || fail '同步复制未确认时无法观察到原库 OID'
installed_oid=$(guard_field stage_oid)
[ -n "$installed_oid" ] \
  || { cat "$tmp/sync-wait.log"; fail '同步复制等待期间未保留临时库身份'; }
! RESTORE_TAG=sync_wait_contender scripts/restore_pg.sh "$good" >"$tmp/sync-wait-contender.log" 2>&1 \
  || fail '切换事务尚在等待时其他恢复成功'
grep -q '已有恢复' "$tmp/sync-wait-contender.log" \
  || { cat "$tmp/sync-wait.log" "$tmp/sync-wait-contender.log"; fail '客户端断开后其他恢复未被恢复锁阻止'; }
[ "$(q "SELECT pg_cancel_backend($swap_pid)" postgres)" = t ] || fail '无法结束切换事务的同步复制等待'
for ((attempt = 0; attempt < 200; attempt++)); do
  [ "$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)" = "$installed_oid" ] && break
  sleep 0.05
done
[ "$attempt" -lt 200 ] || fail '结束同步复制等待后未观察到切换提交'
[ "$(q "SELECT oid FROM pg_database WHERE datname = 'app_old_sync_wait'" postgres)" = "$original_oid" ] \
  || fail '切换提交后的旧库身份不正确'
[ "$(guard_field stage_oid)" = "$installed_oid" ] || fail '切换提交后丢失恢复锁'
[ "$(q 'SELECT id FROM t1')" = 2 ] || fail '切换提交后未读到已恢复的数据'
[ "$(q 'SHOW timescaledb.restoring')" = on ] || fail '切换确认前结束了 TimescaleDB 恢复模式'
[ "$(docker exec "$rname" redis-cli EXISTS refresh:a celery unacked)" = 3 ] || fail '切换确认前清理了 Redis'
q 'ALTER SYSTEM RESET synchronous_standby_names' postgres
q 'ALTER SYSTEM RESET synchronous_commit' postgres
q 'SELECT pg_reload_conf()' postgres
q 'SELECT public.timescaledb_post_restore()' >/dev/null
scripts/clear_redis_after_restore.sh
drop_guard
# 真实 COMMIT 和退出信号同时发生时，锁必须保护已切换、尚未收尾的数据库。
seed
original_oid=$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)
interrupted_status=0
RESTORE_TEST_INTERRUPT=yes PG_EXEC="$tmp/lose-swap-result" RESTORE_TAG=interrupted \
  bash -c 'export RESTORE_TEST_PID=$$; exec scripts/restore_pg.sh "$1"' _ "$second_backup" \
    >"$tmp/interrupted.log" 2>&1 || interrupted_status=$?
[ "$interrupted_status" = 143 ] || { cat "$tmp/interrupted.log"; fail '未在切换提交后收到 SIGTERM'; }
[ "$(q "SELECT oid FROM pg_database WHERE datname = 'app_old_interrupted'" postgres)" = "$original_oid" ] \
  || fail 'SIGTERM 前未实际提交切换事务'
[ "$(q 'SELECT id FROM t1')" = 2 ] || fail 'SIGTERM 后未读到已恢复的数据'
[ "$(q 'SHOW timescaledb.restoring')" = on ] || fail 'SIGTERM 未发生在 TimescaleDB 收尾前'
[ "$(docker exec "$rname" redis-cli EXISTS refresh:a celery unacked)" = 3 ] || fail 'SIGTERM 未发生在 Redis 清理前'
[ "$(q "SELECT count(*) FROM pg_namespace WHERE nspname = 'restore_guard_' || md5('app')" postgres)" = 1 ] \
  || fail 'SIGTERM 后丢失未完成恢复的锁'
[ "$(guard_field original_oid)" = "$original_oid" ] || fail '中断后恢复锁未记录原库身份'
[ "$(guard_field old)" = app_old_interrupted ] || fail '中断后恢复锁未记录旧库名称'
installed_oid=$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)
[ "$(guard_field stage_oid)" = "$installed_oid" ] || fail '中断后恢复锁未记录已安装库的身份'
! RESTORE_TAG=after_interrupt scripts/restore_pg.sh "$good" >"$tmp/after-interrupt.log" 2>&1 \
  || fail 'SIGTERM 后其他恢复覆盖了尚未收尾的目标库'
grep -q '已有恢复' "$tmp/after-interrupt.log" || { cat "$tmp/after-interrupt.log"; fail '中断恢复未明确阻止其他恢复'; }
[ "$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)" = "$installed_oid" ] || fail '后续恢复改变了已安装库'
q 'SELECT public.timescaledb_post_restore()' >/dev/null
scripts/clear_redis_after_restore.sh
drop_guard
seed
! REDIS_EXEC="docker exec -i no-such-redis" scripts/restore_pg.sh "$good" >/dev/null 2>&1 || fail "Redis 清理失败却报告成功"
! scripts/restore_pg.sh "$second_backup" >"$tmp/redis-contender.log" 2>&1 || fail 'Redis 清理失败后允许其他恢复覆盖目标库'
grep -q '已有恢复' "$tmp/redis-contender.log" || { cat "$tmp/redis-contender.log"; fail 'Redis 清理失败后未保留恢复锁'; }
scripts/clear_redis_after_restore.sh || fail "单独重试清理失败"
[ "$(docker exec "$rname" redis-cli EXISTS refresh:a celery unacked)" = 0 ] || fail "重试后仍有残留"
drop_guard
# 2) 恢复中途失败:损坏的备份不能破坏原库,也不留临时库
q "INSERT INTO t1 VALUES (3)"
before_stages=$(stage_names)
head -c "$(( $(wc -c < "$good") / 2 ))" "$good" > "$tmp/bad.dump"
! scripts/restore_pg.sh "$tmp/bad.dump" >/dev/null 2>&1 || fail "损坏备份竟然恢复成功"
[ "$(q "SELECT count(*) FROM t1")" = 2 ] || fail "恢复失败后原库被改动"
[ "$(stage_names)" = "$before_stages" ] || fail "残留临时库"
# 3) 备份失败不留 .dump
before=$(find "$tmp"/b -type f | wc -l)
! PG_EXEC="docker exec -i no-such-container" scripts/backup_pg.sh >/dev/null 2>&1 || fail "备份应当失败"
[ "$(find "$tmp"/b -type f | wc -l)" = "$before" ] || fail "失败后留下了备份文件"
# 4) TimescaleDB 时序数据与后台任务恢复
q 'SELECT _timescaledb_functions.stop_background_workers()' >/dev/null
q "CREATE TABLE restore_job_probe(marker int);
INSERT INTO restore_job_probe VALUES (1);
CREATE PROCEDURE delete_restore_probe(job_id int, config jsonb) LANGUAGE SQL AS \$\$
    DELETE FROM restore_job_probe;
\$\$;
SELECT add_job('delete_restore_probe', INTERVAL '1 hour', fixed_schedule => false)" >/dev/null
probe_backup=$(scripts/backup_pg.sh)
q "CREATE TABLE measurements(ts timestamptz NOT NULL, reading int);
SELECT create_hypertable('measurements', by_range('ts'));
INSERT INTO measurements VALUES ('2026-01-01T00:00Z', 1), ('2026-01-01T01:00Z', 2)" >/dev/null
q "CREATE MATERIALIZED VIEW hourly WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', ts) AS bucket, sum(reading) AS total
FROM measurements GROUP BY bucket" >/dev/null
wait_for_probe_delete() {
  local attempt
  for ((attempt = 0; attempt < 100; attempt++)); do
    [ "$(q 'SELECT count(*) FROM restore_job_probe' "$1")" = 0 ] && return 0
    sleep 0.1
  done
  return 1
}
export -f wait_for_probe_delete
cat > "$tmp/before-swap" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
# 暂停实际改名入口，让数据库调度器在仍是临时库时有机会执行任务。
if [[ ${*: -2} == '-f -' ]]; then
  stage=$(guard_field stage)
  if [ "${RESTORE_TEST_RESTART:-no}" = yes ]; then
    touch "$tmp/restarted-at-swap"
    restart_postgres
    if [ -n "${RESTORE_TEST_CONTENDER:-}" ]; then
      ! PG_EXEC="$RESTORE_TEST_PG_EXEC" RESTORE_TAG=restart_contender scripts/restore_pg.sh "$RESTORE_TEST_CONTENDER" >"$tmp/restart-contender.log" 2>&1 \
        || fail '重启使其它恢复可提前切换'
      grep -q '已有恢复' "$tmp/restart-contender.log" || { cat "$tmp/restart-contender.log"; fail '重启后失去恢复锁'; }
    fi
  fi
  [ "$(q "SELECT scheduled FROM timescaledb_information.jobs WHERE proc_name = 'delete_restore_probe'" "$stage")" = t ] \
    || fail '临时库缺少可运行的 TimescaleDB 删除任务'
  if [ "${RESTORE_TEST_RESTART:-no}" = yes ] && wait_for_probe_delete "$stage"; then
    fail 'TimescaleDB 后台任务在切换前删除了临时库的数据'
  fi
  [ "$(q 'SHOW timescaledb.restoring' "$stage")" = on ] || fail '临时库在切换前已退出 TimescaleDB 恢复模式'
  [ "$(q 'SELECT count(*) FROM restore_job_probe' "$stage")" = 1 ] || fail '临时库数据在切换前被删除'
fi
exec $RESTORE_TEST_PG_EXEC "$@"
SH
chmod +x "$tmp/before-swap"
series_backup=$(scripts/backup_pg.sh)
q "UPDATE _timescaledb_catalog.metadata SET value = '0.0.0' WHERE key = 'timescaledb_version'" >/dev/null
mismatch_backup=$(scripts/backup_pg.sh)
q "UPDATE _timescaledb_catalog.metadata SET value = (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb')
   WHERE key = 'timescaledb_version'" >/dev/null
original_oid=$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)
before_stages=$(stage_names)
q 'ALTER ROLE admin SET timescaledb.restoring TO off' postgres
! RESTORE_TEST_RESTART=yes PG_EXEC="$tmp/before-swap" RESTORE_TAG=role_override scripts/restore_pg.sh "$probe_backup" >"$tmp/role-override.log" 2>&1 \
  || fail '角色设置覆盖 TimescaleDB 恢复模式时仍切换了数据库'
grep -q '角色级 timescaledb.restoring' "$tmp/role-override.log" || { cat "$tmp/role-override.log"; fail '未提前报告角色设置覆盖恢复模式'; }
[ ! -e "$tmp/restarted-at-swap" ] || fail '角色恢复设置到切换阶段才检查'
[ "$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)" = "$original_oid" ] || fail '角色恢复设置冲突后原库被替换'
[ "$(stage_names)" = "$before_stages" ] || fail '角色恢复设置冲突后已创建临时库'
q 'ALTER ROLE admin RESET timescaledb.restoring' postgres
! RESTORE_TAG=bad_catalog scripts/restore_pg.sh "$mismatch_backup" >"$tmp/bad-catalog.log" 2>&1 \
  || fail 'TimescaleDB 目录版本不匹配时仍切换了数据库'
grep -q 'TimescaleDB 目录版本与目标扩展版本不匹配' "$tmp/bad-catalog.log" || { cat "$tmp/bad-catalog.log"; fail '未报告 TimescaleDB 目录版本不匹配'; }
[ "$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)" = "$original_oid" ] || fail '目录版本不匹配后原库被替换'
[ "$(stage_names)" = "$before_stages" ] || fail '目录版本不匹配后临时库仍存在'
q "DROP MATERIALIZED VIEW hourly; DROP TABLE measurements" >/dev/null
RESTORE_TEST_RESTART=yes RESTORE_TEST_CONTENDER="$probe_backup" PG_EXEC="$tmp/before-swap" scripts/restore_pg.sh "$series_backup" >/dev/null \
  || fail "无法在重启后恢复 TimescaleDB"
[ -e "$tmp/restarted-at-swap" ] || fail '未实际执行切换前重启'
[ "$(q "SELECT count(*),sum(reading) FROM measurements")" = "2|3" ] || fail "时序数据恢复不完整"
[ "$(q "SELECT count(*),sum(total) FROM hourly")" = "2|3" ] || fail "连续聚合恢复不完整"
wait_for_probe_delete app || fail '切换到开放的目标库后后台任务没有运行'
# 5) 原库停用连接时，恢复后的库继续停用；切换和 Redis 清理仍应完成。
q "INSERT INTO measurements VALUES ('2026-01-01T02:00Z', 3)" >/dev/null
q "ALTER DATABASE app ALLOW_CONNECTIONS false" postgres
offline_oid=$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)
q "CREATE DATABASE app_old_offline_conflict" postgres
! RESTORE_TAG=offline_conflict scripts/restore_pg.sh "$series_backup" >/dev/null 2>&1 || fail "停用连接时改名冲突应失败"
[ "$(q "SELECT oid,datallowconn FROM pg_database WHERE datname = 'app'" postgres)" = "$offline_oid|f" ] || fail "切换失败后原库被替换或重新开放"
seed
PG_EXEC="$tmp/before-swap" RESTORE_TAG=offline scripts/restore_pg.sh "$series_backup" >/dev/null || fail "无法恢复已停用连接的 TimescaleDB"
[ "$(q "SELECT datallowconn FROM pg_database WHERE datname = 'app'" postgres)" = f ] || fail "恢复后放开了原库的连接限制"
[ "$(q "SELECT oid,datallowconn FROM pg_database WHERE datname = 'app_old_offline'" postgres)" = "$offline_oid|f" ] || fail "保留的旧库身份或连接状态有误"
! q 'SELECT 1' >/dev/null 2>&1 || fail "停用连接的库仍可连接"
[ "$(docker exec "$rname" redis-cli EXISTS refresh:a user_refresh:1 celery unacked unacked_index)" = 0 ] || fail "恢复停用的库后 Redis 未清"
[ "$(guard_field stage_oid)" = "$(q "SELECT oid FROM pg_database WHERE datname = 'app'" postgres)" ] || fail '停用的 TimescaleDB 未完成收尾就清除了恢复锁'
q "ALTER DATABASE app ALLOW_CONNECTIONS true" postgres
[ "$(q "SELECT count(*),sum(reading) FROM measurements")" = "2|3" ] || fail "重新开放后时序数据不正确"
[ "$(q 'SELECT count(*) FROM restore_job_probe')" = 1 ] || fail '后台任务在手动结束恢复前删除了数据'
q 'SELECT public.timescaledb_post_restore()' >/dev/null
wait_for_probe_delete app || fail '手动结束恢复后后台任务没有运行'
drop_guard
[ "$(q "SELECT oid FROM pg_database WHERE datname = 'app_restore'" postgres)" = "$peer_oid" ] || fail '多次恢复改动了独立数据库 app_restore'
[ "$(q 'SELECT id FROM peer_probe' app_restore)" = 77 ] || fail '多次恢复改动了独立数据库 app_restore 的数据'
echo "OK"
