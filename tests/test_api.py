"""Run with pytest after installing requirements.txt + httpx + pytest."""

import os

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError

os.environ.setdefault("POSTGRES_ADMIN_DSN", "postgresql://postgres:secret@db/postgres")
os.environ.setdefault("APP_DB_HOST", "pg-internal")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

from main import AppCreate, CoolifyLink, app, connection_url, db_names  # noqa: E402


def test_names():
    assert db_names("bookflix") == ("bookflix_db", "bookflix_user")
    assert db_names("my-app") == ("my_app_db", "my_app_user")
    for invalid in ["BadCase", "a_b", "a;DROP", "", "a" * 41]:
        with pytest.raises(ValidationError):
            AppCreate(name=invalid)


def test_url_encodes_password():
    assert connection_url("some_user", "a@b/c", "my_db") == (
        "postgresql://some_user:a%40b%2Fc@pg-internal:5432/my_db"
    )


def test_authentication_delegated_to_coolify_proxy():
    # The FastAPI application deliberately has no authentication layer.
    # In production the Coolify reverse proxy MUST require authentication.
    client = TestClient(app)
    for path in ["/", "/app.js", "/style.css", "/api/coolify/status"]:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert "WWW-Authenticate" not in response.headers


def test_health_unprotected():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_post_body_validation_before_db_connect():
    client = TestClient(app)
    response = client.post(
        "/api/apps",
        json={"name": "INVALID!"},
    )
    assert response.status_code == 422


def test_same_origin_protection():
    client = TestClient(app, base_url="https://db.example.com")
    response = client.post(
        "/api/apps",
        headers={"Origin": "https://evil.example.com"},
        json={"name": "bookflix"},
    )
    assert response.status_code == 403


def test_custom_env_key_validation():
    assert CoolifyLink(coolify_app_uuid="app123456", env_key="POSTGRES_DSN").env_key == "POSTGRES_DSN"
    assert CoolifyLink(coolify_app_uuid="app123456", env_key="db.url").env_key == "db.url"
    assert CoolifyLink(coolify_app_uuid="app123456", env_key="dbUrl").env_key == "dbUrl"
    for invalid in ["", "9BAD", "BAD-KEY", "FOO=BAR", "APP PASSWORD", "a" * 129]:
        with pytest.raises(ValidationError):
            CoolifyLink(coolify_app_uuid="app123456", env_key=invalid)
