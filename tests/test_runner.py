# Copyright 2026-present Orbit Contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Exercise migration ordering, idempotence, history validation, and rollback behavior."""

import asyncio
from collections.abc import Iterator, Sequence

import pytest
from orbit_sql import SQLTransaction
from orbit_sql_sqlite import SQLiteDatabase

from orbit_migrations import (
    Migration,
    MigrationDefinitionError,
    MigrationExecutionError,
    MigrationRunner,
    MigrationStateError,
)


@pytest.mark.asyncio
async def test_applies_in_version_order_and_skips_recorded_migrations() -> None:
    """Applying twice runs each migration once and returns only newly applied versions."""
    database = SQLiteDatabase(":memory:")

    async def add_second_table(transaction: SQLTransaction) -> None:
        await transaction.execute("CREATE TABLE second_table (value TEXT NOT NULL)")

    async def add_first_table(transaction: SQLTransaction) -> None:
        await transaction.execute("CREATE TABLE first_table (value TEXT NOT NULL)")

    runner = MigrationRunner(
        database,
        [
            Migration(2, "add_second_table", add_second_table),
            Migration(1, "add_first_table", add_first_table),
        ],
    )
    try:
        assert await runner.apply_pending() == (1, 2)
        assert await runner.apply_pending() == ()
        rows = await database.fetch_all(
            "SELECT version, name FROM orbit_schema_migrations ORDER BY version", limit=10
        )
        assert [(row["version"], row["name"]) for row in rows] == [
            (1, "add_first_table"),
            (2, "add_second_table"),
        ]
    finally:
        await database.aclose()


@pytest.mark.asyncio
async def test_failed_callback_rolls_back_schema_and_history() -> None:
    """A callback failure rolls back its DDL and does not record that version."""
    database = SQLiteDatabase(":memory:")

    async def fail(transaction: SQLTransaction) -> None:
        await transaction.execute("CREATE TABLE must_rollback (value TEXT)")
        raise RuntimeError("secret provider detail")

    runner = MigrationRunner(database, [Migration(1, "failing", fail)])
    try:
        with pytest.raises(
            MigrationExecutionError, match=r"Migration 1 \(failing\) failed"
        ) as error:
            await runner.apply_pending()
        assert "secret provider detail" not in str(error.value)
        assert (
            await database.fetch_one(
                "SELECT name FROM sqlite_master WHERE name = $1", ("must_rollback",)
            )
            is None
        )
        assert (
            await database.fetch_all("SELECT version FROM orbit_schema_migrations", limit=10) == ()
        )
    finally:
        await database.aclose()


@pytest.mark.asyncio
async def test_cancellation_rolls_back_without_becoming_execution_error() -> None:
    """Cancellation propagates and lets Core's SQL transaction perform rollback."""
    database = SQLiteDatabase(":memory:")
    entered = asyncio.Event()

    async def wait_for_cancel(transaction: SQLTransaction) -> None:
        await transaction.execute("CREATE TABLE cancelled_table (value TEXT)")
        entered.set()
        await asyncio.Event().wait()

    runner = MigrationRunner(database, [Migration(1, "cancelled", wait_for_cancel)])
    task = asyncio.create_task(runner.apply_pending())
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            await database.fetch_one(
                "SELECT name FROM sqlite_master WHERE name = $1", ("cancelled_table",)
            )
            is None
        )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await database.aclose()


@pytest.mark.asyncio
async def test_unknown_database_history_fails_before_running_callbacks() -> None:
    """An applied version missing from the supplied release is treated as a state mismatch."""
    database = SQLiteDatabase(":memory:")
    await database.execute(
        "CREATE TABLE orbit_schema_migrations (version BIGINT PRIMARY KEY, name TEXT NOT NULL)"
    )
    await database.execute(
        "INSERT INTO orbit_schema_migrations (version, name) VALUES ($1, $2)",
        (99, "removed_migration"),
    )
    runner = MigrationRunner(database, [])
    try:
        with pytest.raises(MigrationStateError, match="absent from this release"):
            await runner.apply_pending()
    finally:
        await database.aclose()


