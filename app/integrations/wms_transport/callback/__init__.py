"""Transport 的 WMS 回调 operation(合同 §5):一个 operation 一个文件,启动时整包自动导入。

- `transport_task_member_position_changed_v1.py`、`transport_task_resulted_v1.py`:两个 operation,
  只负责串起流程。文件名是注册名把 `.` 和 `@` 换成 `_`,有测试检查两者一致。
- `base.py`:共同的基类,锁任务行、结束提交消息的发送、记诊断。
- `payloads.py`:各 operation 的 DTO,只做结构校验。
- `apply.py`:按冻结事实判断并应用,规则在这里。

两个 operation 都在请求事务内锁住 `transport_task` 行后直接应用,所有响应回显 `transport_task_id`。
设计见 docs/superpowers/specs/2026-10-22-transport-callback-design.md。
新增 operation:在本目录加一个文件,用 `@callback_operation` 注册,不用改控制器和 `main.py`。
"""
