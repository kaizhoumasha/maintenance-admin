# Task 1 报告：深页 OFFSET 保护

## 完成内容

- 在 `app/schemas/base.py` 增加 `MAX_OFFSET = 100_000`。
- `PageParams.check_offset` 在现有 64 位范围检查前拒绝超过 `100_000` 的 OFFSET，继续通过现有 Pydantic 到 `INVALID_REQUEST` 的错误路径返回 400。
- 在 `tests/controllers/test_pagination.py` 增加边界覆盖。`page=1001,page_size=100` 的 OFFSET 为 `100_000`，请求成功。`page=1002,page_size=100` 的 OFFSET 为 `100_100`，请求返回 400。
- 保留原有 64 位边界参数。由于新的业务最大偏移边界优先于 64 位范围检查，原来接近 64 位上限但超过 `MAX_OFFSET` 的两条请求期望更新为 400。真正超过 64 位范围的用例仍保留并返回 400。

## TDD 验证

1. 先加入边界测试并运行 `uv run pytest -q tests/controllers/test_pagination.py::test_pagination_offset_bounds`。新增超限用例按预期失败，接口返回 200。
2. 加入最小生产 guard 后运行分页控制器和服务测试，全部通过。

## 验证结果

- `uv run pytest -q tests/controllers/test_pagination.py tests/services/test_pagination.py`
- 结果：49 passed
- `uv run ruff check app/schemas/base.py tests/controllers/test_pagination.py`
- 结果：All checks passed

## 取舍记录

新的 `MAX_OFFSET` 是业务边界。超过该边界的 OFFSET，即使仍处于 64 位整数范围，也统一走 INVALID_REQUEST。未添加特殊放行逻辑，以保持实现与批准计划中的“超过 100_000 即拒绝”一致。

## 未覆盖范围

未运行完整测试套件，也未进行真实 PostgreSQL 查询计划或生产流量验证。此次改动只覆盖分页 schema 和对应回归测试。

## Review round 1 修复

- 将 `PageParams.check_offset` 的 INT64 范围检查恢复到业务 `MAX_OFFSET` 检查之前，确保数据库范围错误仍可独立触发。
- 增加 `PageParams(page=9223372036854775809, page_size=1)` 的直接 schema 回归测试，并断言其继续返回“分页 OFFSET 超出数据库范围”。
- 修复后分页控制器与服务测试共 50 项通过，目标 Ruff 检查通过。
