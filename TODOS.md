# TODOS

## 联调待确认

- [ ] 与 WMS 确认回调接收来源认证方案；当前合同认证为 `NONE`，暂不实施。详见[回调接收设计 §1.2、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 联调前与 WMS 确认 `member_position_changed` 里程碑的先后规则；当前采用本方约定，尚未经对端确认。详见[回调接收设计 §6.3、§9](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。

## 后续开发

- [ ] 制定回调收据与证据的清理策略；本阶段不清理，依赖货架结果应用上线后的容量数据及 WMS 对重发窗口的承诺。详见[回调接收设计 §7](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
- [ ] 联调控制台增加「投递样例回调并查看结果」场景，仅限 `ENVIRONMENT=local`；依赖货架结果应用切片完成。详见[回调接收设计 §1.2、§10](docs/superpowers/specs/2026-10-22-transport-callback-design.md)。
