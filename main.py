"""Tiny PostgreSQL app/database provisioner for a private Coolify installation."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from coolify import CoolifyClient, CoolifyError, configured as coolify_configured

logger = logging.getLogger("pg_app_manager")
HERE = Path(__file__).resolve().parent
LOCK_ID = 477_737_942_106  # Serialize all provisioning mutations on this PostgreSQL server.

ADMIN_DSN = os.getenv("POSTGRES_ADMIN_DSN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_DB_HOST = os.getenv("APP_DB_HOST", "")
APP_DB_PORT = int(os.getenv("APP_DB_PORT", "5432"))
FERNET_KEY = os.getenv("FERNET_KEY", "")

if not (ADMIN_DSN and ADMIN_PASSWORD and APP_DB_HOST and FERNET_KEY):
    raise RuntimeError(
        "Set POSTGRES_ADMIN_DSN, ADMIN_PASSWORD, APP_DB_HOST and FERNET_KEY. "
        "See README.md."
    )

cipher = Fernet(FERNET_KEY.encode())
security = HTTPBasic()


class AppCreate(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=40,
        pattern=r"^[a-z][a-z0-9-]*$",
        description="Lowercase slug, e.g. bookflix or my-app.",
    )


class DeleteConfirmation(BaseModel):
    confirm: str


class CoolifyLink(BaseModel):
    coolify_app_uuid: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    env_key: str = Field(default="DATABASE_URL", min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    replace_existing: bool = False
    redeploy: bool = False


class CoolifyUnlink(BaseModel):
    remove_env: bool = True
    redeploy: bool = False


class AppInfo(BaseModel):
    name: str
    database: str
    username: str
    created_at: datetime
    coolify_app_uuid: str | None = None
    coolify_env_key: str | None = None


class AppCredentials(AppInfo):
    password: str
    database_url: str
    host: str
    port: int
    warning: str | None = None
    redeploy_requested: bool = False


def authenticate(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> None:
    valid_user = secrets.compare_digest(
        credentials.username.encode(), ADMIN_USERNAME.encode()
    )
    valid_password = secrets.compare_digest(
        credentials.password.encode(), ADMIN_PASSWORD.encode()
    )
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="PG App Manager"'},
        )


Auth = Annotated[None, Depends(authenticate)]


@contextmanager
def admin_connection(*, lock: bool = False):
    # CREATE/DROP DATABASE cannot run inside a transaction.
    with psycopg.connect(ADMIN_DSN, autocommit=True, row_factory=dict_row) as conn:
        if lock:
            conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            yield conn
        finally:
            if lock:
                conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


def bootstrap() -> None:
    with admin_connection() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS app_provisioner")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_provisioner.managed_apps (
                name TEXT PRIMARY KEY,
                database_name TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                encrypted_password TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                coolify_app_uuid TEXT UNIQUE,
                coolify_env_key TEXT
            )
            """
        )
        # In-place migration for installations created with v1.
        conn.execute("ALTER TABLE app_provisioner.managed_apps ADD COLUMN IF NOT EXISTS coolify_app_uuid TEXT")
        conn.execute("ALTER TABLE app_provisioner.managed_apps ADD COLUMN IF NOT EXISTS coolify_env_key TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS managed_apps_coolify_uuid_idx ON app_provisioner.managed_apps (coolify_app_uuid) WHERE coolify_app_uuid IS NOT NULL")


def db_names(name: str) -> tuple[str, str]:
    suffix = name.replace("-", "_")
    return f"{suffix}_db", f"{suffix}_user"


def connection_url(username: str, password: str, database: str) -> str:
    return (
        f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{APP_DB_HOST}:{APP_DB_PORT}/{quote(database, safe='')}"
    )


def to_credentials(row: dict, password: str) -> AppCredentials:
    return AppCredentials(
        name=row["name"],
        database=row["database_name"],
        username=row["username"],
        created_at=row["created_at"],
        password=password,
        database_url=connection_url(row["username"], password, row["database_name"]),
        host=APP_DB_HOST,
        port=APP_DB_PORT,
        coolify_app_uuid=row.get("coolify_app_uuid"),
        coolify_env_key=row.get("coolify_env_key"),
    )


