"""HTTP contract tests: no live Coolify account needed."""
import json

import httpx
import pytest

from coolify import CoolifyClient, CoolifyError, configured, validate_api_url


@pytest.fixture(autouse=True)
def coolify_env(monkeypatch):
    monkeypatch.setenv("COOLIFY_API_URL", "https://coolify.example.com/api/v1")
    monkeypatch.setenv("COOLIFY_API_TOKEN", "fake-token-never-log")
    monkeypatch.delenv("COOLIFY_ALLOW_HTTP", raising=False)


def test_settings(monkeypatch):
    assert configured()
    assert validate_api_url("https://coolify.example.com/api/v1/") == "https://coolify.example.com/api/v1"
    for bad in ["http://coolify.example.com/api/v1", "https://a.example.com", "https://u:p@a.example.com/api/v1", "https://a.example.com/api/v1?access_token=a"]:
        with pytest.raises(ValueError):
            validate_api_url(bad)
    monkeypatch.setenv("COOLIFY_ALLOW_HTTP", "true")
    assert validate_api_url("http://coolify:8000/api/v1") == "http://coolify:8000/api/v1"


def test_discovery_and_auth():
    """Same app name in different projects/environments must not be conflated."""
    recorded = []
    def handler(request):
        recorded.append(request.url.path)
        assert request.headers.get("authorization") == "Bearer fake-token-never-log"
        paths = {
            "/api/v1/projects": [
                {"id": 1, "uuid": "myproject", "name": "Personal"},
                {"id": 2, "uuid": "business", "name": "Company"},
            ],
            "/api/v1/projects/myproject/environments": [
                {"id": 100, "name": "production", "project_id": 1},
                {"id": 101, "name": "staging", "project_id": 1},
            ],
            "/api/v1/projects/business/environments": [
                {"id": 102, "name": "production", "project_id": 2},
            ],
            "/api/v1/applications": [
                {"uuid": "b0000001", "name": "bookflix", "status": "running", "fqdn": "https://bookflix.test", "environment_id": 100, "secret": "DO_NOT_RETURN"},
                {"uuid": "b0000002", "name": "bookflix", "status": "stopped", "environment_id": 101},
                {"uuid": "b0000003", "name": "bookflix", "environment_id": 102},
                {"uuid": "b0000004", "name": "orphaned", "environment_id": 888},
            ],
        }
        return httpx.Response(200, json=paths[request.url.path])

    with CoolifyClient(transport=httpx.MockTransport(handler)) as client:
        result = client.applications()
    resolved = {app["uuid"]: app for app in result}
    assert resolved["b0000001"]["path"] == "Personal / production / bookflix"
    assert resolved["b0000002"]["path"] == "Personal / staging / bookflix"
    assert resolved["b0000003"]["path"] == "Company / production / bookflix"
    assert resolved["b0000001"]["project_uuid"] == "myproject"
    assert resolved["b0000001"]["environment_id"] == 100
    assert resolved["b0000001"]["selectable"] is True
    assert resolved["b0000004"]["selectable"] is False
    assert resolved["b0000004"]["path"] is None
    assert all("secret" not in app for app in result)
    assert recorded == [
        "/api/v1/projects", "/api/v1/projects/myproject/environments",
        "/api/v1/projects/business/environments", "/api/v1/applications",
    ]


def test_ambiguous_environment_id_not_assigned_to_wrong_project():
    def handler(request):
        path = request.url.path
        if path == "/api/v1/projects":
            return httpx.Response(200, json=[
                {"uuid": "p1", "name": "One"}, {"uuid": "p2", "name": "Two"},
            ])
        if path == "/api/v1/projects/p1/environments":
            return httpx.Response(200, json=[{"id": 55, "name": "production"}])
        if path == "/api/v1/projects/p2/environments":
            return httpx.Response(200, json=[{"id": 55, "name": "staging"}])
        return httpx.Response(200, json=[{"uuid": "app-12345", "name": "api", "environment_id": 55}])
    with CoolifyClient(transport=httpx.MockTransport(handler)) as client:
        app = client.applications()[0]
    assert app["selectable"] is False
    assert app["path"] is None


def test_create_env_without_overwrite():
    recorded = []
    def handler(request):
        recorded.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"uuid": "new-env-uuid"})
    with CoolifyClient(transport=httpx.MockTransport(handler)) as client:
        assert client.set_env("abcd1234", "DATABASE_URL", "postgresql://example") == "created"
    assert recorded == [
        ("GET", "/api/v1/applications/abcd1234/envs", None),
        ("POST", "/api/v1/applications/abcd1234/envs", {"key": "DATABASE_URL", "value": "postgresql://example", "is_preview": False, "is_literal": True, "is_shown_once": True}),
    ]


def test_refuse_overwrite_without_permission():
    methods = []
    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json=[{"uuid": "env234", "key": "DATABASE_URL", "is_preview": False, "value": "****"}])
    with CoolifyClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CoolifyError, match="already exists") as error:
            client.set_env("abcd1234", "DATABASE_URL", "new-url", replace=False)
    assert error.value.status_code == 409
    assert methods == ["GET"]


def test_replace_hidden_secret_and_deploy():
    events = []
    def handler(request):
        events.append((request.method, request.url.path, dict(request.url.params)))
        if request.method == "GET":
            return httpx.Response(200, json=[{"uuid": "env234", "key": "DATABASE_URL", "is_preview": False, "value": "*******"}])
        if request.method == "PATCH":
            assert json.loads(request.content)["value"] == "new-secret-url"
            return httpx.Response(201, json={"uuid": "env234"})
        return httpx.Response(200, json={"deployments": [{"deployment_uuid": "queue456"}]})
    with CoolifyClient(transport=httpx.MockTransport(handler)) as client:
        assert client.set_env("abcd1234", "DATABASE_URL", "new-secret-url", replace=True) == "updated"
        assert client.deploy("abcd1234")
    assert events[-1] == ("POST", "/api/v1/deploy", {"uuid": "abcd1234", "force": "false"})


def test_unlink_removes_managed_env_variable():
    events = []
    def handler(request):
        events.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[{"uuid": "env-uuid", "key": "DATABASE_URL", "is_preview": False}])
        return httpx.Response(200, json={"message": "deleted"})
    with CoolifyClient(transport=httpx.MockTransport(handler)) as client:
        assert client.delete_env("abcd1234", "DATABASE_URL")
    assert events[-1] == ("DELETE", "/api/v1/applications/abcd1234/envs/env-uuid")


def test_error_redacts_remote_response():
    def handler(request):
        return httpx.Response(500, json={"detail": "leaked-password-or-token"})
    with CoolifyClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CoolifyError) as error:
            client.applications()
    assert "leaked-password-or-token" not in str(error.value)
    assert "500" in str(error.value)


def test_does_not_follow_redirects():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://evil.example"})
    with CoolifyClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CoolifyError, match="redirect"):
            client.applications()


def test_not_configured(monkeypatch):
    monkeypatch.delenv("COOLIFY_API_TOKEN")
    assert not configured()
    with pytest.raises(CoolifyError, match="not configured"):
        CoolifyClient()
