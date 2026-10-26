# 第一个可靠投递实验

本实验在内存 SQLite 中保存一条业务记录和一条出站消息，用 `ScriptedHttpClient` 返回 ACK，再验证消息进入 `DONE`。它不连接 ECS/WMS，不需要 PostgreSQL、Redis 或 `.env`，也不启动应用发送器。配置与事务边界见[可靠投递参考](integrations/reliable-delivery.md)，正式 Transport 协议见[Transport 参考](integrations/wms-transport.md)。

## 1. 准备文件与命令

在仓库根目录运行 `uv sync --frozen`，确认 `uv run python --version` 为 Python 3.13 或更新版本。开发依赖包含 pytest、pytest-asyncio 和 aiosqlite。

在业务项目创建第一个文件 `tests/reliable/test_transport_submit_example.py`，复制下面整段 Python。若文件已存在，先核对内容，不要覆盖其他人的测试。这里的业务模型只存在于测试内存库，不需要生产迁移。

这个文件包含可供拆分的请求构建、ACK 分类、业务模型、事务写入和测试。它是一个离线学习切片，不是生产适配器：只使用带合法任务 ID 的 `RACK_MOVE` 请求；JSON 数值一律只接收整数（含冗余字段）。正式适配器已在 `app/integrations/wms_transport/adapter.py`，对冗余字段中的浮点数更宽松，且有请求侧 DTO/位置校验和更完整的测试；差异清单见[Transport 参考](integrations/wms-transport.md)的 ACK 与合同边界。

