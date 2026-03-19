# WebMARS Users Service

Small Flask backend for WebMARS user accounts and project storage. It is designed for small text-based workspaces and stores each project as one JSON document inside SQLite.

Password hashing now uses Werkzeug `scrypt` for new passwords. Older `bcrypt` hashes are still accepted and are upgraded automatically on the next successful login, using the native `bcrypt` package to keep legacy logins fast.

## What It Does

- register and log in users with session cookies
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
flask run
```

If your Flask install does not auto-detect `app.py`, use:

```bash
flask --app app run
```

The API will be available at `http://localhost:5000`.

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
gunicorn app:app
```

Typical production deployment is `NGINX -> Gunicorn -> Flask`.

The app also enables credentialed CORS for local WebMARS development from:

- `http://localhost:8080`
- `http://127.0.0.1:8080`

## API Summary

- `POST /api/register`
- `POST /api/login`
- `POST /api/logout`
- `GET /api/me`
- `GET /api/projects`
- `POST /api/projects`
- `GET /api/projects/<id>`
- `PUT /api/projects/<id>`
- `DELETE /api/projects/<id>`

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
