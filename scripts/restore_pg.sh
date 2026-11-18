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
# 自定义格式的目录可直接读取 stdin。先识别扩展，再单独把原备份送给恢复进程。
toc=$(${PG_EXEC:-docker compose exec -T postgres} pg_restore --list < "$1") \
  || { echo "无法读取备份目录，原库未改动" >&2; exit 1; }
timescale=no
if grep -Eq "^[0-9]+; [0-9]+ [0-9]+ EXTENSION - timescaledb([[:space:]]|$)" <<< "$toc"; then
  timescale=yes
fi
tag="${RESTORE_TAG:-$(date +%Y%m%d%H%M%S)_$$}"
maintenance_sql() {
  ${PG_EXEC:-docker compose exec -T postgres} sh -c '
    export LC_ALL=C
    old="${POSTGRES_DB}_old_$1"; stage=$2; shift 2
    if [ "${#old}" -gt 63 ]; then
      echo "数据库备份或恢复名称超过 PostgreSQL 标识符长度" >&2
      exit 1
    fi
    exec psql -XqAt -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
      -v "db=$POSTGRES_DB" -v "stage=$stage" -v "old=$old" "$@"
  ' sh "$tag" "${stage:-}" "$@"
}

# TimescaleDB 调度器使用 PostgreSQL 初始化角色（OID 10）；角色设置优先于库设置。
check_restore_settings() {
  [ "$timescale" = no ] || maintenance_sql <<'SQL'
SELECT $error$DO $$ BEGIN
    RAISE EXCEPTION '角色级 timescaledb.restoring 会覆盖恢复流程，请先检查 PostgreSQL 登录角色或初始化角色的设置';
END $$;$error$
WHERE EXISTS (
    SELECT 1 FROM pg_db_role_setting AS setting
    CROSS JOIN LATERAL unnest(setting.setconfig) AS config
    WHERE setting.setrole IN (10, session_user::regrole::oid)
      AND setting.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = :'db'))
      AND lower(split_part(config, '=', 1)) = 'timescaledb.restoring'
)
\gexec
SQL
}
check_restore_settings

# 在同一 PostgreSQL 上按目标库互斥，连接断开或服务重启也保留在场的恢复锁。
if ! guard_identity=$(maintenance_sql -v "timescale=$timescale" <<'SQL'
BEGIN;
SELECT 'restore_guard_' || md5(:'db') AS guard,
    '_restore_' || replace(gen_random_uuid()::text, '-', '') AS stage,
    oid AS original_oid
FROM pg_database WHERE datname = :'db'
\gset
SELECT jsonb_build_object('target', :'db', 'original_oid', :original_oid,
    'old', :'old', 'stage', :'stage', 'timescale', :'timescale' = 'yes')::text AS description
\gset
CREATE SCHEMA :"guard";
COMMENT ON SCHEMA :"guard" IS :'description';
SELECT oid, :'stage', :original_oid FROM pg_namespace WHERE nspname = :'guard';
COMMIT;
SQL
); then
  echo "无法取得恢复锁（已有恢复或数据库不可用），原库未改动" >&2
  exit 1
fi
IFS='|' read -r guard_oid stage original_oid <<< "$guard_identity"
keep_guard=yes
release_guard() {
  local status=$?
  if [ "$keep_guard" = yes ]; then
    echo "保留恢复锁（OID ${guard_oid}，原库 OID ${original_oid}，临时库 ${stage}，OID ${stage_oid:-未记录}），请按 docs/deploy.md 核对并收尾" >&2
    exit "$status"
  fi
  if ! maintenance_sql -v "guard_oid=$guard_oid" <<'SQL'
SELECT nspname AS guard FROM pg_namespace
WHERE nspname = 'restore_guard_' || md5(:'db') AND oid = :'guard_oid'::oid
\gset
DROP SCHEMA :"guard";
SQL
  then
    echo "无法释放恢复锁，请按 docs/deploy.md 核对本次恢复状态" >&2
    exit 1
  fi
  exit "$status"
}
trap release_guard EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

