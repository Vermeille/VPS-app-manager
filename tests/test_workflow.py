"""Fake-DB workflow tests for Coolify links; real PG integration still required."""
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

import main


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, row):
        self.row = row
        self.events = []

    def execute(self, query, args=None):
        statement = query if isinstance(query, str) else "SQL statement"
        self.events.append((statement, args))
        if "WHERE name = %s" in statement and "SELECT" in statement:
            return FakeResult(dict(self.row))
        if "SELECT 1 FROM app_provisioner.managed_apps WHERE coolify_app_uuid" in statement:
            return FakeResult()
        if "SET coolify_app_uuid=%s" in statement:
            self.row["coolify_app_uuid"], self.row["coolify_env_key"] = args[:2]
        if "SET coolify_app_uuid=NULL" in statement:
            self.row["coolify_app_uuid"], self.row["coolify_env_key"] = None, None
        if "SET encrypted_password=" in statement:
            self.row["encrypted_password"] = args[0]
        return FakeResult()

    @contextmanager
    def transaction(self):
        yield


class FakeCoolify:
    events = []
    error_on_save = False
    error_on_deploy = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def require_application(self, uuid):
        self.events.append(("verified", uuid))
        return {"uuid": uuid}

    def set_env(self, uuid, key, value, *, replace=False):
        self.events.append(("set_env", uuid, key, value, replace))
        if self.error_on_save:
            raise main.CoolifyError("No permission", 502)
        return "updated" if replace else "created"

    def delete_env(self, uuid, key):
        self.events.append(("delete_env", uuid, key))
        return True

    def deploy(self, uuid):
        self.events.append(("deploy", uuid))
        if self.error_on_deploy:
            raise main.CoolifyError("Deployment rejected", 502)
        return True


@pytest.fixture
def setup(monkeypatch):
    row = dict(
        name="bookflix", database_name="bookflix_db", username="bookflix_user",
        encrypted_password=main.cipher.encrypt(b"original-secret").decode(),
        created_at=datetime.now(timezone.utc),
        coolify_app_uuid=None, coolify_env_key=None,
    )
    db = FakeConnection(row)
    @contextmanager
    def fake_admin(*, lock=False):
        yield db
    monkeypatch.setattr(main, "admin_connection", fake_admin)
    monkeypatch.setattr(main, "CoolifyClient", FakeCoolify)
    FakeCoolify.events = []
    FakeCoolify.error_on_save = False
    FakeCoolify.error_on_deploy = False
    return row, db


def test_link_and_queue_deploy(setup):
    row, db = setup
    result = main.link_coolify("bookflix", main.CoolifyLink(coolify_app_uuid="app123456", redeploy=True))
    assert result["env_action"] == "created"
    assert result["deployment_requested"]
    assert row["coolify_app_uuid"] == "app123456"
    assert [event[0] for event in FakeCoolify.events] == ["verified", "set_env", "deploy"]
    assert "original-secret" in FakeCoolify.events[1][3]


def test_link_failure_leaves_registry_unlinked(setup):
    row, _ = setup
    FakeCoolify.error_on_save = True
    with pytest.raises(HTTPException) as exc:
        main.link_coolify("bookflix", main.CoolifyLink(coolify_app_uuid="app123456"))
    assert exc.value.status_code == 502
    assert row["coolify_app_uuid"] is None


def test_linked_rotation_updates_coolify_before_postgresql(setup):
    row, db = setup
    row["coolify_app_uuid"] = "app123456"
    row["coolify_env_key"] = "DATABASE_URL"
    result = main.rotate_password("bookflix")
    assert result.password != "original-secret"
    assert result.redeploy_requested
    assert main.cipher.decrypt(row["encrypted_password"].encode()).decode() == result.password
    assert FakeCoolify.events[1][0] == "set_env"
    assert FakeCoolify.events[-1] == ("deploy", "app123456")


def test_deployment_failure_returns_warning_not_false_success(setup):
    row, _ = setup
    row["coolify_app_uuid"] = "app123456"
    row["coolify_env_key"] = "DATABASE_URL"
    FakeCoolify.error_on_deploy = True
    result = main.rotate_password("bookflix")
    assert result.warning is not None
    assert result.redeploy_requested is False


def test_unlink_removes_env_but_keeps_postgres(setup):
    row, db = setup
    row["coolify_app_uuid"] = "app123456"
    row["coolify_env_key"] = "DATABASE_URL"
    result = main.unlink_coolify("bookflix", main.CoolifyUnlink())
    assert result["unlinked"]
    assert row["coolify_app_uuid"] is None
    assert FakeCoolify.events == [("delete_env", "app123456", "DATABASE_URL")]


def test_link_custom_key_uses_exact_uuid(setup):
    row, db = setup
    result = main.link_coolify(
        "bookflix",
        main.CoolifyLink(coolify_app_uuid="app123456", env_key="POSTGRES_DSN", redeploy=False),
    )
    assert result["coolify_app_uuid"] == "app123456"
    assert result["env_key"] == "POSTGRES_DSN"
    assert row["coolify_env_key"] == "POSTGRES_DSN"
    assert FakeCoolify.events[1][:3] == ("set_env", "app123456", "POSTGRES_DSN")
