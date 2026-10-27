"""A small SQLite cache for public API responses, so busy pages don't hammer the sources.

Only public data goes in here (races, candidates, money, fact checks). Anything keyed by a
voter's address is never cached.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL, value TEXT)")
        self.inflight: dict[str, asyncio.Future] = {}

    def get(self, key: str, stale: bool = False) -> Any | None:
        row = self.db.execute("SELECT expires, value FROM cache WHERE key = ?", (key,)).fetchone()
        if row and (stale or row[0] > time.time()):
            return json.loads(row[1])
        return None

    def put(self, key: str, value: Any, ttl: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, time.time() + ttl, json.dumps(value)))

    async def fetch(self, key: str, ttl: float, load: Callable[[], Awaitable[Any]]) -> Any:
        """Cached value, else load() once even when many requests ask at the same moment.
        If the source fails, an expired copy is better than an error page."""
        hit = self.get(key)
        if hit is not None:
            return hit
        if key in self.inflight:
            return await asyncio.shield(self.inflight[key])
        fut = asyncio.get_running_loop().create_future()
        self.inflight[key] = fut
        try:
            value = await load()
            self.put(key, value, ttl)
            fut.set_result(value)
            return value
        except Exception as e:
            old = self.get(key, stale=True)
            if old is not None:
                fut.set_result(old)
                return old
            fut.set_exception(e)
            raise
        finally:
            del self.inflight[key]
            if fut.done() and not fut.cancelled():
                fut.exception()  # mark retrieved so asyncio doesn't log it
