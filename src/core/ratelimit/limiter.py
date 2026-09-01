"""
Level 2: Deadlock-Free Dual Sliding Window Rate Limiter
Enforces dual-dimension quotas (Requests Per Minute & Tokens Per Minute).
Fixes: P0-REL-03 (No asyncio.sleep inside critical section), P1-CORE-05, P1-REMOTE-05
"""
import time
import asyncio
from typing import Dict, Tuple, Optional
from collections import deque
from src.core.exceptions import RateLimitExceededError


class SlidingWindow:
    def __init__(self, window_size_seconds: float = 60.0):
        self.window_size = window_size_seconds
        # Deque of tuples: (timestamp, count_or_tokens)
        self.events = deque()

    def clean_expired(self, current_time: float) -> None:
        cutoff = current_time - self.window_size
        while self.events and self.events[0][0] <= cutoff:
            self.events.popleft()

    def get_total(self, current_time: float) -> int:
        self.clean_expired(current_time)
        return sum(item[1] for item in self.events)

    def add_event(self, current_time: float, count_or_tokens: int) -> None:
        self.clean_expired(current_time)
        self.events.append((current_time, count_or_tokens))

    def time_until_available(self, current_time: float, limit: int, needed: int) -> float:
        """Calculates seconds until sufficient capacity becomes available."""
        self.clean_expired(current_time)
        current_total = sum(item[1] for item in self.events)
        if current_total + needed <= limit:
            return 0.0

        needed_freed = (current_total + needed) - limit
        freed = 0
        for ts, val in self.events:
            freed += val
            if freed >= needed_freed:
                # Time when this event expires from window
                wait_time = (ts + self.window_size) - current_time
                return max(0.01, wait_time)

        return self.window_size


class DualWindowRateLimiter:
    """
    Thread-safe & Async Deadlock-Free Rate Limiter for LLM/Embedding API calls.
    - RPM: Max Requests Per Minute
    - TPM: Max Tokens Per Minute
    - Critical section is ultra-short (microseconds), sleep is performed OUTSIDE the lock.
    """
    def __init__(
        self,
        max_rpm: int = 1000,
        max_tpm: int = 2_000_000,
        window_size_seconds: float = 60.0,
        default_timeout: float = 120.0,
        max_scopes: int = 10_000,
    ):
        self.max_rpm = max_rpm
        self.max_tpm = max_tpm
        self.window_size = window_size_seconds
        self.default_timeout = default_timeout
        if max_rpm <= 0 or max_tpm <= 0 or window_size_seconds <= 0:
            raise ValueError("Rate limiter limits and window must be positive")
        if max_scopes <= 0:
            raise ValueError("max_scopes must be greater than 0")
        self.max_scopes = max_scopes

        # Per-(service, tenant) sliding windows. Keeping the service in the
        # scope prevents OCR, embedding, LLM, and layout calls from silently
        # consuming one another's quota.
        self._rpm_windows: Dict[Tuple[str, str], SlidingWindow] = {}
        self._tpm_windows: Dict[Tuple[str, str], SlidingWindow] = {}
        self._lock = asyncio.Lock()

    def _get_or_create_windows(
        self,
        tenant_id: str,
        service: str,
        current_time: float,
    ) -> Tuple[SlidingWindow, SlidingWindow]:
        scope = (service, tenant_id)
        if scope not in self._rpm_windows:
            if len(self._rpm_windows) >= self.max_scopes:
                # Remove expired scopes before rejecting a new untrusted
                # tenant/service pair; this keeps the limiter itself bounded.
                expired = [
                    key for key, window in self._rpm_windows.items()
                    if not window.events or window.events[-1][0] <= current_time - self.window_size
                ]
                for key in expired[: max(1, len(expired))]:
                    self._rpm_windows.pop(key, None)
                    self._tpm_windows.pop(key, None)
            if len(self._rpm_windows) >= self.max_scopes:
                raise RateLimitExceededError("Rate limiter scope capacity exhausted")
            self._rpm_windows[scope] = SlidingWindow(self.window_size)
            self._tpm_windows[scope] = SlidingWindow(self.window_size)
        return self._rpm_windows[scope], self._tpm_windows[scope]

    async def acquire(
        self,
        tenant_id: str = "global",
        estimated_tokens: int = 1,
        timeout_seconds: Optional[float] = None,
        block: bool = True,
        service: str = "default",
    ) -> bool:
        """
        Acquires quota for 1 request and estimated_tokens.
        If quota is exhausted and block=True:
          - Releases lock immediately
          - Sleeps in loop outside lock
          - Re-evaluates until acquired or timed out
        """
        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens cannot be negative")
        if estimated_tokens > self.max_tpm:
            raise RateLimitExceededError(
                f"Requested token budget ({estimated_tokens}) exceeds the configured TPM limit"
            )
        start_time = time.monotonic()

        while True:
            # 1. ULTRA-SHORT CRITICAL SECTION
            wait_seconds = 0.0
            allowed = False

            async with self._lock:
                now = time.time()
                rpm_win, tpm_win = self._get_or_create_windows(
                    tenant_id,
                    service,
                    now,
                )

                curr_reqs = rpm_win.get_total(now)
                curr_tokens = tpm_win.get_total(now)

                if (curr_reqs + 1 <= self.max_rpm) and (curr_tokens + estimated_tokens <= self.max_tpm):
                    # Quota granted
                    rpm_win.add_event(now, 1)
                    tpm_win.add_event(now, estimated_tokens)
                    allowed = True
                else:
                    # Calculate required wait time
                    rpm_wait = rpm_win.time_until_available(now, self.max_rpm, 1)
                    tpm_wait = tpm_win.time_until_available(now, self.max_tpm, estimated_tokens)
                    wait_seconds = max(rpm_wait, tpm_wait)

            # 2. LOCK RELEASED!
            if allowed:
                return True

            if not block:
                raise RateLimitExceededError(
                    f"Rate limit exceeded for tenant '{tenant_id}'. Capacity unavailable."
                )

            # Check timeout
            elapsed = time.monotonic() - start_time
            remaining_time = timeout - elapsed
            if remaining_time <= 0 or wait_seconds > remaining_time:
                raise RateLimitExceededError(
                    f"Rate limit timeout ({timeout}s) exceeded for tenant '{tenant_id}'. Need wait {wait_seconds:.2f}s."
                )

            # 3. SLEEP OUTSIDE CRITICAL SECTION (DEADLOCK-FREE)
            sleep_duration = min(wait_seconds, remaining_time, 1.0)
            await asyncio.sleep(sleep_duration)


# Global default rate limiter instance for API providers
global_rate_limiter = DualWindowRateLimiter(max_rpm=1000, max_tpm=2_000_000, default_timeout=120.0)
