# BackMars v0.4.0

BackMars is the optional, self-hostable service for webMARS accounts, private
Cloud projects, email-based account recovery and the per-installation Public
Library. It is a small Flask application backed by SQLite and contains no
vendor-specific mail dependency: any standards-compliant SMTP relay works.

The webMARS assembler, simulator, tools and local project storage do not depend
on BackMars. Each university or teacher may operate an independent instance;
accounts and first-come public names belong only to that instance.

## v0.4.0

- Password recovery and email verification through six-digit, short-lived,
  one-use codes. Only keyed digests are stored; resetting a password revokes
  all older sessions.
- SMTP delivery with STARTTLS or implicit TLS, optional asynchronous dispatch,
  provider-neutral settings and production guards against plaintext secrets.
- Public examples/exercises with permanent first-come slugs, searchable public
  metadata, anonymous reads, unlisted items and immutable numbered revisions.
- Pinned revision metadata and content are snapshotted together. Ordinary
  archive hides the mutable latest URL but keeps `/p/name@N` durable; names are
  never released.
- Server-side publication sanitization strips Cloud/UI/runtime state and accepts
  only bounded, relative text-source files.
- Additive schema migrations, session-version revocation, registration/code
  rate limits, input bounds and production database/path/permission guards.

Earlier v0.3 behavior remains: permanent sessions, ETag/`If-Match` optimistic
locking, batched Cloud sync, request limits, login throttling, WAL and safe
case-insensitive project names.

## Development

Python 3.11 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export WEBMARS_USERS_ENV=development
flask --app app run
```

On Windows activate with `.venv\Scripts\activate` or use `start-web.bat`.
The development API is `http://localhost:5000`; the webMARS development server
normally runs at `http://localhost:8080`.

Run the suite with the virtual environment interpreter:

```bash
python -m unittest discover -s tests -v
```

## Production invariants

BackMars refuses to start when a production configuration would be unsafe or
non-functional. In production:

- `WEBMARS_USERS_ENV=production` is mandatory.
- `SECRET_KEY` must be an unpredictable value of at least 32 bytes, not the
  public development placeholder.
- `BACKMARS_DATABASE` must be an absolute path outside the code checkout.
- If verified email is required for publishing, transactional mail must work.
- SMTP security must be `starttls`, `ssl`/`tls`, or explicit `none`; production
  refuses `none`.
- Authenticated SMTP without TLS is refused in every environment.
- When mail is enabled, the account-code pepper must be independent of the
  session secret and contain at least 32 bytes.

Use `backmars.env.example` as a field reference and follow
[`DEPLOYMENT.md`](DEPLOYMENT.md). Never commit real credentials.

## API

### Health and accounts

- `GET /api/health` — service/version and `mailConfigured`.
- `POST /api/register` — creates account/session; queues verification mail.
- `POST /api/login` — username or email; failed attempts are rate limited.
- `POST /api/logout` — invalidates the browser cookie immediately.
- `GET /api/me` — current session and `emailVerified` state.
- `POST /api/password-reset/request` — always returns the same accepted response
  for known and unknown valid email addresses.
- `POST /api/password-reset/confirm` — email, temporary code and new password.
- `POST /api/email-verification/request` — authenticated resend.
- `POST /api/email-verification/confirm` — authenticated code confirmation.

### Private Cloud projects

- `GET /api/projects?include=documents` — projects, documents, ETags and server
  time for sync.
- `POST /api/projects` — create; project names are unique per user without case
  distinctions.
- `GET /api/projects/<id>` — document plus `ETag`.
- `PUT /api/projects/<id>` — requires `If-Match`; returns `412` for stale state,
  `428` when the precondition is omitted.
- `DELETE /api/projects/<id>` — same precondition contract.

### Public Library

- `GET /api/publications?q=&limit=&offset=` — anonymous published listing.
- `GET /api/publications/<slug>` — latest published/unlisted revision; short
  cache lifetime.
- `GET /api/publications/<slug>/revisions/<N>` — immutable pinned snapshot;
  one-year immutable cache and readable after ordinary archive.
- `GET /api/publications/mine?limit=&offset=` — paginated authenticated owner
  listing, including `hasMore`.
- `POST /api/publications` — claim permanent name and publish revision 1.
- `POST /api/publications/<slug>/revisions` — add immutable revision.
- `PUT /api/publications/<slug>` — edit latest metadata/visibility; the slug
  cannot change.
- `DELETE /api/publications/<slug>` — archive latest/listing without releasing
  the name or deleting pinned revisions.

