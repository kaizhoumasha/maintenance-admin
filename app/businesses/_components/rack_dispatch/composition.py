from app.businesses.components import ComponentDefinition

COMPONENT = ComponentDefinition(
    key="rack_dispatch",
    entry_modules=("app.businesses._components.rack_dispatch.request.rack_departure_decide_v1",),
    model_modules=("app.businesses._components.rack_dispatch.models",),
    required_methods=(
        "register",
        "prepare",
        "advance",
        "progress",
        "withdraw_unused",
        "jobs",
        "pending_ids",
        "pending_upper_id",
        "lock_jobs",
        "lock_actions",
        "note_facts_changed",
        "final_result",
        "has_position_conflict",
    ),
    requires_progression=True,
)
