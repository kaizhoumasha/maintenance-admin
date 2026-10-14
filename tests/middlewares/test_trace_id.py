async def test_access_log_points_to_endpoint(client, admin, caplog):
    await client.get("/api/v1/users/1", headers=admin)
    await client.get("/nope")
    srcs = [r.src for r in caplog.records if hasattr(r, "src") and "->" in r.getMessage()]
    # admin fixture 的登录请求也会产生访问日志,只看最后两条
    assert srcs[-2].startswith("controllers/users.py:") and srcs[-1] == "-"
