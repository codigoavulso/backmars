# BackMars v0.5.0 deployment and upgrade checklist

This procedure assumes Nginx → Gunicorn → Flask on Linux. Adapt paths and the
service account, but keep the security boundaries.

## 1. Inventory before touching production

Record without printing secret values:

- current code revision and service unit;
- absolute SQLite path and service user/group;
- Nginx frontend/API routes and request-size limit;
- exact allowed frontend origins and trusted proxy count;
- whether mail is configured and the sender address;
- free disk space and location/age of the latest restorable backup.

Do not continue if the database location is uncertain.

## 2. Consistent backup

Enter a short maintenance window or stop the service. Prefer SQLite's backup
operation so WAL state is included:

```bash
sudo -u backmars sqlite3 /var/lib/backmars/app.db \
  ".backup '/var/lib/backmars/backups/pre-0.5.0.db'"
sudo chmod 600 /var/lib/backmars/backups/pre-0.5.0.db
```

Test the backup with `PRAGMA integrity_check;` and record its checksum outside
the public web root. A plain copy of `app.db` while writes/WAL are active is not
a reliable backup.

## 3. Files and permissions

- Code/release directory: readable, not writable, by the service account.
- `/var/lib/backmars`: service-owned, mode `0700`.
- SQLite DB and backups: mode `0600`.
- `/etc/backmars/backmars.env`: root-owned, mode `0600`.
- Gunicorn socket/runtime directory: service-owned and not publicly accessible.

Production refuses a relative DB path or a path inside the code checkout.

## 4. Environment

Copy `backmars.env.example` to `/etc/backmars/backmars.env` and replace every
placeholder directly on the server. Generate independent secrets, for example
with `openssl rand -hex 32`; never paste them into Git, a ticket or chat.

For Gmail, enable two-step verification and create a Google App Password. Store
that App Password only as `BACKMARS_SMTP_PASSWORD` in the root-only env file.
Use `smtp.gmail.com:587` with `starttls`.

If `BACKMARS_REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH=1`, mail must be configured or
BackMars refuses to start. This prevents a deployment where nobody can publish
or recover an account.

Rotating `SECRET_KEY` signs every user out. Rotating the code pepper invalidates
only unexpired verification/reset codes.

## 5. Deploy and migrate

1. Install the new code into a new release directory.
2. Create/update its virtual environment from pinned `requirements.txt`.
3. Point the service at the root-only `EnvironmentFile`; remove inline secrets
   from the unit file.
4. Start one worker first. Startup takes a SQLite write lock, replays the
   idempotent schema and applies additive migrations through version 6.
5. Confirm startup logs contain no migration error, then run normal workers.
6. Reload Nginx only after `nginx -t` succeeds.

The migration preserves existing users/projects and adds email/session state,
account codes, registration throttling, publications, revisions and revision
metadata snapshots. It never releases or deletes a public name.

Social sign-in is optional: configure providers using [SOCIAL-LOGIN.md](SOCIAL-LOGIN.md). Migration 6 adds external identities, one-use OAuth flows and aggregate account statistics. Keep OAuth callback query strings out of access logs.

## 6. Reverse proxy

- Terminate HTTPS at Nginx.
- Proxy `/api/` to the private Gunicorn socket/upstream.
- Set `client_max_body_size 4m` (or the exact configured BackMars maximum).
- Forward `Host`, `X-Forwarded-Proto` and the client chain once.
- Set `WEBMARS_USERS_TRUSTED_PROXIES=1` only when exactly one trusted proxy sits
  between clients and Gunicorn.
- Keep frontend and API under the same registrable site where possible.

Do not expose Gunicorn directly or trust arbitrary `X-Forwarded-For` headers.

## 7. Smoke checks

Use a dedicated test account and project:

1. `GET /api/health` reports `version: 0.5.0` and `mailConfigured: true`.
2. Registration queues one verification message; confirm the six-digit code.
3. Login works by username and email and sets Secure, HttpOnly session cookie.
4. Request password reset, use the code once, confirm the old password fails and
   a second browser session was revoked.
5. Create/open/update/delete a Cloud project. Missing `If-Match` is `428`; a
   stale token is `412` and never overwrites newer content.
6. Claim a public name, verify anonymous listing/latest/pinned reads and confirm
   a second account gets `409` for the same name ignoring case.
7. Publish revision 2; confirm revision 1 content/metadata did not change and
   its response includes immutable caching.
8. Archive the item; confirm listing/latest is hidden, `/slug@1` remains readable
   and the public name cannot be reclaimed.
9. A state-changing request from an unlisted Origin receives `403`.
10. Check permissions, logs and mail output contain no passwords, codes, cookies
    or full private project documents.

## 8. Rollback

Stop the new service before restoring. Restore the database backup and previous
code/config as one unit, then start and re-run health/login/Cloud smoke tests.
Do not run old code for long against a schema whose public-link semantics it
does not understand.
