"""Small, secret-safe adapter for Coolify's documented API v1."""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("pg_app_manager.coolify")


class CoolifyError(Exception):
    """Sanitized exception: never includes request bodies, URLs or secrets."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def configured() -> bool:
    return bool(os.getenv("COOLIFY_API_URL", "") and os.getenv("COOLIFY_API_TOKEN", ""))


def validate_api_url(raw: str) -> str:
    """Use an administrator-configured endpoint; never accept URLs from API callers."""
    raw = raw.rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("COOLIFY_API_URL must be a valid http(s) API root")
    # HTTP is permitted explicitly for private internal networks only.
    if parts.scheme == "http" and os.getenv("COOLIFY_ALLOW_HTTP", "false").lower() != "true":
        raise ValueError("Use HTTPS for Coolify, or explicitly set COOLIFY_ALLOW_HTTP=true for a trusted private network")
    if not parts.path.endswith("/api/v1"):
        raise ValueError("COOLIFY_API_URL must end with /api/v1")
    return raw


class CoolifyClient:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        if not configured():
            raise CoolifyError("Coolify integration is not configured", 503)
        try:
            api_url = validate_api_url(os.environ["COOLIFY_API_URL"])
        except ValueError as exc:
            raise CoolifyError(str(exc), 503) from None
        self._client = httpx.Client(
            base_url=api_url + "/",
            headers={"Authorization": "Bearer " + os.environ["COOLIFY_API_TOKEN"], "Accept": "application/json"},
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._client.close()

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = self._client.request(method, path.lstrip("/"), **kwargs)
        except httpx.RequestError:
            logger.warning("Coolify request failed: connection or timeout (%s)", method)
            raise CoolifyError("Cannot reach Coolify. Check COOLIFY_API_URL and networking") from None
        if response.status_code in {301, 302, 303, 307, 308}:
            raise CoolifyError("Coolify returned a redirect. Configure the direct API URL")
        if response.status_code == 401:
            raise CoolifyError("Coolify rejected the API token (401)", 502)
        if response.status_code == 403:
            raise CoolifyError("Coolify token lacks permission for this operation (403)", 502)
        if response.status_code == 404:
            raise CoolifyError("Coolify resource was not found (404)", 404)
        if response.status_code >= 400:
            # Never expose response body: Coolify may include credentials in errors.
            logger.warning("Coolify API returned HTTP %s for %s", response.status_code, method)
            raise CoolifyError(f"Coolify API returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise CoolifyError("Coolify returned invalid JSON") from None

    def applications(self) -> list[dict]:
        """Resolve project/environment/app identities using Coolify's numeric environment_id.

        Applications are globally identified by UUID, not display name. Unresolvable
        resources are returned as unavailable so the UI cannot claim a false path.
        """
        projects = self._request("GET", "projects")
        if not isinstance(projects, list):
            raise CoolifyError("Unexpected projects response from Coolify")
        env_by_id: dict[str, dict | None] = {}
        for project in projects:
            if not isinstance(project, dict) or not isinstance(project.get("uuid"), str):
                continue
            project_uuid = project["uuid"]
            project_name = str(project.get("name") or project_uuid)
            envs = self._request("GET", f"projects/{project_uuid}/environments")
            if not isinstance(envs, list):
                raise CoolifyError("Unexpected project environments response from Coolify")
            for env in envs:
                if not isinstance(env, dict) or env.get("id") is None:
                    continue
                env_id = str(env["id"])
                identity = {
                    "project": project_name,
                    "project_uuid": project_uuid,
                    "environment": str(env.get("name") or env.get("uuid") or env_id),
                    "environment_uuid": env.get("uuid") if isinstance(env.get("uuid"), str) else None,
                }
                # If Coolify returns an inconsistent repeated environment ID,
                # refuse to assign either identity to that application.
                if env_id in env_by_id and env_by_id[env_id] != identity:
                    env_by_id[env_id] = None
                elif env_id not in env_by_id:
                    env_by_id[env_id] = identity

        apps = self._request("GET", "applications")
        if not isinstance(apps, list):
            raise CoolifyError("Unexpected applications response from Coolify")
        result = []
        for app in apps:
            if not isinstance(app, dict) or not isinstance(app.get("uuid"), str):
                continue
            uuid = app["uuid"]
            name = str(app.get("name") or uuid)
            env_id = app.get("environment_id")
            identity = env_by_id.get(str(env_id)) if env_id is not None else None
            result.append({
                "uuid": uuid,
                "name": name,
                "status": str(app.get("status") or "unknown"),
                "fqdn": str(app.get("fqdn") or ""),
                "environment_id": env_id,
                "project": identity["project"] if identity else None,
                "project_uuid": identity["project_uuid"] if identity else None,
                "environment": identity["environment"] if identity else None,
                "environment_uuid": identity["environment_uuid"] if identity else None,
                "path": f"{identity['project']} / {identity['environment']} / {name}" if identity else None,
                "selectable": bool(identity),
            })
        return sorted(result, key=lambda a: ((a["path"] or "~").casefold(), a["uuid"]))

    def require_application(self, uuid: str) -> dict:
        # Confirm chosen resource is actually reachable with this token.
        app = self._request("GET", f"applications/{uuid}")
        if not isinstance(app, dict) or app.get("uuid") != uuid:
            raise CoolifyError("Coolify application UUID was not verified")
        return app

    def environment(self, uuid: str) -> list[dict]:
        vars_ = self._request("GET", f"applications/{uuid}/envs")
        if not isinstance(vars_, list):
            raise CoolifyError("Unexpected environment-variable response from Coolify")
        return [env for env in vars_ if isinstance(env, dict)]

    def find_env(self, uuid: str, key: str) -> dict | None:
        vars_ = [e for e in self.environment(uuid) if e.get("key") == key and not e.get("is_preview", False)]
        if len(vars_) > 1:
            raise CoolifyError(f"Multiple runtime variables named {key} exist; resolve them in Coolify first", 409)
        return vars_[0] if vars_ else None

    def set_env(self, uuid: str, key: str, value: str, *, replace: bool = False) -> str:
        current = self.find_env(uuid, key)
        if current and not replace:
            raise CoolifyError(f"{key} already exists in Coolify. Explicitly allow replacement to continue", 409)
        payload = {"key": key, "value": value, "is_preview": False, "is_literal": True, "is_shown_once": True}
        if current:
            self._request("PATCH", f"applications/{uuid}/envs", json=payload)
            return "updated"
        self._request("POST", f"applications/{uuid}/envs", json=payload)
        return "created"

    def delete_env(self, uuid: str, key: str) -> bool:
        existing = self.find_env(uuid, key)
        if existing is None:
            return False
        env_uuid = existing.get("uuid")
        if not isinstance(env_uuid, str) or not env_uuid:
            raise CoolifyError("Coolify did not return the env UUID needed to remove it")
        self._request("DELETE", f"applications/{uuid}/envs/{env_uuid}")
        return True

    def deploy(self, uuid: str) -> bool:
        response = self._request("POST", "deploy", params={"uuid": uuid, "force": "false"})
        if not isinstance(response, dict) or not response.get("deployments"):
            raise CoolifyError("Coolify did not confirm a queued deployment")
        return True