@pytest.mark.asyncio
async def test_missing_earlier_migration_fails_before_running_out_of_order() -> None:
    """A missing old migration is not applied after a later version is already recorded."""
    database = SQLiteDatabase(":memory:")
    await database.execute(
        "CREATE TABLE orbit_schema_migrations (version BIGINT PRIMARY KEY, name TEXT NOT NULL)"
    )
    await database.execute(
        "INSERT INTO orbit_schema_migrations (version, name) VALUES ($1, $2), ($3, $4)",
        (1, "first", 3, "third"),
    )

    async def migration(transaction: SQLTransaction) -> None:
        await transaction.execute("CREATE TABLE should_not_run (value TEXT)")

    runner = MigrationRunner(
        database,
        [
            Migration(1, "first", migration),
            Migration(2, "second", migration),
            Migration(3, "third", migration),
        ],
    )
    try:
        with pytest.raises(MigrationStateError, match="out-of-order"):
            await runner.apply_pending()
        assert (
            await database.fetch_one(
                "SELECT name FROM sqlite_master WHERE name = $1", ("should_not_run",)
            )
            is None
        )
    finally:
        await database.aclose()


@pytest.mark.asyncio
async def test_oversized_migration_history_fails_before_applying_new_migrations() -> None:
    """History truncation at the SQL row cap cannot hide applied migration versions."""
    database = SQLiteDatabase(":memory:")
    runner = MigrationRunner(database, [])
    try:
        await runner._ensure_history_table()
        for start in range(1, 10_002, 500):
            versions = range(start, min(start + 500, 10_002))
            placeholders = ", ".join(
                f"(${index * 2 + 1}, ${index * 2 + 2})" for index, _ in enumerate(versions)
            )
            parameters = tuple(
                value for version in versions for value in (version, f"migration_{version}")
            )
            await database.execute(
                f"INSERT INTO orbit_schema_migrations (version, name) VALUES {placeholders}",
                parameters,
            )

        with pytest.raises(MigrationStateError, match="exceeds the 10,000-entry limit"):
            await runner.apply_pending()
    finally:
        await database.aclose()


@pytest.mark.asyncio
async def test_duplicate_versions_in_legacy_history_fail_closed() -> None:
    """Duplicate rows in a pre-existing table are not collapsed into one version."""
    database = SQLiteDatabase(":memory:")
    await database.execute(
        "CREATE TABLE orbit_schema_migrations (version BIGINT, name TEXT NOT NULL)"
    )
    await database.execute(
        "INSERT INTO orbit_schema_migrations (version, name) VALUES ($1, $2), ($3, $4)",
        (1, "first", 1, "duplicate"),
    )

    async def noop(transaction: SQLTransaction) -> None:
        del transaction

    runner = MigrationRunner(database, [Migration(1, "first", noop)])
    try:
        with pytest.raises(MigrationStateError, match="duplicate versions"):
            await runner.apply_pending()
    finally:
        await database.aclose()


def test_duplicate_versions_and_unsafe_history_names_are_rejected() -> None:
    """Invalid migration sets cannot create ambiguous or interpolated SQL history names."""

    async def noop(transaction: SQLTransaction) -> None:
        del transaction

    with pytest.raises(MigrationDefinitionError, match="unique"):
        MigrationRunner(
            SQLiteDatabase(":memory:"),
            [Migration(1, "one", noop), Migration(1, "also_one", noop)],
        )
    with pytest.raises(MigrationDefinitionError, match="SQL identifier"):
        MigrationRunner(SQLiteDatabase(":memory:"), [], table="history; DROP TABLE users")


def test_migration_sequence_length_cannot_bypass_iteration_bound() -> None:
    """Custom Sequence implementations cannot hide an oversized migration iterator."""

    async def noop(transaction: SQLTransaction) -> None:
        del transaction

    class LyingSequence(Sequence[Migration]):
        observed = 0

        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int | slice) -> Migration | tuple[Migration, ...]:
            if isinstance(index, slice):
                raise TypeError("slice access is unsupported")
            self.observed += 1
            return Migration(index + 1, f"migration_{index + 1}", noop)

        def __iter__(self) -> Iterator[Migration]:
            index = 0
            while True:
                yield self[index]  # type: ignore[misc]
                index += 1

    migrations = LyingSequence()
    with pytest.raises(MigrationDefinitionError, match="more than 10,000"):
        MigrationRunner(SQLiteDatabase(":memory:"), migrations)
    assert migrations.observed == 10_001
