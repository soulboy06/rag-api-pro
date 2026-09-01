"""
Level 6: Native Token-Level Server-Sent Events (SSE) Streaming Engine
Streams real LLM delta tokens, filters internal thinking chains, and formats standard SSE data frames.
Fixes: P1-API-02, P1-API-03, P1-API-04, P3-CODE-04
"""
import json
import time
import asyncio
import re
from typing import AsyncGenerator, Dict, Any, List, Optional
import httpx

from src.core.config import settings
from src.core.clients import InfrastructureClients
from src.core.ratelimit import TokenCounter, global_rate_limiter, metrics_collector
from src.core.monitoring.metrics import prometheus_metrics
from src.models.schemas import SourceChunk, GraphEntity
from src.core.logger import request_id_ctx, get_logger

logger = get_logger(__name__)


class SSEStreamGenerator:
    """
    Constructs compliant Server-Sent Events (SSE) data streams.
    """

    @staticmethod
    def format_sse_event(event_type: str, data: Dict[str, Any]) -> str:
        """Formats standard SSE event frame: `event: {type}\ndata: {json}\n\n`"""
        json_payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event_type}\ndata: {json_payload}\n\n"

    @staticmethod
    def _filter_thinking_delta(delta: str, state: Dict[str, Any]) -> str:
        """Remove ``<think>...</think>`` across arbitrary provider chunks.

        Providers are free to split a tag over multiple SSE frames. Keeping
        a small suffix buffer prevents internal reasoning from leaking when a
        delimiter arrives as ``<th`` + ``ink>`` (or the closing equivalent).
        """
        opening = "<think>"
        closing = "</think>"
        pending = state.get("pending", "") + delta
        in_thinking = bool(state.get("in_thinking", False))
        visible: List[str] = []

        def longest_prefix_suffix(value: str, marker: str) -> int:
            max_len = min(len(value), len(marker) - 1)
            for length in range(max_len, 0, -1):
                if value.endswith(marker[:length]):
                    return length
            return 0

        while pending:
            marker = closing if in_thinking else opening
            marker_index = pending.find(marker)
            if marker_index >= 0:
                if not in_thinking:
                    visible.append(pending[:marker_index])
                pending = pending[marker_index + len(marker):]
                in_thinking = not in_thinking
                continue

            suffix_len = longest_prefix_suffix(pending, marker)
            if in_thinking:
                # Discard thinking content but retain a possible partial
                # closing marker for the next frame.
                pending = pending[-suffix_len:] if suffix_len else ""
            else:
                # Emit ordinary text immediately and retain only a possible
                # partial opening marker.
                if suffix_len:
                    visible.append(pending[:-suffix_len])
                    pending = pending[-suffix_len:]
                else:
                    visible.append(pending)
                    pending = ""
            break

        state["pending"] = pending
        state["in_thinking"] = in_thinking
        return "".join(visible)

    @staticmethod
    def _flush_thinking_filter(state: Dict[str, Any]) -> str:
        """Flush non-thinking text left in the delimiter buffer at EOF."""
        if state.get("in_thinking", False):
            state["pending"] = ""
            return ""
        pending = state.get("pending", "")
        state["pending"] = ""
        return pending

    @classmethod
    async def stream_query_response(
        cls,
        prompt: str,
        system_prompt: str,
        sources: List[SourceChunk],
        entities: List[GraphEntity],
        tenant_id: str = "global",
        model: Optional[str] = None,
        temperature: float = 0.3,
        start_time: float = 0.0
    ) -> AsyncGenerator[str, None]:
        """
        Executes real streaming token generation and yields SSE events in strict sequence:
        1. status (RETRIEVAL_COMPLETE)
        2. sources (Grounded citations and graph entities)
        3. status (GENERATING)
        4. token (Real incremental deltas, filtered of <think> tags)
        5. done (Execution metrics)
        """
        # 1. Yield initial status
        yield cls.format_sse_event(
            "status",
            {"stage": "RETRIEVAL", "message": "多路知识图谱与向量库检索完成，正在组织参考上下文..."}
        )

        # 2. Yield Grounded Sources
        sources_data = [s.model_dump() for s in sources]
        entities_data = [e.model_dump() for e in entities]
        yield cls.format_sse_event(
            "sources",
            {
                "sources": sources_data,
                "entities": entities_data,
                "count": len(sources_data)
            }
        )

        # 3. Yield Generating status
        yield cls.format_sse_event(
            "status",
            {"stage": "GENERATING", "message": "大模型正在生成深度解答..."}
        )

        # 4. Stream LLM tokens
        est_tokens = TokenCounter.estimate_text_tokens(prompt + system_prompt)
        await global_rate_limiter.acquire(
            tenant_id=tenant_id,
            estimated_tokens=est_tokens,
            service="llm",
        )
        stream_failed = False

        # Check if real API is available
        if not settings.USE_MOCK_MODELS and settings.OPENAI_API_KEY:
            accumulated_tokens = 0
            thinking_filter_state: Dict[str, Any] = {
                "in_thinking": False,
                "pending": "",
            }

            try:
                headers = {
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": model or settings.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": temperature,
                    "stream": True
                }

                client = await InfrastructureClients.get_http_client()
                async with client.stream("POST", f"{settings.OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=120.0) as response:
                    response.raise_for_status()
                    async for raw_line in response.aiter_lines():
                        if not raw_line or not raw_line.startswith("data: "):
                            continue
                        data_str = raw_line[6:].strip()
                        if data_str == "[DONE]":
                            break

                        try:
                            chunk_json = json.loads(data_str)
                            delta = chunk_json.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if not delta:
                                continue

                            # Filter internal reasoning chain, including
                            # tags split across provider SSE frames.
                            visible_delta = cls._filter_thinking_delta(
                                delta,
                                thinking_filter_state,
                            )
                            if not visible_delta:
                                continue

                            accumulated_tokens += 1
                            yield cls.format_sse_event("token", {"delta": visible_delta})
                        except Exception:
                            continue

                trailing_delta = cls._flush_thinking_filter(thinking_filter_state)
                if trailing_delta:
                    accumulated_tokens += 1
                    yield cls.format_sse_event("token", {"delta": trailing_delta})

                metrics_collector.record_success(tokens=accumulated_tokens)
                prometheus_metrics.record_token_usage(
                    tenant_id=tenant_id or "default",
                    model=model or settings.LLM_MODEL,
                    completion_tokens=accumulated_tokens
                )

            except Exception as stream_err:
                stream_failed = True
                logger.warning("LLM streaming failed", extra={"error": str(stream_err)})
                yield cls.format_sse_event(
                    "error",
                    {
                        "error_code": "STREAMING_ERROR",
                        "message": "流式生成暂时失败，请稍后重试",
                        "request_id": request_id_ctx.get(),
                    },
                )
                metrics_collector.record_rejection(reason="streaming_error")
        else:
            # Fallback mock streaming for tests / offline mode
            mock_answer = "【系统生成解答】: 根据提供的参考上下文与知识图谱关系，本系统成功完成了多路召回与融合回答。"
            words = list(mock_answer)
            for w in words:
                await asyncio.sleep(0.01)
                yield cls.format_sse_event("token", {"delta": w})
            metrics_collector.record_success(tokens=len(words))
            prometheus_metrics.record_token_usage(
                tenant_id=tenant_id or "default",
                model=model or settings.LLM_MODEL,
                completion_tokens=len(words)
            )

        # 5. Yield Done
        exec_time = round((time.perf_counter() - start_time) * 1000, 2) if start_time > 0 else 50.0
        yield cls.format_sse_event(
            "done",
            {
                "execution_time_ms": exec_time,
                "status": "FAILED" if stream_failed else "COMPLETED"
            }
        )
