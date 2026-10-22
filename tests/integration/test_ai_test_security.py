from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import settings


@pytest.fixture()
def ai_test_client(monkeypatch):
    values = {
        "OPENAI_API_KEY": "stored-secret", "OPENAI_BASE_URL": "https://trusted.example/v1",
        "OPENAI_MODEL_NAME": "demo-model", "PROXY_URL": "",
    }
    monkeypatch.setattr(settings.env_manager, "get_value", lambda key, default=None: values.get(key, default))
    app = FastAPI()
    app.include_router(settings.router)
    return TestClient(app), values


@pytest.mark.parametrize("overrides", [
    {"OPENAI_BASE_URL": "https://untrusted.example/v1"},
    {"OPENAI_BASE_URL": "https://trusted.example/another-account"},
    {"PROXY_URL": "http://untrusted.example:8080"},
    {"OPENAI_BASE_URL": "https://trusted.example@untrusted.example/v1"},
])
def test_stored_key_cannot_be_forwarded_to_custom_destination(ai_test_client, monkeypatch, overrides):
    client, _ = ai_test_client
    connection = Mock(side_effect=AssertionError("No network call is allowed"))
    monkeypatch.setattr(settings, "_test_ai_connection", connection)
    response = client.post("/api/settings/ai/test", json=overrides)
    assert response.status_code == 400
    connection.assert_not_called()
    assert "stored-secret" not in response.text


def test_stored_key_can_only_use_normalized_configured_endpoint(ai_test_client, monkeypatch):
    client, _ = ai_test_client
    connection = Mock(return_value={"success": True})
    monkeypatch.setattr(settings, "_test_ai_connection", connection)
    assert client.post("/api/settings/ai/test", json={"OPENAI_BASE_URL": "https://TRUSTED.example:443/v1/"}).status_code == 200
    assert connection.call_args.args[0] == "stored-secret"
    assert client.post("/api/settings/ai/test", json={"OPENAI_BASE_URL": "http://127.0.0.1:8000/v1", "OPENAI_API_KEY": "explicit-test-key"}).status_code == 200
    assert connection.call_args.args[0] == "explicit-test-key"


def test_ai_test_does_not_follow_redirects_or_expose_keys(ai_test_client, monkeypatch):
    client, _ = ai_test_client
    requests = []
    real_client = httpx.Client

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://untrusted.example/steal"})

    class TransportClient(real_client):
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    # Keep the TestClient HTTP transport intact; only patch outgoing client construction.
    monkeypatch.setattr(httpx, "Client", TransportClient)
    response = client.post("/api/settings/ai/test", json={})
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert len(requests) == 1
    assert requests[0].url.host == "trusted.example"
    assert requests[0].headers["authorization"] == "Bearer stored-secret"
    assert "stored-secret" not in response.text