```python
import json
import socket
from datetime import timedelta

import pytest
from sqlalchemy import LargeBinary, String, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from app.database import create_database_engine
from app.http_client import DeliveryState, HttpRequest, HttpResult, validate_request
from app.models import ReliableMessage
from app.reliable import FailReason, MessageDraft, Outbox, Verdict, VerdictKind
from app.reliable.config import TargetConfig
from app.reliable.sender import Sender
from app.reliable.store import MessageStore
from app.reliable.testing import DeterministicRandom, FakeClock, ScriptedHttpClient

OPERATION = "transport.task.submit@v1"
OPERATION_ID = "01a13b00-cabd-7390-890a-0c093e7c07a1"
TASK_ID = "transport-9af2751b-4d01-4203-88a7-649bbf69e810"
REQUEST_RAW = json.dumps(
    {
        "operation_id": OPERATION_ID,
        "operation": OPERATION,
        "timestamp": 1791991270077,
        "data": {
            "transport_task_id": TASK_ID,
            "kind": "RACK_MOVE",
            "rack_id": "510004",
            "rcs_template_id": "CTU01",
            "source": {"kind": "RACK", "location_code": "510004"},
            "target": {"kind": "RACK_POSITION", "location_code": "KT16"},
            "target_face": "90",
        },
    },
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")
REJECTION_REASONS = {
    "INVALID_ENVELOPE",
    "UNSUPPORTED_OPERATION",
    "INVALID_DATA",
    "COORDINATED_BIN_EXCHANGE_UNSUPPORTED",
    # 合同原文字面量，不要修改。
    "There are other unfinished tasks on the shelf任务下发失败："
    "There are other unfinished tasks on the shelf",
}


class LabBase(DeclarativeBase):
    pass


class BusinessTransport(LabBase):
    __tablename__ = "example_business_transport"
    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    transport_task_id: Mapped[str] = mapped_column(String, unique=True)
    request_raw: Mapped[bytes] = mapped_column(LargeBinary)


def reject_json_number(value):
    raise ValueError("this learning example accepts integer JSON numbers only")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class TransportSubmitAdapter:
    def build_request(self, message, target):
        payload = json.loads(message.request_raw)
        if (
            message.operation != OPERATION
            or payload["operation"] != OPERATION
            or payload["operation_id"] != message.operation_id
        ):
            raise ValueError("submit identity mismatch")
        return HttpRequest(
            method="POST",
            path="/api/v1/wes/transport-requests",
            headers=(("accept", "application/json"), ("content-type", "application/json")),
            body=message.request_raw,
        )

    def classify_response(self, message, result):
        unknown = Verdict(VerdictKind.UNKNOWN)
        if (
            result.delivery_state != DeliveryState.HTTP_RESPONSE_COMPLETE
            or result.failure_kind is not None
        ):
            return unknown
        if result.status_code in (400, 413) and result.body == b"":
            return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.REJECTED)
        if not isinstance(result.body, bytes):
            return unknown
        try:
            ack = json.loads(
                result.body.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_float=reject_json_number,
                parse_constant=reject_json_number,
            )
        except (UnicodeError, ValueError):
            return unknown
        if not isinstance(ack, dict):
            return unknown
        timestamp = ack.get("timestamp")
        data = ack.get("data")
        expected_task = json.loads(message.request_raw)["data"]["transport_task_id"]
        if (
            ack.get("operation_id") != message.operation_id
            or type(timestamp) is not int
            or not 0 <= timestamp <= 2**63 - 1
            or not isinstance(data, dict)
            or data.get("transport_task_id") != expected_task
        ):
            return unknown
        pair = (result.status_code, ack.get("code"))
        if pair in ((202, "RECEIVED"), (200, "DUPLICATE")):
            return Verdict(VerdictKind.DELIVERED, response=result.body)
        if pair == (409, "CONFLICT"):
            return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.CONFLICT)
        if pair == (422, "REJECTED"):
            reason = data.get("reason_code")
            if isinstance(reason, str) and reason in REJECTION_REASONS:
                return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.REJECTED)
            return unknown
        if pair == (503, "UNAVAILABLE"):
            return Verdict(VerdictKind.NOT_RECEIVED)
        return unknown


def ack_result(
    status, code, *, operation_id=OPERATION_ID, task_id=TASK_ID, timestamp=1791992148964, **extra
):
    data = {} if task_id is None else {"transport_task_id": task_id}
    data.update(extra)
    raw = json.dumps(
        {"operation_id": operation_id, "code": code, "timestamp": timestamp, "data": data},
        ensure_ascii=False,
    ).encode("utf-8")
    return HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        status,
        (("content-type", "application/json"),),
        raw,
    )


def message():
    from app.reliable import OutgoingMessage

    return OutgoingMessage(
        "wms", OPERATION, OPERATION_ID, REQUEST_RAW, None, None, FakeClock()(), None
    )


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("this example must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "create_connection", fail)


@pytest.mark.parametrize(
    "response,kind,reason",
    [
        (ack_result(202, "RECEIVED"), "DELIVERED", None),
        (ack_result(200, "DUPLICATE"), "DELIVERED", None),
        (ack_result(409, "CONFLICT"), "PEER_TERMINAL", "CONFLICT"),
        (ack_result(422, "REJECTED", reason_code="INVALID_DATA"), "PEER_TERMINAL", "REJECTED"),
        (ack_result(503, "UNAVAILABLE"), "NOT_RECEIVED", None),
        (ack_result(200, "RECEIVED"), "UNKNOWN", None),
        (ack_result(202, "NEW_CODE"), "UNKNOWN", None),
        (ack_result(429, "BUSY"), "UNKNOWN", None),
        (ack_result(502, "UNAVAILABLE"), "UNKNOWN", None),
        (ack_result(202, "RECEIVED", operation_id="wrong-id"), "UNKNOWN", None),
        (ack_result(202, "RECEIVED", task_id="another-task"), "UNKNOWN", None),
        (ack_result(422, "REJECTED", reason_code="UNCONFIRMED_REASON"), "UNKNOWN", None),
        *[
            (
                HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, status, (), b""),
                "PEER_TERMINAL",
                "REJECTED",
            )
            for status in (400, 413)
        ],
        (
            HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), b"<html/>"),
            "UNKNOWN",
            None,
        ),
        (HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), b"[]"), "UNKNOWN", None),
        (
            HttpResult(
                DeliveryState.HTTP_RESPONSE_COMPLETE,
                None,
                202,
                (),
                b'{"operation_id":"a","operation_id":"b"}',
            ),
            "UNKNOWN",
            None,
        ),
        (HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), b"\xff"), "UNKNOWN", None),
        (ack_result(422, "REJECTED", reason_code=[]), "UNKNOWN", None),
        (ack_result(202, "RECEIVED", task_id=None), "UNKNOWN", None),
        (ack_result(202, "RECEIVED", timestamp=True), "UNKNOWN", None),
    ],
)
def test_ack_classification(response, kind, reason):
    verdict = TransportSubmitAdapter().classify_response(message(), response)
    assert verdict.kind == kind
    assert verdict.reason == reason
    assert verdict.response == (response.body if kind == "DELIVERED" else None)


@pytest.fixture
async def storage():
    engine = create_database_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, hide_parameters=True
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(LabBase.metadata.create_all)
            await connection.run_sync(ReliableMessage.__table__.create)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def outbox_setup(storage):
    clock = FakeClock()
    target = TargetConfig("wms", "http://example.test", "wms-transport")
    targets = {"wms": target}
    adapters = {"wms-transport": TransportSubmitAdapter()}
    return clock, targets, adapters, Outbox(targets, adapters, clock=clock)


async def save_and_enqueue(storage, outbox):
    async with storage() as session, session.begin():
        session.add(
            BusinessTransport(
                operation_id=OPERATION_ID, transport_task_id=TASK_ID, request_raw=REQUEST_RAW
            )
        )
        return await outbox.enqueue(
            session,
            MessageDraft("wms", OPERATION, OPERATION_ID, REQUEST_RAW, ttl=timedelta(minutes=5)),
        )


async def test_committed_message_is_delivered(storage):
    clock, targets, adapters, outbox = outbox_setup(storage)
    reference = await save_and_enqueue(storage, outbox)
    async with storage() as session, session.begin():
        duplicate = await outbox.enqueue(
            session,
            MessageDraft("wms", OPERATION, OPERATION_ID, REQUEST_RAW, ttl=timedelta(minutes=5)),
        )
        assert duplicate.message_id == reference.message_id
    response = ack_result(202, "RECEIVED")
    client = ScriptedHttpClient(response)
    request = adapters["wms-transport"].build_request(message(), targets["wms"])
    validate_request(targets["wms"].http_config(), request)
    assert client.requests == []
    store = MessageStore(storage, clock=clock)
    sender = Sender(
        store, targets, adapters, {"wms": client}, clock=clock, uniform=DeterministicRandom()
    )
    try:
        assert await sender.tick() == 1
        await sender.aclose()
        async with storage() as session:
            business = await session.get(BusinessTransport, OPERATION_ID)
            row = await session.get(ReliableMessage, reference.message_id)
            assert business.request_raw == REQUEST_RAW
            assert row.status == "DONE"
            assert row.attempts == 1
            assert row.response_raw == response.body
            assert row.lease_id is None
            assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
        assert client.closed
        assert len(client.requests) == 1
        assert client.requests[0].body == REQUEST_RAW
    finally:
        await sender.aclose()


async def test_rollback_keeps_neither_business_record_nor_message(storage):
    _, _, _, outbox = outbox_setup(storage)
    with pytest.raises(RuntimeError, match="business rejected"):
        async with storage() as session, session.begin():
            session.add(
                BusinessTransport(
                    operation_id=OPERATION_ID, transport_task_id=TASK_ID, request_raw=REQUEST_RAW
                )
            )
            await outbox.enqueue(session, MessageDraft("wms", OPERATION, OPERATION_ID, REQUEST_RAW))
            raise RuntimeError("business rejected")
    async with storage() as session:
        assert await session.scalar(select(func.count()).select_from(BusinessTransport)) == 0
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0
```

