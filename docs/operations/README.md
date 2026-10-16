# Orbit Migrations operations

Run schema migrations as an explicit deployment step with the matching application release and
database provider. The runner does not migrate at application startup and should not be started by
every web worker or replica.

## Deployment sequence

1. Build and review the migration definitions as part of the release artifact.
2. Back up the target database and verify the restore procedure for high-value data.
3. Ensure exactly one deployment actor owns the migration run. The in-process lock only protects
   calls through one `MigrationRunner` instance; use CI/deployment serialization or a
   provider-specific advisory lock for cross-process exclusion.
4. Run `apply_pending()` with the intended package version and database credentials.
5. Check the returned versions and database state before rolling out application replicas that
   depend on the new schema.
6. Close the database resource through the code that created it.

The runner is forward-only. For compatible zero-downtime changes, use an expand/migrate/contract
rollout: add backward-compatible schema first, deploy code that can handle both states, backfill in
a separately controlled bounded job, then remove obsolete schema only in a later release after all
old code is gone. Backfills can be long-running and should not be hidden inside a schema migration.

## Transaction and partial failure

Each migration callback and its history row share one `SQLTransaction`; successful earlier
migrations remain committed if a later version fails. The failed callback raises a generic
`MigrationExecutionError` and is expected to roll back through the database transaction context.
Cancellation propagates so the transaction context can perform its cleanup.

Some databases or DDL operations commit implicitly or cannot roll back all schema changes. Test
each migration on the same backend and version used in deployment. After an interruption, inspect
both schema and `orbit_schema_migrations` before rerunning. Do not manually insert history rows to
make the runner skip uncertain or partially applied changes; write a reviewed repair or follow the
database recovery plan.

## Resource and configuration limits

The runner accepts at most 10,000 migrations and at most 10,000 history entries. Versions are
positive signed 64-bit integers and must be unique. The default history table is
`orbit_schema_migrations`; custom names must match `[A-Za-z_][A-Za-z0-9_]{0,62}`. Each migration
name is printable text up to 255 characters. These limits bound runner metadata, not the SQL
runtime, DDL cost, lock duration, table size, or backfill workload.

Migration callbacks should use the transaction passed to them. Avoid reaching around it through a
second connection, because those changes will not share the callback's transaction and may persist
even when the history insert rolls back.

## Provider validation

Verify driver/database compatibility, transaction isolation, DDL rollback semantics, lock behavior,
statement timeouts, long-running lock impact, backup/restore, migration-runner exclusion, and
rolling application compatibility against the chosen backend. This package's local SQLite tests do
not certify PostgreSQL or MySQL behavior or the target deployment's safety.
