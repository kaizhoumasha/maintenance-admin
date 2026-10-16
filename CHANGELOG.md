# Changelog

## [Unreleased]

### Added
- 用户、审计和归档列表支持字段白名单筛选、条件组合与严格输入校验。

### Changed
- 回归测试改为验证公开接口契约，减少对内部实现的依赖，并集中覆盖分页参数与 OpenAPI 错误响应。
- 审计和归档列表改用 `limit/cursor`，响应改为 `{items,next_cursor}`；旧 `page/page_size` 返回 400，三个列表均拒绝未知查询参数。
- OpenAPI 统一声明参数校验、认证、权限和限流错误，保留各路由的业务错误，移除不会返回的 422 声明。

### Removed
- 删除不再使用的 `PageQuery` 别名；用户列表继续使用原有分页和总数。
