# Orbit Migrations: architecture and boundaries

## Responsibility

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

## Declared dependencies

The following dependency declarations come from the checked-in manifests. Optional groups and development dependencies are called out separately.

### `pyproject.toml`
- `orbit-sql>=0.1.0a1,<0.2`
- Optional `dev` group: `pytest>=8,<10`, `pytest-asyncio>=0.24,<2`, `ruff>=0.8,<1`, `mypy>=1.13,<2`, `orbit-sql-sqlite>=0.1.0a1,<0.2`.

Declared dependencies do not mean that optional providers or services are bundled with this package.

## Implementation layout

Representative implementation files in this checkout:

- `.tox/install-target/orbit_migrations/__init__.py`
- `.tox/install-target/orbit_migrations/runner.py`
- `.tox/wheel-install-current/orbit_migrations/__init__.py`
- `.tox/wheel-install-current/orbit_migrations/runner.py`
- `src/orbit_migrations/__init__.py`
- `src/orbit_migrations/runner.py`

## Public contract and scope

The README does not contain a separately headed architecture section. Its responsibility statement above and the public source files define the implemented scope; this guide adds no behavior beyond that description.

## Boundary rules

Keep provider SDKs, credentials, transports, and provider-specific error translation in provider adapters. Keep reusable capability contracts in the matching capability package and lifecycle orchestration in Core. Apply the relevant layer for this repository and preserve the dependency direction shown above.
