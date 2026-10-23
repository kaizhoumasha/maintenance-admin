from app.integrations.ecs_callback.discovery import candidate_modules


def test_dev_module_only_in_local():
    local = candidate_modules("local")
    production = candidate_modules("production")
    assert "app.integrations.ecs_callback.ecs_event" in production
    assert "app.integrations.ecs_callback.dev_ecs_event" in local
    assert "app.integrations.ecs_callback.dev_ecs_event" not in production
