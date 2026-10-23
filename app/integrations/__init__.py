"""与外部系统对接的代码都放在这里:每个子包要么是一套通用机制,要么是一个业务的对接。

## 现有子包

| 子包 | 类型 | 内容 |
|---|---|---|
| `wms_callback` | 机制 | 接收 WMS 主动发来的回调,不认识任何业务 |
| `wms_request` | 机制 | 向 WMS 的 decisions、facts 端点发请求,目前没有具体 operation |
| `wms_transport` | 业务 | 搬运任务:提交给 WMS,并处理 WMS 回传的位置和结果 |

机制包不使用仓储业务词命名。`inbound` 是入库,`outbound` 是出库,不能拿来表示通信方向。
每个子包的 `__init__.py` docstring 写了它的职责和开发方式,改动前先读那一份。

## 业务包的约定

业务包里这些文件名由框架自动发现,启动时导入,装饰器完成注册:

- `callback/`:入站 operation,由 `wms_callback` 接收。operation 很少时也可以是单个 `callback.py`。
- `request/`:出站 operation,由 `wms_request` 发送。同样可以是单个 `request.py`。
- `dev_callback.py`、`dev.py`:联调样例和联调场景,只在 `ENVIRONMENT=local` 导入。

名字写成目录时,一个 operation 一个文件,文件名是注册名把 `.` 和 `@` 换成 `_`,
有测试检查两者一致。其余文件名没有强制规定,`wms_transport` 可以作参照:协议常量、适配器、
请求构造与校验、入队服务、场景封装各一个文件。

## 新增一个业务包

1. 在这里建 `<业务>/`,`__init__.py` 先写 docstring:范围、对应的合同、调用入口、没有做的部分。
2. 有自己的协议适配器时,在 `app/main.py` 里和现有的一起注册到 `app.state.reliable_adapters`。
3. 在 `RELIABLE_TARGETS` 里配对应协议的目标才会真正发送,默认不发。
4. 测试放在 `tests/integrations/<业务>/`。

## 依赖方向

本目录可以用 `app.core`、`app.models`、`app.reliable`、`app.http_client`,
不能导入 `app.controllers`、`app.services`、`app.schemas`、`fastapi` 和 `celery`
(`tests/architecture` 检查)。HTTP 路由和 Celery 任务留在 `controllers/`、`tasks/`,
再调用这里。
"""
