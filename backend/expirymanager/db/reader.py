"""Reader helpers for the analytic store.

Readers take a fresh cursor off the single shared instance and run it in the thread pool. Never
call DuckDB directly from an ``async def`` handler: a 200 ms scan on the event loop thread stalls
every other request, and DuckDB releases the GIL, so the thread pool buys real parallelism.

A cursor is cheap, it allocates a client context rather than a file handle, so there is no pool
here. A semaphore bounds concurrent reader threads instead, which avoids the classic pool bug of
leaking a cursor on an exception path. Measured reader throughput plateaus around six threads,
and stays there while the writer is committing, because DuckDB's MVCC gives readers a snapshot
without taking a lock the writer needs.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Sequence, TypeVar

import pyarrow as pa
import duckdb
from starlette.concurrency import run_in_threadpool

from expirymanager.db.duck import guard_statement

if TYPE_CHECKING:
    from expirymanager.db.duck import DuckStore

T = TypeVar("T")


class DuckReader:
    """Bounded read access to the shared DuckDB instance."""

    def __init__(self, store: DuckStore, *, concurrency: int = 6) -> None:
        self._store = store
        self.concurrency = concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _gate(self) -> asyncio.Semaphore:
        """The semaphore for the running loop.

        A semaphore belongs to the loop that awaits it, and this reader is built lazily off
        DuckStore before any loop exists, so it is created on first use rather than in __init__.
        """
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._semaphore = asyncio.Semaphore(self.concurrency)
            self._loop = loop
        return self._semaphore

    async def run(self, fn: Callable[[duckdb.DuckDBPyConnection], T]) -> T:
        """Run ``fn`` against a fresh cursor. The cursor is closed on every exit path."""
        async with self._gate():

            def call() -> T:
                cur = self._store.cursor()
                try:
                    return fn(cur)
                finally:
                    cur.close()

            return await run_in_threadpool(call)

    async def fetch_all(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        guard_statement(sql, self._store.db_path)
        return await self.run(lambda cur: cur.execute(sql, list(params or [])).fetchall())

    async def fetch_one(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> tuple[Any, ...] | None:
        guard_statement(sql, self._store.db_path)
        return await self.run(lambda cur: cur.execute(sql, list(params or [])).fetchone())

    async def fetch_value(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = await self.fetch_one(sql, params)
        return None if row is None else row[0]

    async def fetch_columns(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Rows plus their column names, for the columnar bars response shape."""
        guard_statement(sql, self._store.db_path)

        def call(cur: duckdb.DuckDBPyConnection) -> tuple[list[str], list[tuple[Any, ...]]]:
            result = cur.execute(sql, list(params or []))
            names = [description[0] for description in result.description]
            return (names, result.fetchall())

        return await self.run(call)

    async def fetch_arrow(self, sql: str, params: Sequence[Any] | None = None) -> pa.Table:
        """Arrow out, for exports and for any vectorised consumer.

        Arrow avoids a pandas materialisation and keeps DECIMAL as decimal128 rather than
        collapsing it to float64.
        """
        guard_statement(sql, self._store.db_path)
        return await self.run(lambda cur: cur.execute(sql, list(params or [])).arrow())
