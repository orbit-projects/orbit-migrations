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
"""Versioned, explicit migration execution on Orbit's async SQL contract."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from orbit_sql import SQLDatabase, SQLTransaction

MigrationCallback: TypeAlias = Callable[[SQLTransaction], Awaitable[None]]
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_MAX_MIGRATIONS = 10_000
_MAX_VERSION = (1 << 63) - 1


class MigrationError(Exception):
    """Base exception for invalid migration definitions or migration state."""


class MigrationDefinitionError(MigrationError, ValueError):
    """Raised when a migration definition or runner configuration is invalid."""


class MigrationStateError(MigrationError):
    """Raised when the database records migrations absent from the supplied migration set."""


class MigrationExecutionError(MigrationError):
    """Raised when a migration callback fails, without exposing provider or SQL details."""

    def __init__(self, version: int, name: str) -> None:
        """Identify the migration that failed without retaining its exception text."""
        self.version = version
        self.name = name
        super().__init__(f"Migration {version} ({name}) failed; its transaction was rolled back.")


@dataclass(frozen=True, slots=True)
class Migration:
    """One explicit schema change applied atomically with its history record."""

    version: int
    name: str
    upgrade: MigrationCallback

    def __post_init__(self) -> None:
        """Validate stable identifiers and the asynchronous migration callback."""
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or not 1 <= self.version <= _MAX_VERSION
        ):
            raise MigrationDefinitionError(
                "Migration version must be a positive signed 64-bit integer."
            )
        if (
            not isinstance(self.name, str)
            or not self.name
            or len(self.name) > 255
            or any(not character.isprintable() for character in self.name)
        ):
            raise MigrationDefinitionError(
                "Migration name must be printable text of at most 255 characters."
            )
        if not callable(self.upgrade):
            raise MigrationDefinitionError("Migration upgrade must be an async callable.")


class MigrationRunner:
    """Apply a fixed migration set using one Core SQL database resource.

    Calls through one runner instance are serialized. Distributed locking is deliberately not
    implied; deployments must ensure only one process runs migrations against a database at once.
    """

    def __init__(
        self,
        database: SQLDatabase,
        migrations: Sequence[Migration],
        *,
        table: str = "orbit_schema_migrations",
    ) -> None:
        """Create a runner with a validated, immutable migration set and history table name."""
        if not isinstance(database, SQLDatabase):
            raise MigrationDefinitionError("database must implement Orbit's SQLDatabase contract.")
        if isinstance(migrations, (str, bytes)) or not isinstance(migrations, Sequence):
            raise MigrationDefinitionError(
                "migrations must be a finite sequence of Migration values."
            )
        if not isinstance(table, str) or _IDENTIFIER.fullmatch(table) is None:
            raise MigrationDefinitionError("History table must be a valid SQL identifier.")

        definitions = _copy_bounded_migrations(migrations)
        if any(not isinstance(migration, Migration) for migration in definitions):
            raise MigrationDefinitionError("Every migration must be a Migration value.")
        versions = [migration.version for migration in definitions]
        if len(set(versions)) != len(versions):
            raise MigrationDefinitionError("Migration versions must be unique.")
        self._database = database
        self._migrations = tuple(sorted(definitions, key=lambda migration: migration.version))
        self._table = f'"{table}"'
        self._lock = asyncio.Lock()

    async def apply_pending(self) -> tuple[int, ...]:
        """Apply pending migrations in ascending version order and return their versions.

        The schema history table is created if necessary. Existing rows must match the supplied
        migration set by both version and name; unknown or renamed entries fail before upgrades run.
        Each callback and its history insert execute within the same SQL transaction.
        """
        async with self._lock:
            await self._ensure_history_table()
            applied = await self._load_history()
            expected = {migration.version: migration.name for migration in self._migrations}
            unknown_versions = set(applied).difference(expected)
            if unknown_versions:
                raise MigrationStateError(
                    "Database history contains migrations absent from this release."
                )
            for version, name in applied.items():
                if expected[version] != name:
                    raise MigrationStateError(
                        "Database history does not match the supplied migration names."
                    )
            latest_applied = max(applied, default=0)
            if any(
                migration.version not in applied and migration.version < latest_applied
                for migration in self._migrations
            ):
                raise MigrationStateError(
                    "Database history is missing an earlier migration; refusing "
                    "out-of-order upgrades."
                )

            newly_applied: list[int] = []
            for migration in self._migrations:
                if migration.version in applied:
                    continue
                try:
                    async with self._database.transaction() as transaction:
                        await migration.upgrade(transaction)
                        await transaction.execute(
                            f"INSERT INTO {self._table} (version, name) VALUES ($1, $2)",
                            (migration.version, migration.name),
                        )
                except Exception:
                    raise MigrationExecutionError(migration.version, migration.name) from None
                newly_applied.append(migration.version)
            return tuple(newly_applied)

    async def _ensure_history_table(self) -> None:
        """Create the portable history table before reading applied versions."""
        await self._database.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} "
            "(version BIGINT PRIMARY KEY, name TEXT NOT NULL)"
        )

    async def _load_history(self) -> dict[int, str]:
        """Read a bounded history snapshot and reject malformed stored entries."""
        count_row = await self._database.fetch_one(
            f"SELECT COUNT(*) AS migration_count FROM {self._table}"
        )
        if count_row is None:
            raise MigrationStateError("Database migration history count could not be read.")
        migration_count = count_row["migration_count"]
        if (
            isinstance(migration_count, bool)
            or not isinstance(migration_count, int)
            or migration_count < 0
        ):
            raise MigrationStateError("Database migration history count is invalid.")
        if migration_count > _MAX_MIGRATIONS:
            raise MigrationStateError(
                f"Database migration history exceeds the {_MAX_MIGRATIONS:,}-entry limit."
            )

        rows = await self._database.fetch_all(
            f"SELECT version, name FROM {self._table} ORDER BY version ASC",
            limit=_MAX_MIGRATIONS,
        )
        if len(rows) != migration_count:
            raise MigrationStateError("Database migration history changed while it was being read.")
        history: dict[int, str] = {}
        for row in rows:
            version = row["version"]
            name = row["name"]
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or not 1 <= version <= _MAX_VERSION
                or not isinstance(name, str)
            ):
                raise MigrationStateError("Database migration history contains an invalid entry.")
            if version in history:
                raise MigrationStateError("Database migration history contains duplicate versions.")
            history[version] = name
        return history


__all__ = [
    "Migration",
    "MigrationCallback",
    "MigrationDefinitionError",
    "MigrationError",
    "MigrationExecutionError",
    "MigrationRunner",
    "MigrationStateError",
]


def _copy_bounded_migrations(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    """Copy a migration sequence without trusting a custom sequence's reported length."""
    definitions: list[Migration] = []
    iterator = iter(migrations)
    for _ in range(_MAX_MIGRATIONS + 1):
        try:
            definitions.append(next(iterator))
        except StopIteration:
            return tuple(definitions)
    raise MigrationDefinitionError("A migration set cannot contain more than 10,000 entries.")