stage_oid=$(maintenance_sql -v "timescale=$timescale" -v "guard_oid=$guard_oid" <<'SQL'
CREATE DATABASE :"stage" WITH TEMPLATE template0 ALLOW_CONNECTIONS false;
BEGIN;
SELECT oid AS stage_oid FROM pg_database WHERE datname = :'stage'
\gset
SELECT nspname AS guard, (obj_description(oid, 'pg_namespace')::jsonb
    || jsonb_build_object('stage_oid', :stage_oid))::text AS description
FROM pg_namespace WHERE nspname = 'restore_guard_' || md5(:'db') AND oid = :'guard_oid'::oid
\gset
COMMENT ON SCHEMA :"guard" IS :'description';
COMMIT;
BEGIN;
REVOKE ALL ON DATABASE :"stage" FROM PUBLIC;
\if :timescale
ALTER DATABASE :"stage" SET timescaledb.restoring TO on;
\endif
ALTER DATABASE :"stage" ALLOW_CONNECTIONS true;
COMMIT;
\echo :stage_oid
SQL
)
[ -n "$stage_oid" ] || exit 1
if ! ${PG_EXEC:-docker compose exec -T postgres} sh -c '
  set -e
  stage=$2
  sql() { psql -Xq -U "$POSTGRES_USER" -d "$stage" -v ON_ERROR_STOP=1 "$@"; }
  if [ "$1" = yes ]; then
    sql <<"SQL"
DO $$ BEGIN
    IF current_setting($key$timescaledb.restoring$key$, true) IS DISTINCT FROM $value$on$value$ THEN
        RAISE EXCEPTION $message$临时库连接未启用 TimescaleDB 恢复模式$message$;
    END IF;
END $$;
CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT public.timescaledb_pre_restore();
SQL
  fi
  pg_restore -U "$POSTGRES_USER" -d "$stage" --single-transaction --exit-on-error --no-owner
  if [ "$1" = yes ]; then
    # 保留 TimescaleDB 恢复模式到切换后，同时提前执行 post_restore 的目录版本校验。
    sql <<"SQL"
DO $$
DECLARE restored_version text;
BEGIN
    SELECT metadata.value INTO restored_version
    FROM pg_catalog.pg_extension AS extension
    JOIN _timescaledb_catalog.metadata AS metadata ON metadata.key = $key$timescaledb_version$key$
    WHERE extension.extname = $key$timescaledb$key$ AND extension.extversion <> metadata.value;
    IF FOUND THEN
        RAISE EXCEPTION $message$恢复的 TimescaleDB 目录版本与目标扩展版本不匹配：%$message$,
            restored_version;
    END IF;
END $$;
SQL
  fi
' sh "$timescale" "$stage" < "$1"; then
  maintenance_sql -v "stage_oid=$stage_oid" <<'SQL'
SELECT format('DROP DATABASE %I', datname) FROM pg_database
WHERE datname = :'stage' AND oid = :'stage_oid'::oid
\gexec
SQL
  keep_guard=no
  echo "恢复失败，原库未改动" >&2
  exit 1
fi

# 原库仍存活时复制现场连接策略，校验后在同一事务里交换库名。
check_restore_settings
swap_reported=yes
maintenance_sql -v "timescale=$timescale" -v "guard_oid=$guard_oid" \
    -v "original_oid=$original_oid" -v "stage_oid=$stage_oid" -o /dev/null -f - <<'SQL' || swap_reported=no
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT pg_advisory_xact_lock(:'guard_oid'::bigint);
SELECT $error$DO $$ BEGIN
    RAISE EXCEPTION '恢复锁、原库或临时库身份发生改变，无法切换';
END $$;$error$
WHERE NOT EXISTS (
    SELECT 1 FROM pg_namespace WHERE nspname = 'restore_guard_' || md5(:'db') AND oid = :'guard_oid'::oid
) OR NOT EXISTS (
    SELECT 1 FROM pg_database WHERE datname = :'db' AND oid = :'original_oid'::oid
) OR NOT EXISTS (
    SELECT 1 FROM pg_database WHERE datname = :'stage' AND oid = :'stage_oid'::oid
)
\gexec

SELECT pg_terminate_backend(pid) FROM pg_stat_activity
WHERE datname = :'db' AND pid <> pg_backend_pid();

SELECT pg_get_userbyid(datdba) AS original_owner, datconnlimit AS connection_limit,
    CASE WHEN datallowconn THEN 'true' ELSE 'false' END AS original_allow_connections
FROM pg_database WHERE datname = :'db'
\gset
ALTER DATABASE :"stage" OWNER TO :"original_owner";
ALTER DATABASE :"stage" CONNECTION LIMIT :connection_limit;
ALTER DATABASE :"stage" ALLOW_CONNECTIONS :original_allow_connections;
REVOKE ALL ON DATABASE :"stage" FROM :"original_owner";

