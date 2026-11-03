from app.businesses.components import ComponentDefinition

COMPONENT = ComponentDefinition(
    key="rack_dispatch",
    model_modules=("app.businesses._components.rack_dispatch.models",),
    required_methods=("register", "prepare", "advance", "progress", "withdraw_unused"),
    requires_progression=True,
)
