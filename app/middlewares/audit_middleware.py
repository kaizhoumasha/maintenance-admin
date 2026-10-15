"""审计中间件(纯 ASGI):自动记录成功的写操作。旁路写入,失败只打日志,不影响业务。

不继承 BaseHTTPMiddleware:它把请求跑在另一个 task 里,而且拿不到响应体,不适合做"响应发出后再记录"。
"""

import json
import re

from app.core import logger
from app.models import AuditLog

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_BODY = 64 * 1024  # 响应体只为取新建资源的 id;请求体超过就整体不记
# 兜底:字段名像敏感信息的,即使忘了标 NO_AUDIT 也不记
SENSITIVE = re.compile(r"password|token|secret", re.I)


def _resource(route_path: str) -> str | None:
    # /api/v1/users/{user_id}/roles -> users
    for seg in route_path.split("/"):
        if seg and seg != "api" and not re.fullmatch(r"v\d+", seg):
            return seg
    return None


def _hidden_fields(route) -> set[str]:
    """路由请求体 schema 里不该记录的字段:标了 NO_AUDIT 的,或名字像敏感信息的。"""
    # FastAPI 的 ModelField 没有 type_,请求体 schema 在 field_info.annotation
    field = getattr(getattr(route, "body_field", None), "field_info", None)
    model = getattr(field, "annotation", None)
    fields = getattr(model, "model_fields", {})
    return {
        name
        for name, f in fields.items()
        if (f.json_schema_extra or {}).get("audit") is False or SENSITIVE.search(name)
    }


class AuditMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in WRITE_METHODS:
            return await self.app(scope, receive, send)

        # 先建好 state:内层依赖往 request.state 写 audit_user_id / audit_action,
        # 这里才读得到同一个 dict
        state = scope.setdefault("state", {})
        status, trace_id, body = 0, "", bytearray()
        req_body, req_too_big, body_too_big = bytearray(), False, False

        async def receive_wrapper():
            # 只旁观,原样转交;请求体用于 detail["body"]。超限就清空并停止缓冲,避免大上传占内存
            nonlocal req_too_big
            message = await receive()
            if message["type"] == "http.request" and not req_too_big:
                req_body.extend(message.get("body", b""))
                if len(req_body) > MAX_BODY:
                    req_too_big = True
                    req_body.clear()
            return message

        async def send_wrapper(message):
            nonlocal status, trace_id, body_too_big
            if message["type"] == "http.response.start":
                status = message["status"]
                trace_id = dict(message["headers"]).get(b"x-trace-id", b"").decode()
            elif message["type"] == "http.response.body" and not body_too_big:
                body.extend(message.get("body", b""))
                if len(body) > MAX_BODY:  # 响应太大就放弃取 id
                    body_too_big = True
                    body.clear()
            await send(message)

        await self.app(scope, receive_wrapper, send_wrapper)

        # 只记成功;没有 audit_action 说明没走 require_permission(未登录 / 被拒 / 无需权限的接口)
        if 200 <= status < 300 and state.get("audit_action"):
            try:
                await self._record(
                    scope, state, trace_id, bytes(body), None if req_too_big else bytes(req_body)
                )
            except Exception:
                logger.exception("审计日志写入失败")

    async def _record(
        self, scope, state, trace_id: str, body: bytes, req_body: bytes | None
    ) -> None:
        params = scope.get("path_params", {})
        template = getattr(scope.get("route"), "path", scope["path"])
        # scope 里的路径参数还是原始字符串(FastAPI 的类型转换发生在依赖解析里)
        resource_id = next((int(v) for v in params.values() if str(v).isdigit()), None)
        if resource_id is None and body:  # 新建资源:id 在响应体里
            try:
                new_id = json.loads(body).get("id")
            except (ValueError, AttributeError):
                new_id = None
            resource_id = new_id if isinstance(new_id, int) else None
        detail = {"method": scope["method"], "route": template, "path_params": params}
        try:
            payload = json.loads(req_body) if req_body else None
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            hidden = _hidden_fields(scope.get("route"))
            # 名字兜底也用在 payload 的 key 上:客户端多发的、schema 里没声明的字段也要挡住
            detail["body"] = {
                k: v for k, v in payload.items() if k not in hidden and not SENSITIVE.search(k)
            }
        ua = dict(scope["headers"]).get(b"user-agent", b"").decode(errors="replace")
        client = scope.get("client")
        log = AuditLog(
            user_id=state.get("audit_user_id"),
            action=state["audit_action"],
            trace_id=trace_id,
            # Postgres 会拒绝超长值,而审计失败又会被吞掉,所以写入前必须截断
            user_agent=ua[:255] or None,
            ip=client[0] if client else None,
            resource=_resource(template),
            resource_id=resource_id,
            detail=detail,
        )
        async with scope["app"].state.audit_session_factory() as session:
            session.add(log)
            await session.commit()
