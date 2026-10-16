import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core import AppError, ErrorCode, verify_password
from app.models import User
from app.schemas.user import UserCreate, UserUpdate
from app.services import session_service, user_service
from scripts.seed_admin import INITIAL_PERMISSIONS, seed_admin


async def _setup(db):
    """种子管理员 root(id=1,拥有全部权限)和普通用户 kai(id=2,无角色)。"""
    await seed_admin(db, "root", "root@example.com", "password123")
    root = await user_service.get_user(db, 1)
    kai = await user_service.create_user(
        db, UserCreate(username="kai", email="kai@example.com", password="password123")
    )
    return root, kai


async def test_get_me_returns_sorted_permission_codes(db):
    root, kai = await _setup(db)
    me = await user_service.get_me(db, root)
    assert me.username == "root" and me.permissions == sorted(INITIAL_PERMISSIONS)
    assert (await user_service.get_me(db, kai)).permissions == []


async def test_update_user_changes_only_given_fields(db, redis):
    root, kai = await _setup(db)
    updated = await user_service.update_user(
        db, redis, root.id, kai.id, UserUpdate(email="new@example.com")
    )
    assert updated.email == "new@example.com" and updated.is_active is True


async def test_update_user_email_conflict(db, redis):
    root, kai = await _setup(db)
    with pytest.raises(AppError) as e:
        await user_service.update_user(db, redis, root.id, kai.id, UserUpdate(email=root.email))
    assert e.value.code == ErrorCode.CONFLICT


async def test_cannot_disable_self(db, redis):
    root, _ = await _setup(db)
    with pytest.raises(AppError) as e:
        await user_service.update_user(db, redis, root.id, root.id, UserUpdate(is_active=False))
    assert e.value.code == ErrorCode.INVALID_REQUEST


async def test_disable_revokes_sessions(db, redis):
    root, kai = await _setup(db)
    await session_service.issue_tokens(redis, kai)
    assert f"user_refresh:{kai.id}" in redis.data
    await user_service.update_user(db, redis, root.id, kai.id, UserUpdate(is_active=False))
    assert f"user_refresh:{kai.id}" not in redis.data


async def test_disabling_last_admin_is_rejected_and_rolled_back(db, redis):
    root, kai = await _setup(db)  # root 是唯一管理员
    root_id, kai_id = root.id, kai.id  # 回滚会使实例过期,先取出 id
    with pytest.raises(AppError) as e:
        await user_service.update_user(db, redis, kai_id, root_id, UserUpdate(is_active=False))
    assert e.value.code == ErrorCode.CONFLICT
    await db.rollback()  # HTTP 请求里会话随请求关闭而回滚,这里显式模拟
    assert (await db.get(User, root_id)).is_active is True


async def test_change_password_rejects_wrong_old_password(db, redis):
    _, kai = await _setup(db)
    with pytest.raises(AppError) as e:
        await user_service.change_password(db, redis, kai, "wrong-pass", "newpass123")
    assert e.value.code == ErrorCode.INVALID_REQUEST
    assert verify_password("password123", kai.hashed_password)


async def test_change_and_reset_password_revoke_sessions(db, redis):
    _, kai = await _setup(db)
    await session_service.issue_tokens(redis, kai)
    await user_service.change_password(db, redis, kai, "password123", "newpass123")
    assert verify_password("newpass123", kai.hashed_password)
    assert f"user_refresh:{kai.id}" not in redis.data

    await session_service.issue_tokens(redis, kai)
    await user_service.reset_password(db, redis, kai.id, "reset-pass1")
    assert verify_password("reset-pass1", kai.hashed_password)
    assert f"user_refresh:{kai.id}" not in redis.data


@pytest.mark.parametrize("reset", [False, True])
async def test_failed_revocation_keeps_old_password(db, redis, monkeypatch, reset):
    _, kai = await _setup(db)
    user_id, old_hash = kai.id, kai.hashed_password
    await session_service.issue_tokens(redis, kai)

    async def unavailable(*args):
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "eval", unavailable)
    with pytest.raises(RedisConnectionError):
        if reset:
            await user_service.reset_password(db, redis, user_id, "newpass123")
        else:
            await user_service.change_password(db, redis, kai, "password123", "newpass123")
    await db.rollback()  # 请求失败时会话关闭,未提交的密码修改应回滚
    assert (await db.get(User, user_id)).hashed_password == old_hash
    assert f"user_refresh:{user_id}" in redis.data
