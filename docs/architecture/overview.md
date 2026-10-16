# Orbit Migrations architecture

`orbit-migrations` is an explicit forward migration runner for the `orbit-sql` async database
contract. It receives a fixed ordered set of Python callbacks, records applied versions in a
history table, and skips already-recorded versions. It does not discover migration modules, bundle
a database driver, or run automatically during application startup.

```text
release/deployment step ──> MigrationRunner ──> orbit-sql SQLDatabase
                                      └── one SQLTransaction per migration
                                           ├── application migration callback
                                           └── history row insert
```

The app installs a provider separately. `orbit-data` and `orbit-sql` define shared contracts;
`orbit-sql-sqlite`, `orbit-sql-postgres`, and `orbit-sql-mysql` own the backend implementation and
driver. No ORM, web framework, SQL driver, schema diff engine, or migration CLI is bundled here.

## Definitions and history

Each `Migration` has a positive signed 64-bit integer version, a printable name of at most 255
characters, and an async callback receiving the transaction. The runner copies no more than 10,000
definitions, sorts them by version, and rejects duplicate versions. Names are compared with the
history table to detect a version that was renamed after it was applied. Removed/unknown versions
and missing earlier migrations fail closed before callbacks run.

The history table defaults to `orbit_schema_migrations`; its configurable name is restricted to a
simple SQL identifier and quoted by the runner. The history has a 10,000-row cap. Existing malformed
or oversized history fails closed; the runner does not silently truncate or repair it.

Each pending callback and its version/name insert are executed through one `SQLTransaction`. A
callback failure raises `MigrationExecutionError` without retaining the original provider/SQL
exception text; the transaction context is responsible for rollback. This is per migration, not
one transaction around the complete migration set: earlier successful migrations remain committed
if a later migration fails. Whether DDL participates in a transaction is database-specific and
must be validated for each supported backend.

## Concurrency and lifecycle

A runner instance serializes its own `apply_pending()` calls. This is only an in-process lock. It
does not coordinate separate runner instances, processes, deployment jobs, or hosts. Concurrent
deployment runners can race after reading history, so the deployment system must establish a
single migration owner or acquire a backend-specific distributed/advisory lock before invoking the
runner. The package intentionally does not pretend that a process-local lock is distributed.

The runner borrows the supplied `SQLDatabase`; it does not close it. The deployment/application
owns database startup, credentials, transaction lifecycle, and shutdown. Run migrations as a
deliberate release/deployment step before scaling or starting code that requires the new schema.

## Rollback and recovery model

The runner supports forward-only application and history validation. It does not execute downgrade
callbacks, generate schemas, repair partial non-transactional DDL, take backups, or coordinate
rolling compatibility between old and new application versions. Author migrations to support the
deployment's rollout sequence, test them against the actual database version, and maintain a
database-specific backup and recovery plan.

The package is alpha. SQLite contract tests do not prove transaction behavior or DDL rollback
semantics for PostgreSQL/MySQL deployments, and hosted/release validation remains separate.
