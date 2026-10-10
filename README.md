# PG App Manager · Coolify Edition

A small **FastAPI + PostgreSQL** web dashboard for provisioning one PostgreSQL database and user per app, and **optionally connecting them directly to existing Coolify applications**.

Instead of manually creating roles and copying passwords for Bookflix, Addiscord, VIP, etc., select a fully qualified **project / environment / application** target, create its database, and inject `DATABASE_URL` (or another key you choose) through the Coolify API. Optionally queue a deployment. You can still provision standalone databases with no Coolify integration at all.

## Features

- PostgreSQL database + dedicated login role + cryptographically random password per app.
- Registry table `app_provisioner.managed_apps` in the administrative PostgreSQL database. Passwords are **encrypted at rest** with Fernet.
- Browser dashboard and JSON API to create, list, reveal credentials, rotate, and delete database allocations.
- Coolify integration: **discover projects, their environments, and existing applications**, and render the **Project / Environment / Application** path (with domain/UUID as a disambiguator). Safely create/update `DATABASE_URL` (or a custom env key), and optionally queue a deployment.
- The dropdown submits the **exact Coolify application UUID**, so two applications with the same visible name in staging and production remain distinct. Apps whose environment cannot be resolved are disabled, not guessed.
- Configure the **environment-variable name before creating the database**, e.g. `DATABASE_URL`, `DB_URL`, `POSTGRES_DSN`, or `db.url`. The manager remembers the chosen name for sync and rotation.
- **Sync + deploy** an already linked database; optional automatic **secret update and redeploy on password rotation**.
- **Unlink** an application and remove its managed env variable; Coolify-linked databases cannot be deleted until unlinked.
- Coolify access happens exclusively on the FastAPI server. The Coolify token is **never returned to the browser**.

Not included: creating new Coolify app resources, cloning Git repositories, connecting services defined inside Docker Compose, or network provisioning. This targets Coolify's regular **Applications** API, not **Services** or their Compose subresources.

## Install on Coolify

1. Upload this directory into a Git repository, then create a Coolify **Application** from it using the included `Dockerfile`. Exposed port: **8000**.
2. Assign an HTTPS domain **and restrict access to your admin/VPN network**. This is a privileged administrator-only tool, not a public multi-user service.
3. Ensure the manager can connect to your **shared PostgreSQL instance**. Set **Connect to Predefined Network** or attach an appropriate private Docker network, and use the PostgreSQL resource's internal host.
4. Configure PostgreSQL/admin environment variables below. Keep secrets **runtime-only**, not Docker build arguments.
5. Optionally create an API token in Coolify under **Keys & Tokens → API tokens** with application read/write and deployment permissions. Add `COOLIFY_API_URL` and `COOLIFY_API_TOKEN` as runtime environment variables to PG App Manager.
6. Deploy PG App Manager, log in with `ADMIN_USERNAME` and `ADMIN_PASSWORD`, and select the target app in the **Connect to Coolify** selector when creating a database.

**Authentication:** PG App Manager already implements HTTP Basic for the dashboard and every management API endpoint (except `/health`). Configure a strong, unique `ADMIN_PASSWORD` over HTTPS and restrict ingress with VPN or an IP allowlist. Do **not** add an independent Coolify/Traefik Basic Auth password on top and expect two separate Basic logins to work: both layers use the same `Authorization` header. If you enable Coolify Basic Auth as an extra proxy check, use matching credentials and leave forwarding enabled; otherwise FastAPI will reject requests. For true SSO use a separately integrated identity proxy, and keep the application-side authorization model in mind.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `POSTGRES_ADMIN_DSN` | Yes | PostgreSQL admin connection to the database where the registry will live, usually `postgres` |
| `ADMIN_USERNAME` | No | HTTP Basic dashboard username; defaults to `admin` |
| `ADMIN_PASSWORD` | Yes | Long, random password for dashboard/API access |
| `FERNET_KEY` | Yes | Stable Fernet key used to encrypt registry passwords |
| `APP_DB_HOST` | Yes | **PostgreSQL hostname reachable from the Coolify application containers**, not just from PG App Manager |
| `APP_DB_PORT` | No | Defaults to `5432` |
| `COOLIFY_API_URL` | Optional | Full API root, such as `https://coolify.example.com/api/v1` |
| `COOLIFY_API_TOKEN` | Optional | Bearer API token. Set together with `COOLIFY_API_URL` |
| `COOLIFY_ALLOW_HTTP` | Optional | `true` *only* when connecting to a **trusted private** HTTP URL |

