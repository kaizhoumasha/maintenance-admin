from fastapi import APIRouter, Depends, status

from app.core import ErrorResponse, SessionDep, require_permission
from app.schemas.base import DatabaseId
from app.schemas.rbac import RoleCreate, RolePermissionsIn, RoleRead
from app.services import rbac_service

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get(
    "", response_model=list[RoleRead], dependencies=[Depends(require_permission("admin:role:read"))]
)
async def list_roles(session: SessionDep):
    return await rbac_service.list_roles(session)


@router.post(
    "",
    response_model=RoleRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
    dependencies=[Depends(require_permission("admin:role:create"))],
)
async def create_role(data: RoleCreate, session: SessionDep):
    return await rbac_service.create_role(session, data.name, data.permissions)


@router.put(
    "/{role_id}/permissions",
    response_model=RoleRead,
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_permission("admin:role:set_permissions"))],
)
async def set_role_permissions(role_id: DatabaseId, data: RolePermissionsIn, session: SessionDep):
    return await rbac_service.set_role_permissions(session, role_id, data.permissions)
