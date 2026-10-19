from fastapi import Depends, status

from app.core import SessionDep, error_responses, require_permission
from app.core.routing import new_router
from app.schemas.base import DatabaseId
from app.schemas.menu import MenuIn, MenuNode, MenuRead
from app.services import menu_service

router = new_router(prefix="/menus", tags=["menus"])

_ERRORS = error_responses(404, 409)


@router.get(
    "",
    response_model=list[MenuNode],
    dependencies=[Depends(require_permission("admin:menu:read"))],
)
async def list_menus(session: SessionDep):
    return await menu_service.list_menu_tree(session)


@router.post(
    "",
    response_model=MenuRead,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
    dependencies=[Depends(require_permission("admin:menu:create"))],
)
async def create_menu(data: MenuIn, session: SessionDep):
    return await menu_service.create_menu(session, data)


@router.put(
    "/{menu_id}",
    response_model=MenuRead,
    responses=_ERRORS,
    dependencies=[Depends(require_permission("admin:menu:update"))],
)
async def update_menu(menu_id: DatabaseId, data: MenuIn, session: SessionDep):
    return await menu_service.update_menu(session, menu_id, data)


@router.delete(
    "/{menu_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
    dependencies=[Depends(require_permission("admin:menu:delete"))],
)
async def delete_menu(menu_id: DatabaseId, session: SessionDep) -> None:
    await menu_service.delete_menu(session, menu_id)
