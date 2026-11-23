"""权限码规则:三段式,同一权限可保护多个独立路由。"""

import pytest

from app.core import deps
from app.core.deps import require_permission
from scripts.seed_admin import INITIAL_PERMISSIONS

PERMISSIONS = "/api/v1/permissions"


def test_routes_codes_match_seed_exactly():
    # 路由里用到的码必须都在 seed 里(写错码不会报错,只会让所有人 403),seed 里也不留没人用的码
    assert deps.registered_codes == set(INITIAL_PERMISSIONS)


@pytest.mark.parametrize("code", ["user:create", "Admin:user:create", "admin:user:", "a:b:c:d", ""])
def test_require_permission_rejects_bad_format(code, monkeypatch):
    monkeypatch.setattr(deps, "registered_codes", set())
    with pytest.raises(ValueError, match="格式"):
        require_permission(code)


async def test_same_permission_routes_enforce_grants_revocation_and_documentation(
    client, admin, monkeypatch
):
    from typing import Annotated

    from fastapi import Depends, FastAPI, Request
    from httpx import ASGITransport, AsyncClient

    from app.core import AppError
    from app.core.handlers import app_error_handler
    from app.core.routing import new_router
    from app.main import app
    from app.models import User
    from tests.helpers import bearer, make_user_with_perms

    original_codes = set(deps.registered_codes)
    monkeypatch.setattr(deps, "registered_codes", set(original_codes))
    router = new_router()

    @router.get("/first")
    async def first(
        request: Request, user: Annotated[User, Depends(require_permission("admin:user:read"))]
    ):
        return {"user_id": user.id, "action": request.state.audit_action}

    @router.get("/second")
    async def second(
        request: Request, user: Annotated[User, Depends(require_permission("admin:user:read"))]
    ):
        return {"user_id": user.id, "action": request.state.audit_action}

    api = FastAPI(exception_handlers={AppError: app_error_handler})
    api.dependency_overrides.update(app.dependency_overrides)
    api.include_router(router)
    uid = await make_user_with_perms(client, admin, ["admin:user:read"])
    role = next(
        r for r in (await client.get("/api/v1/roles", headers=admin)).json() if r["name"] == "r"
    )
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as isolated:
        for path in ("/first", "/second"):
            assert "403" in api.openapi()["paths"][path]["get"]["responses"]
            assert (await isolated.get(path)).status_code == 401
            response = await isolated.get(path, headers=bearer(uid))
            assert response.status_code == 200
            assert response.json() == {"user_id": uid, "action": "admin:user:read"}
        response = await client.put(
            f"/api/v1/roles/{role['id']}/permissions", json={"permissions": []}, headers=admin
        )
        assert response.status_code == 200
        for path in ("/first", "/second"):
            assert (await isolated.get(path, headers=bearer(uid))).status_code == 403
        assert deps.registered_codes == original_codes


@pytest.mark.parametrize(
    "code", ["user:create", "Admin:User:Create", "admin:user", "admin:user:cre ate"]
)
async def test_create_permission_rejects_bad_format(client, admin, code):
    r = await client.post(PERMISSIONS, json={"code": code, "name": "x"}, headers=admin)
    assert r.status_code == 400


async def test_create_permission_accepts_three_segments(client, admin):
    r = await client.post(
        PERMISSIONS, json={"code": "biz:order:cancel_all", "name": "x"}, headers=admin
    )
    assert r.status_code == 201
