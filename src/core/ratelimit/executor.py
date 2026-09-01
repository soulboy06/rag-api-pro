"""
Level 2: Safe Async Executor for Synchronous & CPU-bound Tasks
Isolates blocking sync functions and heavy regex CPU processing to dedicated thread pools.
Prevents starvation or freezing of the main FastAPI event loop.
Fixes: P0-CORE-03, P1-PARSER-07
"""
import asyncio
from typing import Callable, TypeVar, Any
from concurrent.futures import ThreadPoolExecutor

T = TypeVar("T")

# Dedicated bounded worker pool for sync tasks (prevents unbounded thread creation)
_SYNC_TASK_POOL = ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix="rag-sync-worker"
)


class SafeAsyncExecutor:
    @classmethod
    async def run_sync(
        cls,
        func: Callable[..., T],
        *args: Any,
        timeout: float = 60.0,
        **kwargs: Any
    ) -> T:
        """
        Executes a synchronous or CPU-intensive function in the dedicated thread pool.
        Guarantees non-blocking event loop execution and applies a strict timeout.
        """
        loop = asyncio.get_running_loop()
        call_with_kwargs = lambda: func(*args, **kwargs)

        try:
            future = loop.run_in_executor(_SYNC_TASK_POOL, call_with_kwargs)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Synchronous execution timed out after {timeout}s")

    @classmethod
    def shutdown_pool(cls, wait: bool = False) -> None:
        """Gracefully terminates worker threads upon server shutdown."""
        _SYNC_TASK_POOL.shutdown(wait=wait)
