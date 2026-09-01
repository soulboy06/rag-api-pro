"""
Level 8: Enterprise Observability & Prometheus Metrics Collector
Provides RED metrics (Rate, Errors, Duration), token telemetry, queue depth monitoring,
and storage consistency drift counters.
Fixes: P2-OBS-01, P2-OBS-02, P2-OBS-03, P2-OBS-07
"""
from typing import Optional, Dict, Any
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    CollectorRegistry,
    generate_latest,
    CONTENT_TYPE_LATEST,
    REGISTRY
)


class PrometheusMetrics:
    """Singleton Prometheus Metrics Manager."""
    
    _instance: Optional["PrometheusMetrics"] = None

    def __init__(self, registry: Optional[CollectorRegistry] = None):
        self.registry = registry or REGISTRY

        # 1. RED Metrics: Request Total & Duration
        self.http_requests_total = Counter(
            "rag_requests_total",
            "Total number of HTTP requests processed by the RAG Gateway",
            ["tenant_id", "endpoint", "status_code", "mode"],
            registry=self.registry
        )

        self.http_request_duration_seconds = Histogram(
            "rag_request_duration_seconds",
            "HTTP request latency distribution in seconds",
            ["tenant_id", "endpoint", "mode"],
            buckets=(0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
            registry=self.registry
        )

        # 2. Token Telemetry
        self.token_usage_total = Counter(
            "rag_token_usage_total",
            "Accumulated LLM token usage",
            ["tenant_id", "model", "token_type"],  # token_type: prompt, completion, total
            registry=self.registry
        )

        # 3. Queue & Ingestion Telemetry
        self.task_queue_depth = Gauge(
            "rag_task_queue_depth",
            "Current pending / unacknowledged messages in Redis Streams",
            ["stream_name", "group_name"],
            registry=self.registry
        )

        # 4. Retrieval & Knowledge Base Telemetry
        self.retrieval_chunks_total = Counter(
            "rag_retrieval_chunks_total",
            "Total chunks retrieved across all GraphRAG queries",
            ["tenant_id", "mode"],
            registry=self.registry
        )

        # 5. Storage Drift & Reconciliation Telemetry
        self.reconciliation_drift_total = Counter(
            "rag_storage_reconciliation_drift_total",
            "Total storage inconsistency drifts detected by StorageReconciler",
            ["storage_type", "drift_type"],
            registry=self.registry
        )

        # 6. Active Tenants Gauge
        self.active_tenants = Gauge(
            "rag_active_tenants_count",
            "Count of active provisioned tenants",
            registry=self.registry
        )

    @classmethod
    def get_instance(cls) -> "PrometheusMetrics":
        if cls._instance is None:
            cls._instance = PrometheusMetrics()
        return cls._instance

    def record_request(
        self,
        tenant_id: str,
        endpoint: str,
        status_code: int,
        duration_seconds: float,
        mode: str = "none"
    ) -> None:
        """Records HTTP request rate, status code, and latency distribution."""
        clean_tenant = tenant_id or "anonymous"
        clean_endpoint = endpoint.split("?")[0]
        self.http_requests_total.labels(
            tenant_id=clean_tenant,
            endpoint=clean_endpoint,
            status_code=str(status_code),
            mode=mode
        ).inc()

        self.http_request_duration_seconds.labels(
            tenant_id=clean_tenant,
            endpoint=clean_endpoint,
            mode=mode
        ).observe(duration_seconds)

    def record_token_usage(
        self,
        tenant_id: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0
    ) -> None:
        """Records token consumption broken down by prompt and completion."""
        clean_tenant = tenant_id or "default"
        clean_model = model or "glm-4-flash"
        if prompt_tokens > 0:
            self.token_usage_total.labels(
                tenant_id=clean_tenant,
                model=clean_model,
                token_type="prompt"
            ).inc(prompt_tokens)
        if completion_tokens > 0:
            self.token_usage_total.labels(
                tenant_id=clean_tenant,
                model=clean_model,
                token_type="completion"
            ).inc(completion_tokens)

    def record_reconciliation_drift(self, storage_type: str, drift_type: str, count: int = 1) -> None:
        """Records detected data consistency drift across storage tiers."""
        self.reconciliation_drift_total.labels(
            storage_type=storage_type,
            drift_type=drift_type
        ).inc(count)

    def export_metrics(self) -> bytes:
        """Generates Prometheus text representation of all collected metrics."""
        return generate_latest(self.registry)


# Global singleton instance
prometheus_metrics = PrometheusMetrics.get_instance()
