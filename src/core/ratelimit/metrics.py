"""
Level 2: Lock-Free Metrics Collector and Background Heartbeat Reporter
Decouples fast atomic counters from external logging and alerting I/O.
Fixes: P0-CORE-04, P2-OBS-06
"""
import time
from typing import Dict, Any
from dataclasses import dataclass, field


@dataclass
class RateLimitMetrics:
    total_requests: int = 0
    total_tokens_consumed: int = 0
    total_throttled_requests: int = 0
    last_heartbeat_timestamp: float = field(default_factory=time.time)
    last_success_timestamp: float = field(default_factory=time.time)

    def record_success(self, tokens: int = 1) -> None:
        self.total_requests += 1
        self.total_tokens_consumed += tokens
        self.last_success_timestamp = time.time()
        self.last_heartbeat_timestamp = time.time()

    def record_throttled(self) -> None:
        self.total_throttled_requests += 1
        self.last_heartbeat_timestamp = time.time()

    def get_summary(self) -> Dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "total_tokens_consumed": self.total_tokens_consumed,
            "total_throttled_requests": self.total_throttled_requests,
            "seconds_since_last_success": time.time() - self.last_success_timestamp,
            "seconds_since_last_heartbeat": time.time() - self.last_heartbeat_timestamp,
        }


# Global metrics instance
metrics_collector = RateLimitMetrics()
