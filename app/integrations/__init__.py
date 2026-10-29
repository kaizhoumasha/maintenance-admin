"""本目录保存通用机制与固定平台能力，领域业务统一放在 app.businesses。

## 现有子包

| 子包 | 类型 | 内容 |
|---|---|---|
| `wms_callback` | 机制 | 接收 WMS 主动发来的回调,不认识任何业务 |
| `wms_request` | 机制 | 向 WMS 的 decisions、facts 端点发请求,目前没有具体 operation |
| `wms_transport` | 固定平台能力 | 搬运任务:提交给 WMS,并处理 WMS 回传的位置和结果 |
| `ecs_status` | 内置能力 | 查询指定 ECS 的设备描述和状态，无登记/存储前提，不是业务注册表 |

机制包不使用仓储业务词命名。`inbound` 是入库,`outbound` 是出库,不能拿来表示通信方向。
每个子包的 `__init__.py` docstring 写了它的职责和开发方式,改动前先读那一份。
跨子包共用的 WMS 合同检查放在本目录下的 `wms_envelope.py`,它不是业务入口。

## 显式入口

领域业务只放在 `app/businesses/<业务>/`，由 `catalog.entries()` 返回 composition，
其 `ENTRY_MODULES` 显式声明 callback、request、ecs_event 及 local 入口。
统一加载器 `app.integrations.discovery` 合并 catalog 与固定平台清单，不扫描 integrations 根目录。
声明的入口可以是模块或包；包内子模块仍逐个导入，一个 operation 一个文件时校验注册名对应文件名。

固定平台正式入口是 `wms_transport.callback`；local 固定加载 ECS/WMS 示例及 Transport 场景。
所有正式入口与合同校验先于 local 入口和运行时资源装配。
正式 catalog 当前登记粗分扫码、WMS 验证及首个 MOVE_FORWARD / Result 阶段，
各环境使用同一入口与配置数据库。

## 依赖方向

本目录可以用 `app.core`、`app.models`、`app.reliable`、`app.http_client`,
不能导入 `app.controllers`、`app.services`、`app.schemas`、`fastapi` 和 `celery`
(`tests/architecture` 检查)。HTTP 路由和 Celery 任务留在 `controllers/`、`tasks/`,
再调用这里。
"""
