"""Device-directory policy around the storage-independent ECS Status query."""

import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from app.core.errors import AppError, ErrorCode
from app.core.identifiers import uuid7
from app.integrations.ecs_status import (
    StatusQueryError,
    normalize_status_origin,
    query,
)
from app.models import Device, DeviceRoute
from app.schemas.device import DevicePreviewRead, DeviceStatusRead, PreviewEntry, PreviewSnapshot
from app.services.device_service import cache_call

_ACQUIRE = """
local t=redis.call('TIME')
local now=t[1]*1000+math.floor(t[2]/1000)
local lease=tonumber(redis.call('HGET',KEYS[1],'lease_until') or '0')
local stored_next=redis.call('HGET',KEYS[1],'next_at')
if not stored_next and tonumber(ARGV[2])<0 then return {0,'RECOVER',0} end
local next_at=tonumber(stored_next or ARGV[2])
if lease>now then return {0,'BUSY',math.ceil((lease-now)/1000)} end
if next_at>now then return {0,'WAIT',math.ceil((next_at-now)/1000)} end
redis.call('HSET',KEYS[1],'token',ARGV[1],'lease_until',now+15000,'next_at',now+5000)
redis.call('PEXPIREAT',KEYS[1],now+45000)
return {1,'OK',0}
"""
_FINISH = """
local owns=redis.call('HGET',KEYS[1],'token')==ARGV[1]
if not owns and tonumber(ARGV[2])<=0 then return 0 end
local t=redis.call('TIME')
local now=t[1]*1000+math.floor(t[2]/1000)
local next_at=math.max(tonumber(redis.call('HGET',KEYS[1],'next_at') or '0'),tonumber(ARGV[2]))
redis.call('HSET',KEYS[1],'next_at',next_at)
if owns then redis.call('HSET',KEYS[1],'lease_until',0) end
redis.call('PEXPIREAT',KEYS[1],math.max(next_at,now)+30000)
return owns and 1 or 0
"""


def utc_now():
    return datetime.now(UTC)


@dataclass(frozen=True)
class Target:
    id: int
    version: int
    route_id: str
    code: str
    origin: str