The complete sample is in `.env.example`.

Generate a key locally:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Back up `FERNET_KEY` separately. Never change it accidentally during deployments, since previously encrypted passwords would become unreadable. Do not expose it to application containers.

**Coolify API example:**

```dotenv
COOLIFY_API_URL=https://coolify.your-domain.example/api/v1
COOLIFY_API_TOKEN=<token from Coolify Keys & Tokens>
```

If Coolify is on a private network without HTTPS, its direct HTTP URL may work with `COOLIFY_ALLOW_HTTP=true`, **but only if that network is trusted**. HTTPS is preferred, including on private networks. Do not pass a token to arbitrary URLs, use a token with the minimum useful permissions, and **do not disable TLS certificate verification**.

### Networking caveat

`APP_DB_HOST` is embedded in the generated connection string that Coolify receives. Even if PG App Manager can reach PostgreSQL, Bookflix/Addiscord/etc. may not share its Docker network. Fix Coolify network connectivity before deploying apps. PostgreSQL itself should **not** expose port 5432 publicly.

Likewise, PG App Manager's container must be able to reach the configured Coolify API URL. Using the external HTTPS domain is often simpler than relying on an undocumented internal container hostname.

## Workflow

**New app**

1. Enter `bookflix` in PG App Manager.
2. Optionally choose `Personal / production / Bookflix` from the Coolify project/environment/application dropdown. Leave blank for a standalone DB. The actual target is the application's UUID.
3. Change the variable name if your app expects something else, e.g. `DB_URL` or `POSTGRES_DSN` (default: **`DATABASE_URL`**).
4. Optionally allow overwrite of an existing variable with that name (default: **no**), and choose whether to redeploy (default: **yes**).
5. Click **Create app**. PostgreSQL creates `bookflix_db`, `bookflix_user`, a random password and a registry entry. If linked, the manager calls Coolify to inject the chosen variable.
6. If requested, the Coolify API queues a deployment. **Queued does not mean healthy**. Inspect deployment status and app logs in Coolify.

**Existing database**

- Click **Connect Coolify** to attach it to an existing database; choose the fully qualified Coolify app and customize the env var key.
- Click **Sync + deploy** to refresh the Coolify env variable from the encrypted registry secret and queue a redeploy.
- Click **Rotate** to generate a new DB password. When linked, PG App Manager **updates Coolify before rotating PostgreSQL**, then requests a redeploy. If Coolify can't be updated, the existing PostgreSQL password is left alone. A deployment failure is reported for manual action.
- Click **Unlink** to remove the managed env variable from Coolify and clear the link. Unlinking **does not redeploy the app** and does not delete the database; it may lose DB connectivity on a future deploy.
- Click **Delete** only once unlinked. **Deletion is permanent** and removes the PostgreSQL database, role and registry entry.

**Important:** Database creation followed by Coolify linking is a two-step process. If the Coolify API fails, **the database remains created and registered**. Reopen PG App Manager and use **Connect Coolify** to retry. Similarly, PostgreSQL and Coolify have no distributed transaction, so an unexpected crash in the middle of synchronization may require manual inspection.

**Permissions:** The Coolify API token must be permitted to list **projects**, **environments** and **applications**, besides editing application environment variables. If project/environment discovery is blocked, the dropdown cannot populate fully qualified targets. Unresolvable apps are disabled rather than mislabeled.

**Scope:** The selected key is created/updated only on the chosen **application**. It does **not** modify shared project or environment variables, and does not target preview-only variables. The key must match Coolify's `[A-Za-z_][A-Za-z0-9_.]*` naming rule.

**Coolify services:** The integration intentionally only lists and updates regular Coolify **Applications**. Services and their managed Compose apps may expose different APIs and environment semantics. The provisioner still works in standalone mode for them.

## API

