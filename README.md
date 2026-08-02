# WebMARS Users Service v0.3.0

Small Flask backend for WebMARS user accounts and project storage. It is designed for small text-based workspaces and stores each project as one JSON document inside SQLite.

Password hashing now uses Werkzeug `scrypt` for new passwords. Older `bcrypt` hashes are still accepted and are upgraded automatically on the next successful login, using the native `bcrypt` package to keep legacy logins fast.

Runtime dependencies are pinned in `requirements.txt`; update them deliberately
and rerun the complete test suite instead of allowing a deployment months later
to resolve a different Flask/Werkzeug combination.

## v0.3.0

- startup requires an explicit `WEBMARS_USERS_ENV`; production can no longer silently inherit development mode
- `PUT` and `DELETE` require `If-Match` and return `428` when the caller did not identify the version it read
- version checks are part of the SQL mutation, closing the check-then-write race between concurrent workers
- `GET /api/projects?include=documents` batches documents and ETags for the webMARS sync view
- SQLite uses WAL, a busy timeout and normal synchronous mode for concurrent Gunicorn workers

## v0.2.0

- production refuses to start while `SECRET_KEY` is the development placeholder
- request bodies are capped, so a project document cannot be used to fill the disk
- repeated failed sign-ins from one address are turned away with `429`
- project names are unique per user; a clash answers `409`
- `GET /api/projects/<id>` returns an `ETag`, and `PUT` honours `If-Match` with `412`
- `GET /api/projects` reports `serverTime` so clients can correct for clock skew

### Upgrading from v0.1.1

Set `SECRET_KEY` before restarting in production; the service now refuses to boot
without it rather than signing sessions with a key that is public in this
repository. Everything else migrates on first start: the schema is replayed
(it is written with `IF NOT EXISTS`), and any pre-existing duplicate project
names are renamed to `name (2)`, `name (3)` and so on before the unique index is
created. No project is ever deleted by the migration.

## v0.1.1

- persistent authenticated sessions for up to 365 days
- session restoration across page refreshes and browser restarts
- immediate cookie revocation on logout
- secure production cookies and non-sliding absolute expiry
- health endpoint version reporting and authentication regression tests

## What It Does

- register and log in users with session cookies
- keep authenticated sessions across refreshes and browser restarts for up to 365 days
- store projects per user
- load a single project
- list user projects
- update and delete projects

## Project Layout

- `app.py`: Flask app entry point and app factory
- `auth.py`: auth endpoints
- `projects.py`: project CRUD endpoints
- `db.py`: SQLite connection lifecycle and database bootstrap
- `models.py`: compact data-access helpers
- `utils.py`: password hashing and auth helpers
- `schema.sql`: SQLite schema
- `data/app.db`: SQLite database file

## Development

Run from the `users/` folder:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export WEBMARS_USERS_ENV=development
flask run
```

If your Flask install does not auto-detect `app.py`, use:

```bash
flask --app app run
```

The API will be available at `http://localhost:5000`.

Authentication cookies are `HttpOnly`, permanent for 365 days from login or
registration, and removed immediately by `POST /api/logout`. The expiry is not
silently extended on every request. Production mode marks the cookie `Secure`
by default; set `SESSION_COOKIE_SECURE=0` only for an intentional HTTP-only
deployment.

On Windows you can also just run:

```bat
start-web.bat
```

That script starts the Flask dev server on `http://localhost:5000`. You can override it with `WEBMARS_USERS_HOST` and `WEBMARS_USERS_PORT`.

For local WebMARS integration, prefer running both services on `localhost`:

- frontend: `http://localhost:8080`
- users service: `http://localhost:5000`

That keeps the browser session cookie flow simpler during development.

## Database Initialization

No manual step is required. On first startup the service creates `data/app.db` automatically from `schema.sql`.

If you want a clean reset during development:

```bash
rm -f data/app.db
flask run
```

## Gunicorn

Run from the `users/` folder:

```bash
export WEBMARS_USERS_ENV=production
export SECRET_KEY='replace-with-a-long-random-secret'
export WEBMARS_USERS_TRUSTED_PROXIES=1
export WEBMARS_USERS_ALLOWED_ORIGINS='https://webmars.nfiles.top'
gunicorn app:app
```

Typical production deployment is `NGINX -> Gunicorn -> Flask`.

The app also enables credentialed CORS for local WebMARS development from:

- `http://localhost:8080`
- `http://127.0.0.1:8080`

## API Summary

- `POST /api/register`
- `POST /api/login` — `429` with `Retry-After` once an address has failed too often
- `POST /api/logout`
- `GET /api/me`
- `GET /api/projects` — also returns `serverTime`
- `POST /api/projects` — `409` when the name is already taken
- `GET /api/projects/<id>` — returns `ETag` and an `etag` field on the project
- `PUT /api/projects/<id>` — requires `If-Match`; `412` when the stored copy moved on, `409` on a name clash, `428` when omitted
- `DELETE /api/projects/<id>` — requires `If-Match`, with the same `412`/`428` precondition behaviour

Any request body above `WEBMARS_USERS_MAX_CONTENT_LENGTH` (4 MB by default) is
rejected with `413`.

### Optimistic locking

Read a project, keep its `etag`, and send it back as `If-Match` on the next
`PUT`. If another session saved in the meantime the write is refused with `412`
and the response carries the current project so the caller can merge without a
second round trip. Mutation without `If-Match` is rejected with `428`; this is
intentional because silently falling back to last-writer-wins would reintroduce
the data-loss path optimistic locking is meant to close.

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | *(none)* | Session signing key. Required in production. |
| `WEBMARS_USERS_ENV` | *(none)* | Required: `development` or `production`. Startup refuses to guess. |
| `WEBMARS_USERS_ALLOWED_ORIGINS` | *(empty)* | Extra comma-separated CORS origins. |
| `WEBMARS_USERS_MAX_CONTENT_LENGTH` | `4194304` | Largest accepted request body, in bytes. |
| `WEBMARS_USERS_LOGIN_MAX_ATTEMPTS` | `20` | Failed sign-ins per address before `429`. |
| `WEBMARS_USERS_DATABASE_BUSY_TIMEOUT_MS` | `5000` | How long SQLite waits for a concurrent writer. |

Browser writes whose `Origin` is not in the allowlist are rejected with `403`,
including requests from an untrusted sibling subdomain. Requests without an
`Origin` remain available to command-line administration clients.
| `WEBMARS_USERS_LOGIN_WINDOW_SECONDS` | `900` | Window the failures are counted over. |
| `WEBMARS_USERS_TRUSTED_PROXIES` | `0` | Above zero, read the client address from `X-Forwarded-For`. |
| `SESSION_COOKIE_SAMESITE` | `Lax` | Set to `None` (with `Secure`) if the API moves to another registrable domain. |

Behind NGINX set `WEBMARS_USERS_TRUSTED_PROXIES=1`, otherwise every request
looks like it comes from the proxy and the login rate limit applies to all
clients at once.

Project payload example:

```json
{
  "name": "demo",
  "project": {
    "files": {
      "main.asm": "...",
      "utils.asm": "...",
      "test.c": "..."
    },
    "activeFile": "main.asm",
    "settings": {},
    "updatedAt": "2026-03-15T12:00:00Z"
  }
}
```
