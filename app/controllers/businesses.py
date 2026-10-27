from fastapi import Depends

from app.businesses import catalog
from app.core import require_permission
from app.core.deps import require_admin
from app.core.routing import new_router
from app.schemas.business import BusinessRead

router = new_router(
    prefix="/businesses", tags=["businesses"], dependencies=[Depends(require_admin)]
)


@router.get(
    "",
    response_model=list[BusinessRead],
    dependencies=[Depends(require_permission("asset:workline:read"))],
)
async def list_businesses():
    return [
        {
            "code": definition.code,
            "display_name": definition.display_name,
            "device_roles": [
                {"key": role.role_key, "display_name": role.display_name, "required": role.required}
                for role in definition.device_roles
            ],
            "position_slots": [
                {"key": slot.slot_key, "display_name": slot.display_name, "required": slot.required}
                for slot in definition.position_slots
            ],
            "initial_entries": [
                {"kind": entry.kind, "name": entry.name, "device_role": entry.device_role}
                for entry in definition.initial_entries
            ],
        }
        for definition in catalog.definitions()
    ]