def require_managed(conn, name: str) -> dict:
    row = conn.execute(
        """SELECT name, database_name, username, encrypted_password, created_at
           , coolify_app_uuid, coolify_env_key
           FROM app_provisioner.managed_apps WHERE name = %s""",
        (name,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown app")
    return row


def safe_database_error(action: str, exc: Exception) -> HTTPException:
    # Never log queries/exceptions: CREATE/ALTER ROLE statements contain passwords.
    logger.error("PostgreSQL %s failed: %s", action, type(exc).__name__)
    return HTTPException(
        status_code=503,
        detail=f"PostgreSQL {action} failed. Check DB privileges and server logs.",
    )


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    bootstrap()
    yield


app = FastAPI(
    title="PG App Manager",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=app_lifespan,
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    # JSON API is same-origin and requires credentials on every request.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlsplit

            origin_parts = urlsplit(origin)
            expected_host = request.headers.get("host", "")
            if origin_parts.netloc.lower() != expected_host.lower():
                return JSONResponse({"detail": "Cross-origin request forbidden"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def home(_auth: Auth):
    return FileResponse(HERE / "static" / "index.html")


@app.get("/app.js", include_in_schema=False)
def js(_auth: Auth):
    return FileResponse(HERE / "static" / "app.js", media_type="text/javascript")


@app.get("/style.css", include_in_schema=False)
def styles(_auth: Auth):
    return FileResponse(HERE / "static" / "style.css", media_type="text/css")


@app.get("/api/apps", response_model=list[AppInfo])
def list_apps(_auth: Auth):
    try:
        with admin_connection() as conn:
            rows = conn.execute(
                """SELECT name, database_name AS database, username, created_at,
                          coolify_app_uuid, coolify_env_key
                   FROM app_provisioner.managed_apps ORDER BY name"""
            ).fetchall()
            return rows
    except psycopg.Error as exc:
        raise safe_database_error("listing", exc) from None


@app.get("/api/coolify/status")
def coolify_status(_auth: Auth):
    return {"configured": coolify_configured()}


@app.get("/api/coolify/applications")
def list_coolify_apps(_auth: Auth):
    try:
        with CoolifyClient() as client:
            return client.applications()
    except CoolifyError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@app.post("/api/apps/{name}/coolify")
def link_coolify(name: str, payload: CoolifyLink, _auth: Auth):
    try:
        with admin_connection(lock=True) as conn:
            row = require_managed(conn, name)
            current_uuid = row["coolify_app_uuid"]
            if current_uuid and (current_uuid != payload.coolify_app_uuid or row["coolify_env_key"] != payload.env_key):
                raise HTTPException(409, "Already linked with another Coolify app or variable. Unlink first")
            if conn.execute(
                "SELECT 1 FROM app_provisioner.managed_apps WHERE coolify_app_uuid = %s AND name <> %s",
                (payload.coolify_app_uuid, name),
            ).fetchone():
                raise HTTPException(409, "This Coolify application is already linked to another database")
            password = cipher.decrypt(row["encrypted_password"].encode()).decode()
            url = connection_url(row["username"], password, row["database_name"])
            with CoolifyClient() as client:
                client.require_application(payload.coolify_app_uuid)
                already_managed = current_uuid == payload.coolify_app_uuid and row["coolify_env_key"] == payload.env_key
                outcome = client.set_env(
                    payload.coolify_app_uuid, payload.env_key, url,
                    replace=payload.replace_existing or already_managed,
                )
                # Persist only after a confirmed Coolify API write.
                conn.execute(
                    """UPDATE app_provisioner.managed_apps
                       SET coolify_app_uuid=%s, coolify_env_key=%s WHERE name=%s""",
                    (payload.coolify_app_uuid, payload.env_key, name),
                )
                warning = None
                deployment_requested = False
                if payload.redeploy:
                    try:
                        client.deploy(payload.coolify_app_uuid)
                        deployment_requested = True
                    except CoolifyError:
                        warning = "Environment saved, but deployment was not queued. Deploy manually in Coolify."
                return {"name": name, "coolify_app_uuid": payload.coolify_app_uuid,
                        "env_key": payload.env_key, "env_action": outcome,
                        "deployment_requested": deployment_requested, "warning": warning}
    except (CoolifyError, InvalidToken) as exc:
        if isinstance(exc, InvalidToken):
            raise HTTPException(500, "Cannot decrypt credentials: check FERNET_KEY") from None
        raise HTTPException(exc.status_code, str(exc)) from None
    except HTTPException:
        raise
    except psycopg.Error as exc:
        raise safe_database_error("linking Coolify application", exc) from None


@app.post("/api/apps/{name}/coolify/unlink")
def unlink_coolify(name: str, payload: CoolifyUnlink, _auth: Auth):
    if payload.redeploy and not payload.remove_env:
        raise HTTPException(400, "Redeploy requires removing the managed environment variable")
    try:
        with admin_connection(lock=True) as conn:
            row = require_managed(conn, name)
            uuid, key = row["coolify_app_uuid"], row["coolify_env_key"]
            if not uuid:
                raise HTTPException(409, "This database is not linked to Coolify")
            warning = None
            deployment_requested = False
            if payload.remove_env:
                with CoolifyClient() as client:
                    client.delete_env(uuid, key)
                    # Only clear the registry after the variable was deleted or was absent.
                    conn.execute(
                        "UPDATE app_provisioner.managed_apps SET coolify_app_uuid=NULL, coolify_env_key=NULL WHERE name=%s",
                        (name,),
                    )
                    if payload.redeploy:
                        try:
                            client.deploy(uuid)
                            deployment_requested = True
                        except CoolifyError:
                            warning = "Variable removed, but deployment was not queued. Deploy manually in Coolify."
            else:
                conn.execute(
                    "UPDATE app_provisioner.managed_apps SET coolify_app_uuid=NULL, coolify_env_key=NULL WHERE name=%s",
                    (name,),
                )
            return {"name": name, "unlinked": True, "deployment_requested": deployment_requested,
                    "warning": warning}
    except CoolifyError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except HTTPException:
        raise
    except psycopg.Error as exc:
        raise safe_database_error("unlinking Coolify application", exc) from None


@app.post("/api/apps", response_model=AppCredentials, status_code=201)
def create_app(payload: AppCreate, _auth: Auth):
    name = payload.name
    db_name, username = db_names(name)
    password = secrets.token_urlsafe(32)
    password_encrypted = cipher.encrypt(password.encode()).decode()
    created_role = False
    created_db = False
    try:
        with admin_connection(lock=True) as conn:
            # Never adopt or modify an existing database or role not created by us.
            if conn.execute(
                "SELECT 1 FROM app_provisioner.managed_apps WHERE name = %s", (name,)
            ).fetchone():
                raise HTTPException(409, "App already exists")
            if conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (username,)
            ).fetchone() or conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
            ).fetchone():
                raise HTTPException(409, "Database or role name is already taken")

            try:
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(username), sql.Literal(password)
                    )
                )
                created_role = True
                conn.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(db_name), sql.Identifier(username)
                    )
                )
                created_db = True
                conn.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(db_name)
                    )
                )
                row = conn.execute(
                    """INSERT INTO app_provisioner.managed_apps
                       (name, database_name, username, encrypted_password)
                       VALUES (%s, %s, %s, %s)
                       RETURNING name, database_name, username, created_at""",
                    (name, db_name, username, password_encrypted),
                ).fetchone()
            except Exception:
                # Best-effort cleanup if provisioning failed partway through.
                if created_db:
                    try:
                        conn.execute(
                            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                                sql.Identifier(db_name)
                            )
                        )
                    except psycopg.Error:
                        logger.error("Could not clean up database after failed provisioning")
                if created_role:
                    try:
                        conn.execute(
                            sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(username))
                        )
                    except psycopg.Error:
                        logger.error("Could not clean up role after failed provisioning")
                raise
            return to_credentials(row, password)
    except HTTPException:
        raise
    except psycopg.Error as exc:
        raise safe_database_error("provisioning", exc) from None


