from typing import Annotated

from fastapi import Depends, Query, Request

from app.businesses import catalog
from app.controllers.asset_dependencies import actor
from app.core import SessionDep, error_responses, require_permission
from app.core.deps import require_admin
from app.core.routing import new_router
from app.schemas.base import DatabaseId, Page
from app.schemas.query import UserListParams, parse_filter
from app.schemas.workline import WorkLineCreate, WorkLineRead, WorkLineUpdate
from app.services import workline_service as service

router = new_router(
    prefix="/work-lines", tags=["work-lines"], dependencies=[Depends(require_admin)]
)


@router.get(
    "",
    response_model=Page[WorkLineRead],
    dependencies=[Depends(require_permission("asset:workline:read"))],
)
async def list_worklines(session: SessionDep, params: Annotated[UserListParams, Query()]):
    return await service.list_worklines(session, params, parse_filter(params.filter))


@router.post(
    "",
    status_code=201,
    response_model=WorkLineRead,
    responses=error_responses(409),
    dependencies=[Depends(require_permission("asset:workline:create"))],
)
async def create_workline(request: Request, data: WorkLineCreate, session: SessionDep):
    result = await service.create_workline(
        session, data, actor=actor(request), definition_lookup=catalog.get_definition
    )
    request.state.audit_recorded = True
    return result


@router.get(
    "/{workline_id}",
    response_model=WorkLineRead,
    responses=error_responses(404),
    dependencies=[Depends(require_permission("asset:workline:read"))],
)
async def get_workline(workline_id: DatabaseId, session: SessionDep):
    return await service.read_workline(session, workline_id)


@router.put(
    "/{workline_id}",
    response_model=WorkLineRead,
    responses=error_responses(404, 409),
    dependencies=[Depends(require_permission("asset:workline:update"))],
)
async def update_workline(
    request: Request, workline_id: DatabaseId, data: WorkLineUpdate, session: SessionDep
):
    result = await service.update_workline(
        session, workline_id, data, actor=actor(request), definition_lookup=catalog.get_definition
    )
    request.state.audit_recorded = True
    return result


@router.delete(
    "/{workline_id}",
    status_code=204,
    responses=error_responses(404, 409),
    dependencies=[Depends(require_permission("asset:workline:delete"))],
)
async def delete_workline(
    request: Request,
    workline_id: DatabaseId,
    session: SessionDep,
    version: Annotated[int, Query(ge=1)],
):
    await service.delete_workline(session, workline_id, version=version, actor=actor(request))
    request.state.audit_recorded = True