Publishing requires a verified email by default. Public snapshots support up to
128 bounded text files by default (`.s`, `.asm`, `.mips`, `.c`, `.c0`, `.h`,
`.h0`, `.txt`) and reject absolute/traversal/duplicate paths, NUL bytes and
binary extensions.

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `WEBMARS_USERS_ENV` | none | Required `development` or `production`. |
| `SECRET_KEY` | development placeholder | Session signing; unpredictable and at least 32 bytes in production. |
| `BACKMARS_ACCOUNT_CODE_PEPPER` | session secret in development | Separate HMAC secret of at least 32 bytes; required separately with production mail. |
| `BACKMARS_DATABASE` | `data/app.db` | SQLite path; explicit absolute outside checkout in production. |
| `WEBMARS_PUBLIC_URL` | `http://localhost:8080` | Installation URL used in transactional copy. |
| `WEBMARS_USERS_ALLOWED_ORIGINS` | local dev origins | Comma-separated exact browser origins allowed to write. |
| `WEBMARS_USERS_TRUSTED_PROXIES` | `0` | Trusted proxy hops used for client IP/proto. |
| `SESSION_COOKIE_SECURE` | `1` in production | HTTPS-only session cookie. |
| `SESSION_COOKIE_SAMESITE` | `Lax` | Use `None` only for genuine cross-site HTTPS. |
| `WEBMARS_USERS_MAX_CONTENT_LENGTH` | `4194304` | Maximum HTTP request body. |
| `WEBMARS_USERS_LOGIN_MAX_ATTEMPTS` | `20` | Failed logins per address/window. |
| `WEBMARS_USERS_LOGIN_WINDOW_SECONDS` | `900` | Failed-login window. |
| `BACKMARS_REGISTER_MAX_ATTEMPTS` | `20` | Registrations per address/window. |
| `BACKMARS_REGISTER_WINDOW_SECONDS` | `3600` | Registration window. |
| `BACKMARS_PASSWORD_MIN_LENGTH` | `8` | Minimum password characters. |
| `BACKMARS_PASSWORD_MAX_LENGTH` | `1024` | Maximum password characters before hashing. |
| `BACKMARS_USERNAME_MAX_LENGTH` | `64` | Maximum username characters. |
| `BACKMARS_PROJECT_MAX_NAME_LENGTH` | `120` | Maximum private project-name characters. |
| `BACKMARS_CODE_TTL_SECONDS` | `600` | Temporary code lifetime. |
| `BACKMARS_CODE_MAX_ATTEMPTS` | `5` | Wrong attempts before invalidation. |
| `BACKMARS_CODE_USER_LIMIT_PER_HOUR` | `5` | Code issues per account/purpose/hour. |
| `BACKMARS_CODE_ADDRESS_LIMIT_PER_HOUR` | `20` | Code issues per address/purpose/hour. |
| `BACKMARS_REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH` | `1` | Gate public mutations on verified email. |
| `BACKMARS_MAIL_ENABLED` | `0` | Enable transactional SMTP. |
| `BACKMARS_MAIL_FROM` | empty | Envelope/display sender address. |
| `BACKMARS_MAIL_FROM_NAME` | `webMARS` | Sender display name. |
| `BACKMARS_SMTP_HOST` | empty | SMTP relay hostname. |
| `BACKMARS_SMTP_PORT` | `587` | SMTP relay port. |
| `BACKMARS_SMTP_SECURITY` | `starttls` | `starttls`, `ssl`/`tls`, or `none` outside production. |
| `BACKMARS_SMTP_USERNAME` | empty | Optional SMTP login. |
| `BACKMARS_SMTP_PASSWORD` | empty | SMTP/App Password; server secret. |
| `BACKMARS_SMTP_TIMEOUT_SECONDS` | `15` | Network timeout. |
| `BACKMARS_MAIL_SEND_ASYNC` | `1` in production | Queue delivery off the request path. |
| `WEBMARS_USERS_DATABASE_BUSY_TIMEOUT_MS` | `5000` | SQLite writer wait. |

Publication field/file limits use the `BACKMARS_PUBLICATION_*` variables in
`config.py` and are listed in `backmars.env.example`. Keep the total public
project limit at or below the matching webMARS local-project limit (1 MiB in
the standard build), so every accepted publication can be opened by clients.

## Storage and migrations

Startup replays the idempotent schema then applies additive migrations under a
single SQLite write lock. v0.4 adds verification/session state, account codes,
registration attempts, publications, revisions and immutable revision metadata.
Existing users and Cloud projects are preserved.

SQLite runs with foreign keys, WAL, a busy timeout and `synchronous=NORMAL`.
This improves concurrency but does not replace backups. Use SQLite's online
`.backup` command or stop writes before copying all database state.

## Repository layout

- `app.py` — app factory, production guards, CORS and health.
- `auth.py` — sessions, registration, reset and verification flows.
- `projects.py` — private Cloud CRUD and optimistic locking.
- `publications.py` — public metadata, sanitization and revision API.
- `mailer.py` — provider-neutral SMTP transport and account messages.
- `models.py` — SQLite data access and atomic account/publication operations.
- `db.py` / `schema.sql` — connection policy and additive migrations.
- `tests/` — account, session, project, publication, migration and security tests.
