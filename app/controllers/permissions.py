from fastapi import Depends, status

from app.core import SessionDep, error_responses, require_permission
from app.core.routing import new_router
from app.schemas.rbac import PermissionCreate, PermissionRead
from app.services import rbac_service

router = new_router(prefix="/permissions", tags=["permissions"])


@router.get(
    "",
    response_model=list[PermissionRead],
    dependencies=[Depends(require_permission("admin:permission:read"))],
)
async def list_permissions(session: SessionDep):
    return await rbac_service.list_permissions(session)


@router.post(
    "",
    response_model=PermissionRead,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(409),
    dependencies=[Depends(require_permission("admin:permission:create"))],
)
async def create_permission(data: PermissionCreate, session: SessionDep):
    return await rbac_service.create_permission(session, data.code, data.name)
