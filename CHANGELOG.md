# BackMars changelog

All notable BackMars changes are recorded here. BackMars follows semantic
versioning independently from webMARS and the Manual.

## [0.6.0] — 2026-10-22

- Added paginated v2 project metadata and document endpoints, stable client identities and idempotent creation.
- Added a recycle bin with reversible deletion, restore and explicit permanent removal.
- Added authenticated username updates for Google account setup and server-side publication filtering.
- Added migration 7 and regression coverage for existing accounts, project concurrency and publication lists.

## [0.5.0] — 2026-10-20

- Added optional Google OpenID Connect and Facebook sign-in with explicit linking to existing accounts.
- Added one-use, expiring OAuth flows with origin, state, nonce and replay protection; provider tokens are not persisted.
- Added migration 6 for external identities and aggregate account/Cloud activity statistics.
- Added public aggregate statistics by usage context and a Portuguese provider setup guide.
- Preserved password sign-in, account recovery and all existing Cloud data.

## [0.4.1] — 2026-09-03

### Security

- made login, registration and account-code rate-limit reservations atomic;
- preserved earlier failed-login attempts after a successful sign-in;
- equalized the password-verification path for known and unknown accounts;
- required production session and account-code secrets to contain at least 32
  bytes, including deployments with a custom mail transport;
- tightened email validation and rejected non-text authentication fields.

### Fixed

- returned clean validation responses for project and revision IDs too large
  for SQLite instead of allowing an internal error;
- bounded private project names and rejected implicit object-to-string coercion
  in projects, publications, file paths, file IDs and source text;
- made anonymous Public Library pagination report an exact `hasMore` value.

### Documentation

- documented the minimum secret strength and
  `BACKMARS_PROJECT_MAX_NAME_LENGTH`;
- ignored local coverage and Ruff cache artefacts.

## [0.4.0] — 2026-08-29

### Added

- password recovery and email verification with short-lived one-use codes;
- per-installation Public Library with permanent names and immutable revisions;
- sanitized, bounded public project snapshots;
- production deployment, Gmail SMTP, backup and migration guidance.

[0.4.1]: https://github.com/codigoavulso/backmars/releases/tag/v0.4.1
[0.4.0]: https://github.com/codigoavulso/backmars/releases/tag/v0.4.0
