from app.integrations.wms_callback.discovery import candidate_modules


def test_dev_only_modules_are_discovered_in_local_only():
    local = candidate_modules("local")
    production = candidate_modules("production")
    assert "app.integrations.wms_callback.dev_callback" in local
    assert "app.integrations.wms_callback.dev_callback" not in production
    assert "app.integrations.wms_transport.callback" in production
