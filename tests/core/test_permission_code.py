"""权限码规则:三段式 <module>:<resource>:<action>,且一个权限码只对应一个路由。"""

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


def test_require_permission_rejects_duplicate_code(monkeypatch):
    monkeypatch.setattr(deps, "registered_codes", set())
    require_permission("admin:demo:read")
    with pytest.raises(ValueError, match="重复"):
        require_permission("admin:demo:read")


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
