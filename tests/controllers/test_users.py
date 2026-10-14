import threading

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import user_service

PAYLOAD = {"username": "kai", "email": "k@example.com", "password": "password123"}


async def test_create_and_get_user(client, admin):
    r = await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == "kai" and "password" not in body and "hashed_password" not in body

    r = await client.get(f"/api/v1/users/{body['id']}", headers=admin)
    assert r.status_code == 200 and r.json()["email"] == "k@example.com"


async def test_duplicate_username_conflicts(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    r = await client.post(
        "/api/v1/users", json={**PAYLOAD, "email": "other@example.com"}, headers=admin
    )
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"


async def test_get_missing_user_404(client, admin):
    r = await client.get("/api/v1/users/999", headers=admin)
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


async def test_duplicate_email_conflicts(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    r = await client.post("/api/v1/users", json={**PAYLOAD, "username": "other"}, headers=admin)
    assert r.status_code == 409 and r.json()["message"] == "邮箱已被注册"


async def test_concurrent_duplicate_hits_unique_constraint(client, admin, monkeypatch, caplog):
    # 模拟并发:预检查都没查到,commit 时由真实的唯一约束报 IntegrityError,应被 handler 转成 409
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)

    async def no_hit(*_a, **_k):
        return None

    monkeypatch.setattr(AsyncSession, "scalar", no_hit)
    r = await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    rec = next(x for x in caplog.records if "integrity" in x.getMessage())
    assert rec.src.startswith("services/user_service.py:")


async def test_password_hashing_runs_off_event_loop(client, admin, monkeypatch):
    threads = []
    real = user_service.hash_password

    def spy(password):
        threads.append(threading.current_thread())
        return real(password)

    monkeypatch.setattr(user_service, "hash_password", spy)
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    assert threads and threads[0] is not threading.main_thread()
