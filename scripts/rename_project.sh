#!/usr/bin/env bash
# 从模板新建项目后改名(只需运行一次,之后可删除本脚本):
#   scripts/rename_project.sh <项目名> <数据库名>     例: scripts/rename_project.sh demo-admin demo
# 替换 pyproject/配置默认值/compose/CI/文档里的项目名和数据库名,并刷新 uv.lock。
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -eq 2 ] || { sed -n '2,4p' "$0"; exit 1; }
export NAME="$1" DB="$2"
perl -pi -e '
  s/enterprise-backend/$ENV{NAME}/g;
  s/enterprise_backend_test/$ENV{DB}_test/g;
  s/\benterprise_backend\b/$ENV{DB}/g;
' pyproject.toml app/core/config.py docker-compose.yml .env.example .github/workflows/ci.yml \
  README.md AGENTS.md docs/deploy.md .serena/project.yml \
  tests/controllers/test_filtering_postgres.py tests/scripts/test_release_bundle.py
uv lock
