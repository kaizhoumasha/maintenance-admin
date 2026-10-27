from typing import Annotated

from fastapi import Depends, Query, Request

from app.controllers.asset_dependencies import actor, device_status
from app.controllers.ecs_status import status_error
from app.core import SessionDep, error_responses, require_permission
from app.core.deps import require_admin
from app.core.errors import AppError, ErrorCode
from app.core.redis import RedisDep
from app.core.routing import new_router
from app.integrations.ecs_status import StatusQueryError
from app.schemas.base import DatabaseId, Page
from app.schemas.device import (
    DeviceAssignmentsIn,
    DeviceDiscoveryIn,
    DeviceMigrationIn,
    DevicePreviewRead,
    DeviceRead,
    DeviceRegisterIn,
    DeviceStatusRead,
    DeviceUpdate,
)
from app.schemas.query import UserListParams, parse_filter
from app.services import device_service as service
from app.services.device_status_service import DeviceStatusService

router = new_router(prefix="/devices", tags=["devices"], dependencies=[Depends(require_admin)])


@router.get(
    "",
    response_model=Page[DeviceRead],
    dependencies=[Depends(require_permission("asset:device:read"))],
)
async def list_devices(session: SessionDep, params: Annotated[UserListParams, Query()]):
    return await service.list_devices(session, params, parse_filter(params.filter))


@router.post(
    "/assignments",
    response_model=list[DeviceRead],
    responses=error_responses(404, 409),
    dependencies=[Depends(require_permission("asset:device:assign"))],
)
async def assign_devices(request: Request, data: DeviceAssignmentsIn, session: SessionDep):
    result = await service.assign_devices(session, data, actor=actor(request))
    request.state.audit_recorded = True
    return result


@router.post(
    "/discovery/preview",
    response_model=DevicePreviewRead,
    responses=error_responses(409, 502, 503, 504),
    dependencies=[Depends(require_permission("asset:device:discover"))],
)
async def preview(
    request: Request,
    data: DeviceDiscoveryIn,
    status: Annotated[DeviceStatusService, Depends(device_status)],
):
    try:
        result = await status.preview(
            actor_id=request.state.audit_user_id, ecs_origin=data.ecs_origin
        )
    except StatusQueryError as error:
        raise status_error(error) from None
    except ValueError:
        raise AppError(ErrorCode.INVALID_REQUEST, "ECS 地址无效") from None
    request.state.audit_recorded = True
    return result


@router.post(
    "/discovery/register",
    status_code=201,
    response_model=list[DeviceRead],
    responses=error_responses(404, 409, 503),
    dependencies=[Depends(require_permission("asset:device:register"))],
)
async def register(request: Request, data: DeviceRegisterIn, session: SessionDep, redis: RedisDep):
    result = await service.register_devices(session, data, actor=actor(request), redis=redis)
    request.state.audit_recorded = True
    return result


@router.get(
    "/{device_id}",
    response_model=DeviceRead,
    responses=error_responses(404),
    dependencies=[Depends(require_permission("asset:device:read"))],
)
async def get_device(device_id: DatabaseId, session: SessionDep):
    return await service.read_device(session, device_id)


@router.put(
    "/{device_id}",
    response_model=DeviceRead,
    responses=error_responses(404, 409),
    dependencies=[Depends(require_permission("asset:device:update"))],
)
async def update(request: Request, device_id: DatabaseId, data: DeviceUpdate, session: SessionDep):
    result = await service.update_device(session, device_id, data, actor=actor(request))
    request.state.audit_recorded = True
    return result


@router.post(
    "/{device_id}/route-migrations",
    response_model=DeviceRead,
    responses=error_responses(404, 409, 503),
    dependencies=[Depends(require_permission("asset:device:migrate"))],
)
async def migrate(
    request: Request,
    device_id: DatabaseId,
    data: DeviceMigrationIn,
    session: SessionDep,
    redis: RedisDep,
):
    result = await service.migrate_device_route(
        session, device_id, data, actor=actor(request), redis=redis
    )
    request.state.audit_recorded = True
    return result


@router.delete(
    "/{device_id}",
    status_code=204,
    responses=error_responses(404, 409),
    dependencies=[Depends(require_permission("asset:device:delete"))],
)
async def delete(
    request: Request,
    device_id: DatabaseId,
    session: SessionDep,
    version: Annotated[int, Query(ge=1)],
):
    await service.delete_device(session, device_id, version=version, actor=actor(request))
    request.state.audit_recorded = True


@router.post(
    "/{device_id}/refresh",
    response_model=DeviceStatusRead,
    responses=error_responses(404, 409, 502, 503, 504),
    dependencies=[Depends(require_permission("asset:device:refresh"))],
)
async def refresh(
    request: Request,
    device_id: DatabaseId,
    status: Annotated[DeviceStatusService, Depends(device_status)],
):
    try:
        result = await status.refresh_device(device_id)
    except StatusQueryError as error:
        raise status_error(error) from None
    request.state.audit_recorded = True
    return result
