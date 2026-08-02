# backmars v0.3.0 deployment checklist

## Before the restart

1. Back up the SQLite database while the old service is stopped, or use SQLite's backup command.
2. Install the pinned dependencies from `requirements.txt` in the service virtual environment.
3. Configure these environment variables in the systemd/Gunicorn service:

   ```text
   WEBMARS_USERS_ENV=production
   SECRET_KEY=<long random value kept outside the repository>
   WEBMARS_USERS_ALLOWED_ORIGINS=https://webmars.nfiles.top
   WEBMARS_USERS_TRUSTED_PROXIES=1
   ```

   `WEBMARS_USERS_TRUSTED_PROXIES=1` is correct only for exactly one trusted NGINX proxy between the public client and Gunicorn.

4. Deploy the matching webMARS release before, or at the same time as, this backend. backmars v0.3.0 rejects legacy project `PUT` and `DELETE` requests that omit `If-Match`.

## Restart and migration

The service startup performs the additive schema work. It preserves all projects, renames pre-existing case-insensitive name clashes, rebuilds the per-user unique-name index, and enables SQLite WAL on the first connection.

The service must refuse to start if `WEBMARS_USERS_ENV` is missing or if production still uses `dev-change-me`. Treat either failure as a deployment configuration error; do not weaken the guard.

## Smoke checks

1. `GET /api/health` returns `version: 0.3.0`.
2. Registration or login sets a `Secure`, `HttpOnly`, `SameSite=Lax` session cookie.
3. `GET /api/projects?include=documents` returns `serverTime`, project documents and an `etag` for every project.
4. `PUT /api/projects/<id>` without `If-Match` returns `428`.
5. A current `If-Match` updates the project; reusing the old token returns `412` and leaves the newer content intact.
6. Rename and delete from the webMARS file window update the same cloud record rather than creating or restoring a duplicate.
7. A state-changing request with `Origin: https://evil.nfiles.top` returns `403`.

## Rollback

Stop the new service before restoring the database backup. The webMARS client remains able to talk to backmars v0.2.0, but v0.3.0 should not be left serving an older webMARS build because the older client does not send mandatory preconditions for every mutation.
