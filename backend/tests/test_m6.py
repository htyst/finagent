from test_m4 import demo, login, start

def test_stale_tab_user_header_blocks_reads_and_confirmation(demo):
    client, _ = demo
    headers = login(client)
    task = start(client, headers, "从活期账户转到储蓄账户100元")
    assert client.get("/api/accounts", headers={"X-FinAgent-Expected-User":"u1"}).status_code == 200
    client.post("/api/demo/session", json={"user_id":"u2"})
    for path in ("/api/accounts", "/api/tasks/current", "/api/agent/tasks/current", "/api/demo/csrf"):
        response = client.get(path,headers={"X-FinAgent-Expected-User":"u1"})
        assert response.status_code == 409 and response.json()["error_code"] == "SESSION_CHANGED"
        assert response.json()["data"] is None
    response=client.post(f"/api/proposals/{task['proposal']['id']}/confirmation-token",headers={**headers,"X-FinAgent-Expected-User":"u1"})
    assert response.status_code == 409
    assert client.get("/api/accounts",headers={"X-FinAgent-Expected-User":"u2"}).json()["data"][0]["balance_minor"] == 680000
