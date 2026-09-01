"""
Level 4: Bounded Tenant Runtime Cache with LRU Eviction & Reference Pinning
Manages tenant connections and runtime states with capacity limits and in-use protection.
Fixes: P1-CORE-01, P1-CORE-02, P1-CORE-03, P1-CORE-04, P3-CODE-03
"""
import time
import asyncio
import inspect
from collections import OrderedDict
from typing import Dict, Any, Optional, Callable, Generic, TypeVar
from dataclasses import dataclass, field

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    key: str
    value: T
    ref_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)


class BoundedTenantCache(Generic[T]):
    """
    Thread-safe, bounded LRU cache for tenant runtime resources.
    Protects currently executing requests from eviction via reference pinning.
    """

    def __init__(self, max_capacity: int = 100, on_evict: Optional[Callable[[str, T], Any]] = None):
        if max_capacity <= 0:
            raise ValueError("max_capacity must be greater than 0")
        self.max_capacity = max_capacity
        self.on_evict = on_evict
        self._cache: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._inflight: Dict[str, asyncio.Future[T]] = {}

    async def get_or_set(self, key: str, factory: Callable[[], T]) -> T:
        """Gets or creates one value without running user code under the lock."""
        creator = False
        creation_future: asyncio.Future[T]
        async with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                entry.last_accessed = time.time()
                self._cache.move_to_end(key)
                return entry.value

            creation_future = self._inflight.get(key)
            if creation_future is None:
                creation_future = asyncio.get_running_loop().create_future()
                self._inflight[key] = creation_future
                creator = True

        if not creator:
            return await creation_future

        try:
            async with self._lock:
                evicted = self._pop_lru_unlocked()
            await self._release(evicted)

            val = factory()
            if inspect.isawaitable(val):
                val = await val

            async with self._lock:
                # Another key may have been created while this factory was
                # running. Keep the cache strictly bounded at insertion time.
                evicted_after_factory = self._pop_lru_unlocked()
                self._cache[key] = CacheEntry(key=key, value=val)
                self._inflight.pop(key, None)
                if not creation_future.done():
                    creation_future.set_result(val)
            await self._release(evicted_after_factory)
            return val
        except Exception as exc:
            async with self._lock:
                self._inflight.pop(key, None)
                if not creation_future.done():
                    creation_future.set_exception(exc)
            raise

    async def pin(self, key: str) -> Optional[T]:
        """Pins a tenant resource, incrementing ref count to protect from LRU eviction."""
        async with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                entry.ref_count += 1
                entry.last_accessed = time.time()
                self._cache.move_to_end(key)
                return entry.value
            return None

    async def unpin(self, key: str) -> None:
        """Unpins a tenant resource, decrementing ref count."""
        async with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                entry.ref_count = max(0, entry.ref_count - 1)
                entry.last_accessed = time.time()

    async def size(self) -> int:
        async with self._lock:
            return len(self._cache)

    async def clear(self) -> None:
        """Clears cache and invokes on_evict on all items."""
        async with self._lock:
            entries = list(self._cache.values())
            self._cache.clear()
        for entry in entries:
            await self._release((entry.key, entry.value))

    def _pop_lru_unlocked(self):
        """Pops the least recently used unpinned entry, if one exists."""
        if len(self._cache) < self.max_capacity:
            return None

        # Find oldest entry with ref_count == 0
        candidate_key = None
        for k, entry in self._cache.items():
            if entry.ref_count == 0:
                candidate_key = k
                break

        if candidate_key is not None:
            evicted_entry = self._cache.pop(candidate_key)
            return candidate_key, evicted_entry.value
        else:
            # Never violate the configured bound. Callers can retry after an
            # active request unpins an entry.
            raise RuntimeError("Tenant runtime cache capacity exhausted: all entries are pinned")

    async def _release(self, evicted) -> None:
        """Runs resource release after the cache lock has been released."""
        if evicted is None or not self.on_evict:
            return
        key, value = evicted
        try:
            result = self.on_evict(key, value)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Eviction must not corrupt cache bookkeeping. Production callers
            # should emit their own release-failure metric/log from the hook.
            return
