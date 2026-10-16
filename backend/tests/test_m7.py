import httpx
import pytest
from app.agent.telemetry import collect, measured_post
from app.agent.provider import select_live_tool
from app.domain.errors import DomainError

def test_retry_metrics_are_scoped_and_do_not_capture_secrets(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "private-test-key")
    responses = iter([httpx.Response(503), httpx.Response(200, json={
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16, "private": "secret"},
        "choices": [{"message": {"tool_calls": [{"function": {"name": "list_accounts", "arguments": '{}'}}]}}]})])
    with httpx.Client(transport=httpx.MockTransport(lambda request: next(responses))) as client:
        with collect() as attempts:
            select_live_tool(user_text="private-message", tool_name="list_accounts", expected_args={}, client=client)
        assert len(attempts) == 2
        assert [a["http_status"] for a in attempts] == [503, 200]
        assert attempts[1]["usage"]["total_tokens"] == 16
        assert "private" not in str(attempts) and "secret" not in str(attempts)
        with collect() as separate:
            assert separate == []
        assert len(attempts) == 2

def test_transport_failure_retained_without_exception_message():
    def fail(request):
        raise httpx.ConnectError("secret connection details")
    with httpx.Client(transport=httpx.MockTransport(fail)) as client, collect() as attempts:
        with pytest.raises(httpx.ConnectError):
            measured_post(client, "https://example.invalid")
    assert attempts[0]["error"] == "ConnectError"
    assert attempts[0]["seconds"] >= 0
    assert "secret" not in str(attempts)