-- 先临时补足原转授权人的直接授权选项，保留原授权人；同一事务中按原 ACL 撤回临时权限。
WITH permissions AS (
    SELECT source.datdba AS owner_id, permission.*
    FROM pg_database AS source
    CROSS JOIN LATERAL aclexplode(coalesce(source.datacl, acldefault('d', source.datdba)))
        AS permission
    WHERE source.datname = :'db'
), temporary_grants AS (
    SELECT DISTINCT p.owner_id, p.grantor, p.privilege_type,
        direct.grantee IS NOT NULL AS keep_privilege
    FROM permissions AS p
    LEFT JOIN permissions AS direct ON direct.grantor = p.owner_id
        AND direct.grantee = p.grantor AND direct.privilege_type = p.privilege_type
    WHERE p.grantor <> p.owner_id AND NOT coalesce(direct.is_grantable, false)
), commands(step, sql) AS (
    SELECT CASE WHEN grantor = owner_id THEN 0 ELSE 2 END, format(
        'SET LOCAL ROLE %I; GRANT %s ON DATABASE %I TO %s%s; RESET ROLE;',
        pg_get_userbyid(grantor), privilege_type, :'stage',
        CASE WHEN grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(grantee)) END,
        CASE WHEN is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
    )
    FROM permissions
    UNION ALL
    SELECT 1, format('SET LOCAL ROLE %I; GRANT %s ON DATABASE %I TO %I WITH GRANT OPTION; RESET ROLE;',
        pg_get_userbyid(owner_id), privilege_type, :'stage', pg_get_userbyid(grantor))
    FROM temporary_grants
    UNION ALL
    SELECT 3, format('SET LOCAL ROLE %I; REVOKE %s%s ON DATABASE %I FROM %I RESTRICT; RESET ROLE;',
        pg_get_userbyid(owner_id), CASE WHEN keep_privilege THEN 'GRANT OPTION FOR ' ELSE '' END,
        privilege_type, :'stage', pg_get_userbyid(grantor))
    FROM temporary_grants
)
SELECT sql FROM commands ORDER BY step
\gexec

WITH settings AS (
    SELECT CASE WHEN setting.setrole = 0 THEN format('ALTER DATABASE %I', :'stage')
                ELSE format('ALTER ROLE %I IN DATABASE %I',
                            pg_get_userbyid(setting.setrole), :'stage')
           END AS subject,
           split_part(config, '=', 1) AS name,
           substr(config, strpos(config, '=') + 1) AS value
    FROM pg_database AS source
    JOIN pg_db_role_setting AS setting ON setting.setdatabase = source.oid
    CROSS JOIN LATERAL unnest(setting.setconfig) AS config
    WHERE source.datname = :'db'
      AND NOT (:'timescale' = 'yes' AND setting.setrole = 0
               AND lower(split_part(config, '=', 1)) = 'timescaledb.restoring')
)
SELECT CASE WHEN name IN ('search_path', 'temp_tablespaces', 'local_preload_libraries',
                         'session_preload_libraries')
    THEN format(
        'SELECT set_config(%L, %L, true); %s SET %I FROM CURRENT; RESET %I;',
        name, value, subject, name, name
    )
    ELSE format('%s SET %I TO %L;', subject, name, value)
END
FROM settings
\gexec

WITH metadata AS (
    SELECT datname, datdba, datconnlimit, datallowconn,
        ARRAY(SELECT permission::text
              FROM aclexplode(coalesce(db.datacl, acldefault('d', db.datdba))) AS permission
              ORDER BY permission::text) AS permissions,
        ARRAY(SELECT jsonb_build_array(setting.setrole, config)::text
              FROM pg_db_role_setting AS setting
              CROSS JOIN LATERAL unnest(setting.setconfig) AS config
              WHERE setting.setdatabase = db.oid
                AND NOT (:'timescale' = 'yes' AND setting.setrole = 0
                         AND lower(split_part(config, '=', 1)) = 'timescaledb.restoring')
              ORDER BY 1) AS settings
    FROM pg_database AS db
    WHERE datname IN (:'db', :'stage')
)
SELECT $error$DO $$ BEGIN RAISE EXCEPTION '恢复库的数据库级权限或设置与原库不一致'; END $$;$error$
WHERE EXISTS (
    SELECT datdba, datconnlimit, datallowconn, permissions, settings FROM metadata WHERE datname = :'db'
    EXCEPT
    SELECT datdba, datconnlimit, datallowconn, permissions, settings FROM metadata WHERE datname = :'stage'
)
\gexec

