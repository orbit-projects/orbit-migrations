# Orbit Migrations security guidance

Database migrations execute application-supplied SQL and can alter or destroy persistent data.
Treat migration files, release artifacts, deployment credentials, and runner invocation as
privileged production controls.

## Change review and execution identity

Require code review and controlled release provenance for migration changes. Restrict who can
publish a release and trigger a migration deployment. Use a dedicated, least-privilege database
identity with only the DDL and data privileges the migration requires; do not reuse broad interactive
administrator credentials. Load credentials through a secret manager, rotate them, and avoid
placing them in command arguments or SQL literals.

The runner does not acquire a distributed lock. Prevent simultaneous migration jobs through the
deployment system or a provider-specific lock; overlapping migrations can race against schema and
history state. The Python lock serializes one object only.

## SQL and sensitive data

The configurable history table is restricted to a simple quoted identifier. Migration SQL remains
arbitrary application code. Parameterize data values, validate any dynamically composed identifier
with an explicit allowlist, and never interpolate untrusted request values into migration SQL.
Avoid embedding secrets or personal data in migration names, statements, source-control examples,
or failure diagnostics.

`MigrationExecutionError` identifies the version/name but omits the provider exception text. Other
database setup, history-read, or state errors can originate from the SQL adapter; configure database
logging to avoid recording credentials, bound parameters, or sensitive SQL values. Do not assume
exception masking is a substitute for reviewing the adapter's logs and tracing hooks.

## Data safety and recovery

The runner does not create backups, encrypt database storage, authorize schema changes, or recover
partially committed DDL. Take and verify backups according to the data's recovery objectives before
high-risk migrations. Test destructive and long-running work against representative data in a
non-production environment. Prefer additive, backward-compatible changes and defer destructive
cleanup until old application versions are no longer active.

DDL transaction behavior varies by backend. If a provider commits schema changes outside the
transaction boundary, the schema may be partially changed even if no history row was recorded.
Inspect the database and use a reviewed recovery procedure before retrying.

Follow the repository [security policy](../../SECURITY.md) for vulnerability disclosure; do not
publish credentials, customer data, or exploitable database details in public issues.