All routes except `/health` require HTTP Basic authentication.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/apps` | List managed PostgreSQL allocations, including linked Coolify UUID/key (no passwords) |
| `POST` | `/api/apps` | Create DB/role, `{"name":"bookflix"}` |
| `GET` | `/api/apps/bookflix/credentials` | Reveal PostgreSQL password and connection URL |
| `POST` | `/api/apps/bookflix/rotate` | Rotate password, synchronize linked Coolify app, request deploy |
| `DELETE` | `/api/apps/bookflix` | Permanently drop DB/role, `{"confirm":"bookflix"}`; refuses linked DBs |
| `GET` | `/api/coolify/status` | Show whether Coolify API is configured |
| `GET` | `/api/coolify/applications` | List Coolify apps with `project`, `environment`, `path`, `uuid`, `selectable` (sanitized fields only) |
| `POST` | `/api/apps/bookflix/coolify` | Link/sync an existing app and optional deploy |
| `POST` | `/api/apps/bookflix/coolify/unlink` | Remove managed Coolify env and unlink |
| `GET` | `/health` | HTTP liveness check |

Example to provision directly:

```sh
curl -u admin:'<your admin password>' -H 'Content-Type: application/json' \
  -d '{"name":"bookflix"}' https://pgmanager.example.com/api/apps
```

Example to attach to a Coolify app:

```sh
curl -u admin:'<your admin password>' -H 'Content-Type: application/json' \
  -d '{"coolify_app_uuid":"<actual Coolify app UUID>","env_key":"DATABASE_URL","replace_existing":false,"redeploy":true}' \
  https://pgmanager.example.com/api/apps/bookflix/coolify
```

Sync against a linked application again with the same endpoint and UUID/key. If changing either one, **unlink first**.

## Local run

```sh
cp .env.example .env
# Fill .env with actual values; omit COOLIFY_* if unused.
docker compose up --build
```

Open `http://127.0.0.1:8000` on the local host. Local Compose intentionally binds to loopback only. A separate PostgreSQL network must be connected manually.

## Security & operations

- **Highly privileged service:** holds the PostgreSQL admin DSN and a Coolify API token with deployment/environment-variable write access. Keep it behind VPN or strong network allowlisting in addition to HTTPS + HTTP Basic.
- **Secrets:** passwords are encrypted with Fernet and revealed to authenticated admins on explicit request. Coolify receives an app-specific secret with `is_shown_once=true`. Avoid exposing PostgreSQL admin credentials or the Fernet key to other apps.
- **No automatic overwrite:** linking refuses to replace an existing env variable unless explicitly authorized. Syncing the same managed binding is allowed.
- **Safe rotation order:** when linked, the Coolify variable is updated **before** the PostgreSQL password is changed. A failed PostgreSQL rotation attempts to restore the old URL in Coolify. Watch for any urgent warning and repair manually if recovery fails.
- **Deploy semantics:** Coolify's API queues deployments. It does not guarantee that the app starts successfully. A link can succeed even if deployment is not queued, and you'll be warned.
- **Backups:** take off-server backups of PostgreSQL data, the registry and the separate Fernet key. Rotation and deletion cannot replace backups.
- **Isolation:** per-app DB ownership is not a full security boundary. PostgreSQL is shared infrastructure.
- **API safety:** no arbitrary URL is accepted from users; outbound requests are limited to the administrator-provided Coolify API root. No redirects, TLS verification enabled, 10-second HTTP timeout, no logging of secret response bodies.
- **Versions:** PostgreSQL 13+ for `DROP DATABASE ... WITH (FORCE)`. This program runs one Uvicorn worker and uses PostgreSQL advisory locks for provisioning mutations.

## Tests

```sh
pip install -r requirements.txt pytest
PYTHONPATH=. python -m pytest -q tests
node --check static/app.js  # optional JS syntax check
```

The Coolify API tests use `httpx.MockTransport` and do not require a real Coolify instance. **Run a real end-to-end provisioning/linking test on your own VPS** before trusting password rotation in production.

### Coolify API reference

The integration calls these documented endpoints:

- [List projects](https://coolify.io/docs/api/endpoints/projects/list-projects)
- [List environments in a project](https://coolify.io/docs/api/endpoints/projects/get-environments)
- [List applications](https://coolify.io/docs/api/endpoints/applications/list-applications)
- [Get application](https://coolify.io/docs/api/endpoints/applications/get-application-by-uuid)
- [List environment variables](https://coolify.io/docs/api/endpoints/applications/list-envs-by-application-uuid)
- [Create environment variable](https://coolify.io/docs/api/endpoints/applications/create-env-by-application-uuid)
- [Update environment variable](https://coolify.io/docs/api/endpoints/applications/update-env-by-application-uuid)
- [Delete environment variable](https://coolify.io/docs/api/endpoints/applications/delete-env-by-application-uuid)
- [Deploy by UUID](https://coolify.io/docs/api/endpoints/deployments/deploy-by-tag-or-uuid)