@app.get("/api/apps/{name}/credentials", response_model=AppCredentials)
def reveal_credentials(name: str, _auth: Auth):
    try:
        with admin_connection() as conn:
            row = require_managed(conn, name)
        password = cipher.decrypt(row["encrypted_password"].encode()).decode()
        return to_credentials(row, password)
    except psycopg.Error as exc:
        raise safe_database_error("reading credentials", exc) from None
    except InvalidToken:
        raise HTTPException(500, "Cannot decrypt credentials: check FERNET_KEY") from None


@app.post("/api/apps/{name}/rotate", response_model=AppCredentials)
def rotate_password(name: str, _auth: Auth):
    new_password = secrets.token_urlsafe(32)
    encrypted = cipher.encrypt(new_password.encode()).decode()
    try:
        with admin_connection(lock=True) as conn:
            row = require_managed(conn, name)
            uuid, key = row["coolify_app_uuid"], row["coolify_env_key"]
            if uuid:
                # Update Coolify before changing PostgreSQL, never the other way around:
                # a failed Coolify request must not invalidate the running app's credentials.
                previous_password = cipher.decrypt(row["encrypted_password"].encode()).decode()
                old_url = connection_url(row["username"], previous_password, row["database_name"])
                new_url = connection_url(row["username"], new_password, row["database_name"])
                with CoolifyClient() as client:
                    client.require_application(uuid)
                    client.set_env(uuid, key, new_url, replace=True)
                    try:
                        with conn.transaction():
                            conn.execute(
                                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                                    sql.Identifier(row["username"]), sql.Literal(new_password)
                                )
                            )
                            conn.execute(
                                "UPDATE app_provisioner.managed_apps SET encrypted_password=%s WHERE name=%s",
                                (encrypted, name),
                            )
                    except psycopg.Error:
                        try:
                            client.set_env(uuid, key, old_url, replace=True)
                        except CoolifyError:
                            raise HTTPException(503, "Database rotation failed and restoring Coolify credentials also failed. Check both immediately") from None
                        raise
                    # Deployment is not atomic with credential rotation. Make failure explicit.
                    warning = None
                    deployed = False
                    try:
                        client.deploy(uuid)
                        deployed = True
                    except CoolifyError:
                        warning = "Password rotated and Coolify secret updated, but redeploy was not queued. Redeploy the application manually NOW."
                    result = to_credentials(row, new_password)
                    result.warning = warning
                    result.redeploy_requested = deployed
                    return result
            else:
                with conn.transaction():
                    conn.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                            sql.Identifier(row["username"]), sql.Literal(new_password)
                        )
                    )
                    conn.execute(
                        "UPDATE app_provisioner.managed_apps SET encrypted_password=%s WHERE name=%s",
                        (encrypted, name),
                    )
                return to_credentials(row, new_password)
    except CoolifyError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except InvalidToken:
        raise HTTPException(500, "Cannot decrypt credentials: check FERNET_KEY") from None
    except HTTPException:
        raise
    except psycopg.Error as exc:
        raise safe_database_error("password rotation", exc) from None


@app.delete("/api/apps/{name}")
def delete_app(name: str, payload: DeleteConfirmation, _auth: Auth):
    if payload.confirm != name:
        raise HTTPException(status_code=400, detail="Confirmation does not match app name")
    try:
        with admin_connection(lock=True) as conn:
            row = require_managed(conn, name)
            if row["coolify_app_uuid"]:
                raise HTTPException(409, "Unlink this app from Coolify before deleting its database")
            # Irreversible! PostgreSQL 13+ required for WITH (FORCE).
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(row["database_name"])
                )
            )
            conn.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(row["username"]))
            )
            conn.execute(
                "DELETE FROM app_provisioner.managed_apps WHERE name = %s", (name,)
            )
            return {"deleted": name}
    except HTTPException:
        raise
    except psycopg.Error as exc:
        raise safe_database_error("deletion", exc) from None