class DeviceStatusService:
    def __init__(self, factory, redis, *, query_fn=query, clock=utc_now):
        self.factory, self.redis, self.query_fn, self.clock = factory, redis, query_fn, clock

    @staticmethod
    def origin_key(origin):
        return (
            "device-status:" + hashlib.sha256(normalize_status_origin(origin).encode()).hexdigest()
        )

    async def _eval(self, script, key, *args):
        return await cache_call(self.redis.eval(script, 1, key, *args))

    async def _query(self, origin, *, device_code=None, target=None):
        origin = normalize_status_origin(origin)
        token = str(uuid7())
        key = self.origin_key(origin)
        allowed, kind, seconds = await self._eval(_ACQUIRE, key, token, -1)
        if kind == "RECOVER":
            server = await cache_call(self.redis.info("server"))
            restart = self.clock() - timedelta(seconds=server["uptime_in_seconds"])
            recover = math.ceil((restart + timedelta(seconds=30)).timestamp() * 1000)
            allowed, kind, seconds = await self._eval(_ACQUIRE, key, token, recover)
        if not allowed:
            if kind == "BUSY":
                raise AppError(ErrorCode.CONFLICT, "该ECS正在刷新", detail="QUERY_IN_PROGRESS")
            raise AppError(
                ErrorCode.TOO_MANY_REQUESTS,
                "该ECS尚在查询等待期",
                headers={"Retry-After": str(seconds)},
            )
        result, error, next_at = None, None, 0
        try:
            result = await self.query_fn(origin, device_code=device_code)
        except StatusQueryError as failure:
            error = failure
            if error.http_status == 429:
                wait = max(
                    self.clock() + timedelta(seconds=30), error.retry_after_at or self.clock()
                )
                error.retry_after_at = wait
                next_at = math.ceil(wait.timestamp() * 1000)
        finally:
            owns = await self._eval(_FINISH, key, token, next_at)
        if target is not None:
            current = await self._target(target.id)
            if current != target:
                raise AppError(
                    ErrorCode.CONFLICT, "设备配置已变化，请重新查询", detail="DEVICE_CONFIG_CHANGED"
                )
        current_token = await cache_call(self.redis.hget(key, "token"))
        if not owns or current_token != token:
            raise AppError(ErrorCode.CONFLICT, "查询已被更新请求替代", detail="QUERY_SUPERSEDED")
        if error:
            raise error
        return result

    async def fetch(self, origin, *, device_code=None):
        return await self._query(origin, device_code=device_code)

    async def preview(self, *, actor_id, ecs_origin):
        result = await self.fetch(ecs_origin)
        codes = [item.device.device_code for item in result.devices]
        async with self.factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            else:
                await session.execute(text("BEGIN"))
            current = {
                device.device_code: (device, route)
                for device, route in (
                    await session.execute(
                        select(Device, DeviceRoute)
                        .join(DeviceRoute, Device.current_route_id == DeviceRoute.id)
                        .where(Device.device_code.in_(codes))
                    )
                ).all()
            }
            counts = dict(
                (
                    await session.execute(
                        select(DeviceRoute.device_code, func.count())
                        .where(DeviceRoute.device_code.in_(codes))
                        .group_by(DeviceRoute.device_code)
                    )
                ).all()
            )
            entries = {}
            public = []
            for item in result.devices:
                code = item.device.device_code
                device, route = current.get(code, (None, None))
                if device is None:
                    classification = "NEW"
                elif route.ecs_origin == result.ecs_origin:
                    classification = "EXISTING"
                else:
                    classification = "ADDRESS_CONFLICT"
                entries[code] = PreviewEntry(
                    classification=classification,
                    device_id=device.id if device else None,
                    version=device.version if device else None,
                    route_id=route.id if route else None,
                    route_history_count=counts.get(code, 0),
                    data=item,
                )
                public.append(
                    {
                        "classification": classification,
                        **item.model_dump(mode="json"),
                        "local": {
                            "id": device.id,
                            "display_name": device.display_name,
                            "work_line_id": device.work_line_id,
                            "version": device.version,
                            "ecs_origin": route.ecs_origin,
                        }
                        if device
                        else None,
                    }
                )
        expires = result.fetched_at + timedelta(seconds=300)
        remaining = math.ceil((expires - self.clock()).total_seconds())
        if remaining <= 0:
            raise AppError(ErrorCode.CONFLICT, "预览已过期", detail="PREVIEW_EXPIRED")
        token = secrets.token_urlsafe(32)
        snapshot = PreviewSnapshot(
            actor_id=actor_id,
            ecs_origin=result.ecs_origin,
            fetched_at=result.fetched_at,
            expires_at=expires,
            entries=entries,
        )
        await cache_call(
            self.redis.set("device-preview:" + token, snapshot.model_dump_json(), ex=remaining)
        )
        return DevicePreviewRead(
            preview_token=token,
            ecs_origin=result.ecs_origin,
            fetched_at=result.fetched_at,
            expires_at=expires,
            items=public,
        )

    async def _target(self, device_id):
        async with self.factory() as session:
            pair = (
                await session.execute(
                    select(Device, DeviceRoute)
                    .join(DeviceRoute, Device.current_route_id == DeviceRoute.id)
                    .where(Device.id == device_id)
                )
            ).one_or_none()
            if pair is None:
                if await session.get(Device, device_id) is not None:
                    raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "设备路由配置不一致")
                raise AppError(ErrorCode.NOT_FOUND, "设备配置不存在")
            device, route = pair
            if route.device_code != device.device_code:
                raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "设备路由配置不一致")
            return Target(device.id, device.version, route.id, device.device_code, route.ecs_origin)

    async def refresh_device(self, device_id):
        target = await self._target(device_id)
        result = await self._query(target.origin, device_code=target.code, target=target)
        return DeviceStatusRead(
            device_id=target.id,
            device_version=target.version,
            current_route_id=target.route_id,
            result=result,
        )
