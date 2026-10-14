async def test_access_log_points_to_endpoint(client, caplog):
    await client.get("/api/v1/users/1")
    await client.get("/nope")
    srcs = [r.src for r in caplog.records if hasattr(r, "src") and "->" in r.getMessage()]
    assert srcs[0].startswith("controllers/users.py:") and srcs[1] == "-"
