from app.main import app


async def test_result_controller_is_unavailable_without_explicit_assembly(client, monkeypatch):
    monkeypatch.setattr(app.state, "ecs_commands", None, raising=False)
    response = await client.post("/api/v1/callback/result", json={})
    assert response.status_code == 503


async def test_result_controller_rejects_oversize_before_database(client):
    response = await client.post("/api/v1/callback/result", content=b"x" * (262144 + 1))
    assert response.status_code == 413
