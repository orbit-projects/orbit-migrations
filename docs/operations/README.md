# Orbit Migrations: operations and security

This guide organizes runtime behavior documented by the package. It does not certify production readiness. Verify provider/client versions, permissions, transport security, limits, and failure behavior in the target environment before release.

## Configuration surface

Environment names found in the package README:

The package README does not name `ORBIT_*` variables. Use its typed constructors and application configuration, and confirm exact runtime inputs in the implementation before deployment.

Use the package README's constructor and deployment examples. Store credentials in a secret manager and avoid logging credentials, raw provider errors, request data, or opaque cursors.

## Lifecycle, failure behavior, and limits

`orbit-migrations` is an optional, explicit migration runner for the provider-neutral `orbit-sql`
contract. Install one SQL provider separately, such as SQLite for local development; the migration
package does not install a database driver, ORM, or web framework.

```bash
python -m pip install orbit-data orbit-sql orbit-sql-sqlite orbit-migrations
```

Define ordered, asynchronous migration callbacks and give them to a runner:

```python
import asyncio

from orbit_sql import SQLTransaction
from orbit_sql_sqlite import SQLiteDatabase
from orbit_migrations import Migration, MigrationRunner


async def create_notes(transaction: SQLTransaction) -> None:
    await transaction.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")


async def main() -> None:
    database = SQLiteDatabase("var/orbit.db")
    runner = MigrationRunner(database, [Migration(1, "create_notes", create_notes)])
    try:
        applied_versions = await runner.apply_pending()
        print(applied_versions)
    finally:
        await database.aclose()


asyncio.run(main())
```

Each migration and its history record run in one database transaction. A failed migration rolls
back and raises a sanitized `MigrationExecutionError`; cancellation propagates after the database
transaction has rolled back. Reapplying a migration set is safe: already-recorded versions are
skipped, while unknown versions, changed names, or missing earlier versions fail closed. Migration
versions are positive, unique integers and are applied in ascending order.
The recorded history is bounded to 10,000 entries; oversized or duplicate legacy history fails
closed before a new migration runs.

Run migrations as a deliberate deployment step before starting or scaling application workers.
Only one migration runner may operate on a database at a time: the runner serializes calls made
through one instance but does not acquire a distributed/advisory lock across processes. Migration
callbacks are application code and should use only the provided transaction. Do not place secrets
in migration names or SQL literals.

The history table defaults to `orbit_schema_migrations` and can be changed using a validated SQL
identifier. This package is forward-only; it does not execute destructive downgrades or discover
migration modules automatically.

## Production validation

Validate startup/shutdown cleanup, timeout and cancellation behavior, concurrency and payload bounds where applicable, secret rotation and least-privilege access, data durability, backup/restore, and failover against the selected provider. Do not infer distributed or durable guarantees from an in-process API or fake-client tests.