运行时覆盖环境变量，只影响这一个测试进程；`--noconftest` 不加载仓库其他 fixture，确保实验自给自足：

```bash
ENVIRONMENT=local \
DATABASE_URL=sqlite+aiosqlite:// \
REDIS_URL=redis://127.0.0.1:1/0 \
JWT_SECRET_KEY=example-test-secret-not-for-production \
uv run python -m pytest --noconftest -q tests/reliable/test_transport_submit_example.py
```

## 2. 检查结果

预期为 `23 passed`。成功用例实际经过 `Outbox.enqueue → Sender.tick → MessageStore.claim → ScriptedHttpClient.send → classify_response → 数据库 DONE`。回滚用例证明业务记录和出站消息共同回滚。网络保护 fixture 会在尝试打开连接时使测试失败。

可靠层表带项目 schema（`enterprise_backend`），所以实验必须用 `create_database_engine` 建引擎：它在 SQLite 下去掉 schema 前缀、在 PostgreSQL 下设置 `search_path`；直接用 `create_async_engine` 建表会报 `unknown database enterprise_backend`。

`FakeClock` 固定持久化时间，`DeterministicRandom` 固定退避随机输入。`Sender.aclose()` 等待本次在途任务并关闭假客户端；内存库由 fixture 释放。测试读取 `ReliableMessage` 和装配 `Sender/MessageStore` 是基础设施测试做法，生产业务不能照搬这些内部依赖。

## 3. 实验与正式实现的对应关系

Transport 的正式实现已经落在仓库里，实验里的各部分对应如下：

| 实验中的部分 | 正式位置 | 说明 |
|---|---|---|
| `TransportSubmitAdapter` | `app/integrations/wms_transport/adapter.py`，测试 `tests/integrations/wms_transport/test_adapter.py` | 同步纯函数，不导入数据库或网络客户端；已按协议名 `wms-transport` 在 `app/main.py` 注册 |
| `REQUEST_RAW` 与请求构造 | `wire.py`、`types.py` | 由 `TransportService` 在冻结正文前校验并生成 |
| `BusinessTransport` 与 `save_and_enqueue` | `app/models/transport.py` 的 `TransportTask`、`service.py` 的 `TransportService` | 同一事务保存任务并 `enqueue`；不要照搬 `BusinessTransport` 测试表 |
| 应用入口与目标配置 | `RELIABLE_TARGETS` | 默认为空，不发任何请求；是否启用真实目标按[Transport 参考](integrations/wms-transport.md)中的业务结果消费出口及合同确认项决定 |

实验仍是学习用的自给自足切片，不会随正式实现自动更新。新增其他协议时，才需要按实验拆分出自己的适配器、模型和入队服务。不让初级开发者自行修改重试预算、租约围栏、结果消费或设备动态路由。ECS 不使用本实验的静态 `wms` target。

本实验验证单进程 SQLite 事务和协议分类，不验证 PostgreSQL 锁、进程崩溃恢复、真实对端去重、部署安全或物理完成。真实 PostgreSQL/Redis 的准备与全量命令见[开发测试](testing/README.md)。
