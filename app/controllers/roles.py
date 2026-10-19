from fastapi import Depends, status

from app.core import SessionDep, error_responses, require_permission
from app.core.routing import new_router
from app.schemas.base import DatabaseId
from app.schemas.rbac import RoleCreate, RolePermissionsIn, RoleRead, RoleUpdate
from app.services import rbac_service

router = new_router(prefix="/roles", tags=["roles"])


@router.get(
    "", response_model=list[RoleRead], dependencies=[Depends(require_permission("admin:role:read"))]
)
async def list_roles(session: SessionDep):
    return await rbac_service.list_roles(session)


@router.post(
    "",
    response_model=RoleRead,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(409),
    dependencies=[Depends(require_permission("admin:role:create"))],
)
async def create_role(data: RoleCreate, session: SessionDep):
    return await rbac_service.create_role(session, data.name, data.permissions)


@router.put(
    "/{role_id}/permissions",
    response_model=RoleRead,
    responses=error_responses(404),
    dependencies=[Depends(require_permission("admin:role:set_permissions"))],
)
async def set_role_permissions(role_id: DatabaseId, data: RolePermissionsIn, session: SessionDep):
    return await rbac_service.set_role_permissions(session, role_id, data.permissions)


@router.put(
    "/{role_id}",
    response_model=RoleRead,
    responses=error_responses(404, 409),
    dependencies=[Depends(require_permission("admin:role:update"))],
)
async def update_role(role_id: DatabaseId, data: RoleUpdate, session: SessionDep):
    return await rbac_service.update_role(session, role_id, data.name)


@router.delete(
    "/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(404, 409),
    dependencies=[Depends(require_permission("admin:role:delete"))],
)
async def delete_role(role_id: DatabaseId, session: SessionDep) -> None:
    await rbac_service.delete_role(session, role_id)
