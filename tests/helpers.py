"""跨测试文件共享的辅助函数(夹具放 conftest.py,这里放普通函数)。"""

import base64
import json
from typing import Any

from app.core.security import create_access_token

PAYLOAD = {"username": "kai", "email": "k@example.com", "password": "password123"}


def bearer(user_id: int | str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def make_user_with_perms(client, admin, perms):
    """建一个只拥有 perms 的用户(经 API 走一遍 角色→权限→用户),返回其 id。"""
    role = (
        await client.post("/api/v1/roles", json={"name": "r", "permissions": perms}, headers=admin)
    ).json()
    uid = (await client.post("/api/v1/users", json=PAYLOAD, headers=admin)).json()["id"]
    r = await client.put(
        f"/api/v1/users/{uid}/roles", json={"role_ids": [role["id"]]}, headers=admin
    )
    assert r.status_code == 204
    return uid


def raw_cursor(payload: Any) -> str:
    """按对外约定(base64url、无填充的 JSON)手工构造游标,用来造非法或边界输入。"""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
