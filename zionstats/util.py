"""Small shared utilities: the NaType safe-arithmetic sentinel and a
minimal TTL cache.  No dependencies beyond the standard library."""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from typing import Any


# Ported from nvitop/api/constants.py (Apache-2.0)
# Copyright (c) 2021 Xuehai Pan.  https://github.com/XuehaiPan/nvitop
# Modifications for zionstats: py3.9 floor; docstring adapted.
class _NaType(float):
    """A float that is NaN: every comparison is False and it renders as
    ``N/A``.  Lets metric code do ``value or NaType()``-style arithmetic
    without None-checks.  Note: NaN is truthy -- use ``is NaType`` or
    ``value != value`` to test for it."""

    def __new__(cls) -> _NaType:
        return super().__new__(cls, "nan")

    def __repr__(self) -> str:
        return "N/A"

    def __str__(self) -> str:
        return "N/A"


NaType: _NaType = _NaType()


class TTLCache:
    """Tiny maxsize+TTL cache (concept: nvitop's use of
    ``cachetools.TTLCache``; this is an original stdlib-only implementation).

    `clock` is injectable (monotonic by default) so tests can drive expiry
    without sleeping -- same pattern as CoolingController's injectable clock.
    Reads/writes are not lock-protected: the TUI owns the only thread that
    touches this."""

    def __init__(self, maxsize: int = 128, ttl: float = 60.0,
                 clock: Callable[[], float] | None = None) -> None:
        self._maxsize = max(1, int(maxsize))
        self._ttl = float(ttl)
        self._clock = clock if clock is not None else time.monotonic
        self._data: dict[Hashable, tuple[float, Any]] = {}

    def get(self, key: Hashable, default: Any = None) -> Any:
        hit = self._data.get(key)
        if hit is None:
            return default
        ts, value = hit
        if self._clock() - ts > self._ttl:
            del self._data[key]
            return default
        return value

    def __getitem__(self, key: Hashable) -> Any:
        v = self.get(key, _MISSING)
        if v is _MISSING:
            raise KeyError(key)
        return v

    def __setitem__(self, key: Hashable, value: Any) -> None:
        self._data[key] = (self._clock(), value)
        if len(self._data) > self._maxsize:
            # evict oldest first (insertion order == oldest stamp under a
            # monotonic clock; re-set refreshes both)
            oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])
            for k, _ in oldest[: len(self._data) - self._maxsize]:
                del self._data[k]

    def __contains__(self, key: object) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def expire(self) -> int:
        """Drop expired entries; returns how many were removed."""
        now = self._clock()
        dead = [k for k, (ts, _v) in self._data.items() if now - ts > self._ttl]
        for k in dead:
            del self._data[k]
        return len(dead)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


_MISSING = object()
