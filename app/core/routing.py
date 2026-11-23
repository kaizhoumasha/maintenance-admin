"""路由类:按依赖和参数自动补全 OpenAPI 的通用错误响应,路由只需手写业务错误(404/409/503 等)。"""

from typing import Any

from fastapi import APIRouter
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.core.deps import get_current_user
from app.core.errors import error_responses


def _walk(dependant: Dependant):
    yield dependant
    for dep in dependant.dependencies:
        yield from _walk(dep)


class DocumentedRoute(APIRoute):
    """401/403 来自 get_current_user / require_permission,400 来自任何请求参数或请求体
    (校验失败统一映射为 400),429 对所有路由成立(app 级全局限流)。"""

    def __init__(self, path: str, endpoint: Any, *, responses: dict | None = None, **kwargs: Any):
        super().__init__(path, endpoint, responses=responses, **kwargs)
        nodes = list(_walk(self.dependant))
        calls = [n.call for n in nodes]
        auto = {429}
        if get_current_user in calls:
            auto.add(401)
        if any(hasattr(call, "requires_permission") for call in calls):
            auto.add(403)
        if any(
            n.query_params or n.path_params or n.header_params or n.cookie_params or n.body_params
            for n in nodes
        ):
            auto.add(400)
        missing = auto - set(responses or {})
        if missing:  # response_fields 在 __init__ 里生成,所以带上合并后的 responses 重建一次
            super().__init__(
                path,
                endpoint,
                responses={**error_responses(*missing), **(responses or {})},
                **kwargs,
            )


def new_router(**kwargs: Any) -> APIRouter:
    return APIRouter(route_class=DocumentedRoute, **kwargs)


def drop_422(openapi: dict[str, Any]) -> dict[str, Any]:
    """移除映射为 400 的自动校验响应,保留协议显式声明的 422。"""
    for operations in openapi["paths"].values():
        for operation in operations.values():
            responses = operation["responses"]
            schema = responses.get("422", {}).get("content", {}).get("application/json", {})
            if schema.get("schema", {}).get("$ref") == "#/components/schemas/HTTPValidationError":
                responses.pop("422")
    schemas = openapi.get("components", {}).get("schemas", {})
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    return openapi