ALTER DATABASE :"db" RENAME TO :"old";
ALTER DATABASE :"stage" RENAME TO :"db";
COMMIT;
SQL

# 客户端断开后切换仍可能提交。持锁核对身份和解除失败切换的恢复锁，防止迟到的切换越过核对。
swap_result=$(maintenance_sql -v "guard_oid=$guard_oid" -v "original_oid=$original_oid" -v "stage_oid=$stage_oid" <<'SQL'
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT pg_try_advisory_xact_lock(:'guard_oid'::bigint) AS swap_idle
\gset
\if :swap_idle
SELECT CASE WHEN target.oid = :'stage_oid'::oid AND old.oid = :'original_oid'::oid
            THEN target.datallowconn::text || ' ' || :'old'
            WHEN target.oid = :'original_oid'::oid THEN 'unchanged'
            ELSE 'unknown'
       END AS outcome
FROM pg_database AS target
LEFT JOIN pg_database AS old ON old.datname = :'old'
WHERE target.datname = :'db' AND EXISTS (
    SELECT 1 FROM pg_namespace WHERE nspname = 'restore_guard_' || md5(:'db') AND oid = :'guard_oid'::oid
)
\gset
SELECT format('DROP SCHEMA %I', nspname) FROM pg_namespace
WHERE nspname = 'restore_guard_' || md5(:'db') AND oid = :'guard_oid'::oid AND :'outcome' = 'unchanged'
\gexec
COMMIT;
\echo :outcome
\else
ROLLBACK;
\endif
SQL
) || swap_result=
case "$swap_result" in
  true\ *|false\ *)
    if [ "$swap_reported" = no ]; then
      echo "已核对库身份，数据库切换已提交，继续恢复收尾" >&2
    fi
    ;;
  unchanged)
    trap - EXIT
    echo "切换失败，原库保持原名（OID ${original_oid}），临时库 ${stage}（OID ${stage_oid}）保留供核对" >&2
    exit 1
    ;;
  *) echo "无法确认切换状态" >&2; exit 1 ;;
esac
original_allow_connections=${swap_result%% *}
timescale_ready=yes
if [ "$timescale" = yes ] && [ "$original_allow_connections" = true ] && ! ${PG_EXEC:-docker compose exec -T postgres} sh -c '
    exec psql -Xq -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v "stage_oid=$1"
  ' sh "$stage_oid" <<'SQL'
SELECT $error$DO $$ BEGIN
    RAISE EXCEPTION '目标库身份发生改变，无法结束恢复模式';
END $$;$error$
WHERE NOT EXISTS (
    SELECT 1 FROM pg_database WHERE datname = current_database() AND oid = :'stage_oid'::oid
)
\gexec
SELECT public.timescaledb_post_restore();
SQL
then
  timescale_ready=no
  echo "数据库已切换，TimescaleDB 恢复收尾失败（预期目标库 OID ${stage_oid}），请按 docs/deploy.md 核对后单独收尾" >&2
fi
echo "已切换数据库，旧库保留为 ${swap_result#* }"
# 清掉 Redis 会话和恢复前的待执行/未确认任务。保留恢复锁直至收尾完成。
if ! scripts/clear_redis_after_restore.sh; then
  echo "数据库已切换,但 Redis 清理未完成:保持 api/worker/beat 停止,排除故障后单独执行 scripts/clear_redis_after_restore.sh,不要重新恢复数据库" >&2
  exit 1
fi
[ "$timescale_ready" = yes ] || exit 1
if [ "$original_allow_connections" = false ]; then
  if [ "$timescale" = yes ]; then
    echo "数据库已切换，Redis 清理已完成。确认可以开放目标库后，按 docs/deploy.md 完成 TimescaleDB 收尾并解除恢复锁。"
    exit 0
  fi
  echo "数据库恢复和 Redis 清理已完成；目标库仍停用连接。确认可以开放后，按 docs/deploy.md 开放连接并启动应用。"
else
  echo "数据库恢复和 Redis 清理已完成;应用仍须保持停止。请选择目标发布目录,按 docs/deploy.md 的固定镜像流程启动并核对新密钥生效;不要使用开发镜像或重新构建。"
fi
keep_guard=no
