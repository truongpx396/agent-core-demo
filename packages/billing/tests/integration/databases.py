"""A throwaway database inside the package's shared test container (spec 012), for the integration tests that must not touch `appdata`.

Each test that uses one gets its OWN database (`CREATE DATABASE itest_...`, dropped afterwards), so re-applying DDL cannot deadlock with
another test's writes (the repo's pattern 60) and one test's rows can never be seen by another.
"""
import re
import uuid
from contextlib import asynccontextmanager

import psycopg

from credit_billing import schema

NAMES = ["20-credit-wallet.sql", "22-billing.sql"]
WALLET_TABLES = ("credit_accounts", "credit_lots", "credit_transactions", "credit_entries")


class Throwaway:
    """One database, created for one test and dropped after it."""

    def __init__(self, container_url: str) -> None:
        self.name = f"itest_{uuid.uuid4().hex[:10]}"
        self._admin = re.sub(r"/appdata$", "/langfuse", container_url)
        self.url = re.sub(r"/appdata$", f"/{self.name}", container_url)

    async def create(self) -> "Throwaway":
        async with await psycopg.AsyncConnection.connect(self._admin, autocommit=True) as conn:
            await conn.execute(f"CREATE DATABASE {self.name}")
        return self

    async def drop(self) -> None:
        async with await psycopg.AsyncConnection.connect(self._admin, autocommit=True) as conn:
            await conn.execute(f"DROP DATABASE IF EXISTS {self.name} WITH (FORCE)")

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(self.url) as conn:  # commits on a clean exit, rolls back on an exception
            yield conn

    async def scalar(self, sql, *params):
        async with self.connection() as conn:
            cur = await conn.execute(sql, params)
            return (await cur.fetchone())[0]

    async def rows(self, sql, *params):
        async with self.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def exists(self, table: str) -> bool:
        return await self.scalar("SELECT to_regclass(%s::text) IS NOT NULL", table)

    async def snapshot(self) -> dict[str, list]:
        """Every row of the wallet tables, in a stable order: what must be identical after an adoption."""
        return {table: await self.rows(f"SELECT * FROM {table} ORDER BY 1, 2") for table in WALLET_TABLES}


async def open_connection(database: Throwaway) -> psycopg.AsyncConnection:
    """A connection the test closes itself: one that holds a lock or a transaction open on purpose."""
    return await psycopg.AsyncConnection.connect(database.url)


async def create_database(container_url: str) -> Throwaway:
    return await Throwaway(container_url).create()


async def init_like_the_host(database: Throwaway) -> None:
    """What the container's init does on a fresh volume: run each shipped script, record nothing."""
    for name in NAMES:
        async with database.connection() as conn:
            await conn.execute(schema.executable(name))
