from fastapi import APIRouter, Depends, status

from app.core import ErrorResponse, SessionDep, require_permission
from app.schemas.rbac import PermissionCreate, PermissionRead
from app.services import rbac_service

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.get(
    "",
    response_model=list[PermissionRead],
    dependencies=[Depends(require_permission("role:read"))],
)
async def list_permissions(session: SessionDep):
    return await rbac_service.list_permissions(session)


@router.post(
    "",
    response_model=PermissionRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
    dependencies=[Depends(require_permission("role:write"))],
)
async def create_permission(data: PermissionCreate, session: SessionDep):
    return await rbac_service.create_permission(session, data.code, data.name)
