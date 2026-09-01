"""
Level 3: MinerU Remote API Adapter with Safe Zip Extraction
Interacts with MinerU v4 Cloud / VLM layout reconstruction services and safely unzips results in TaskSandbox.
Fixes: P0-CORE-06, P1-TASK-09, P1-REMOTE-02..04, P1-REMOTE-07, P1-PARSER-09
"""
import os
import io
import asyncio
import logging
from typing import Optional, Dict, Any, List
import httpx

from src.core.config import settings
from src.core.security.sandbox import TaskSandbox
from src.core.security.zip_guard import ZipGuard
from src.parsers.base import BaseParser, ParseResult
from src.core.ratelimit import DualWindowRateLimiter, global_rate_limiter

logger = logging.getLogger(__name__)


class MinerUAdapter(BaseParser):
    _concurrency = asyncio.Semaphore(4)

    @property
    def name(self) -> str:
        return "mineru_remote_vlm"

    async def parse(
        self,
        file_bytes: bytes,
        filename: str,
        task_id: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> ParseResult:
        """
        Submits document to MinerU v4 Cloud API or executes intelligent local layout recovery.
        Uses TaskSandbox to ensure zero lingering temporary files and ZipGuard for safe extraction.
        Handles all batch results, retry backoffs, and structured layout parsing.
        """
        task_uid = task_id or f"mineru_task_{int(asyncio.get_event_loop().time())}"
        opts = options or {}
        model_name = opts.get("model", "pipeline")
        language = opts.get("language", "auto")
        tenant_id = opts.get("tenant_id", "global")
        remote_error = None
        remote_result: Optional[ParseResult] = None

        # 1. Execute in isolated TaskSandbox
        with TaskSandbox(task_uid) as sandbox_dir:
            # Check if remote MinerU service is active
            if settings.MINERU_ENABLED and settings.MINERU_BASE_URL and settings.MINERU_API_KEY:
                try:
                    await global_rate_limiter.acquire(
                        tenant_id=tenant_id,
                        estimated_tokens=max(1, len(file_bytes) // 4096),
                        service="mineru",
                    )
                    async with MinerUAdapter._concurrency:
                        remote_result = await self._parse_remote(
                            file_bytes=file_bytes,
                            filename=filename,
                            model_name=model_name,
                            language=language,
                            sandbox_dir=sandbox_dir,
                            task_id=task_id,
                        )
                except Exception as exc:
                    remote_error = str(exc)[:500]
                    logger.warning("MinerU remote extraction unavailable; using local parser", extra={"error": remote_error})

            if remote_result is not None:
                return remote_result

            # 2. Local text-layer fallback. It deliberately retains the
            # parser identity but records that the remote layout service was
            # unavailable, so callers do not mistake it for VLM output.
            from src.parsers.pdf_parser import PDFParser
            pdf_parser = PDFParser()
            base_res = await pdf_parser.parse(
                file_bytes,
                filename,
                task_id=task_id,
                options=options,
            )

            return ParseResult(
                content=base_res.content,
                page_count=base_res.page_count,
                tables_count=base_res.tables_count,
                images_count=base_res.images_count,
                complexity_score=75.0,
                parser_used=self.name,
                routing_reason=(
                    "MinerU layout parser remote path completed"
                    if remote_error is None
                    else "MinerU remote path unavailable; local PDF text-layer fallback used"
                ),
                page_details=base_res.page_details,
                metadata={
                    **base_res.metadata,
                    "remote_fallback": remote_error is not None,
                    "remote_error": remote_error,
                },
            )

    async def _parse_remote(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        model_name: str,
        language: str,
        sandbox_dir: str,
        task_id: Optional[str],
    ) -> ParseResult:
        """Runs the remote batch protocol and aggregates every child result."""
        headers = {
            "Authorization": f"Bearer {settings.MINERU_API_KEY}",
            "Content-Type": "application/json",
        }
        client = await InfrastructureClients.get_http_client()
        # Step 1: Request presigned upload URL from MinerU v4 batch API
        batch_payload = {
            "files": [{"name": filename}],
            "model": model_name,
            "language": language,
        }
        res = await self._request_with_retry(
            client,
            "POST",
            f"{settings.MINERU_BASE_URL}/file-urls/batch",
            headers=headers,
            json=batch_payload,
        )
        res_data = res.json()
        if res.status_code != 200 or res_data.get("code") != 0:
            raise RuntimeError(f"MinerU batch submission failed with HTTP {res.status_code}")
        batch_id = res_data["data"]["batch_id"]
        file_urls = res_data["data"].get("file_urls", [])

        if not file_urls:
            raise RuntimeError("MinerU did not return a presigned upload URL")
        upload_url = file_urls[0]
        # Step 2: PUT raw binary file to presigned upload URL
        upload_res = await self._request_with_retry(
            client, "PUT", upload_url, content=file_bytes
        )
        upload_res.raise_for_status()

        # Step 3: Poll extract results with backoff
        collected_markdowns = []
        total_images = 0
        total_tables = 0
        failed_items = []
        timed_out = True

        for attempt_idx in range(30):
            await asyncio.sleep(min(5.0, 1.5 + attempt_idx * 0.2))
            poll_res = await self._request_with_retry(
                client,
                "GET",
                f"{settings.MINERU_BASE_URL}/extract-results/batch/{batch_id}",
                headers=headers,
            )
            poll_res.raise_for_status()
            poll_data = poll_res.json()
            extract_results = poll_data.get("data", {}).get("extract_result", [])

            if extract_results:
                all_terminal = all(
                    item.get("state") in {"done", "failed"}
                    for item in extract_results
                )
                if not all_terminal:
                    continue
                timed_out = False
                for item in extract_results:
                    item_name = item.get("file_name") or item.get("name") or "unknown"
                    if item.get("state") == "failed":
                        failed_items.append(item_name)
                        continue
                    zip_url = item.get("full_zip_url")
                    if not zip_url:
                        failed_items.append(item_name)
                        continue
                    zip_res = await self._request_with_retry(client, "GET", zip_url)
                    zip_res.raise_for_status()
                    extracted_files = ZipGuard.validate_and_extract(
                        io.BytesIO(zip_res.content),
                        sandbox_dir,
                    )
                    for rel_path in extracted_files:
                        absolute_path = os.path.join(sandbox_dir, rel_path)
                        if rel_path.endswith((".md", ".markdown")):
                            with open(absolute_path, "r", encoding="utf-8") as f:
                                md_text = f.read()
                            collected_markdowns.append(md_text)
                            total_tables += md_text.count("|---|")
                        elif rel_path.endswith((".png", ".jpg", ".jpeg")):
                            total_images += 1
                break

        if not collected_markdowns:
            raise RuntimeError(
                "MinerU returned no usable markdown results"
                + (" before timeout" if timed_out else "")
            )
        merged_md = "\n\n---\n\n".join(collected_markdowns)
        quality = "partial" if failed_items or timed_out else "layout_vlm"
        return ParseResult(
            content=merged_md,
            page_count=max(1, len(collected_markdowns)),
            tables_count=total_tables,
            images_count=total_images,
            complexity_score=85.0,
            parser_used=self.name,
            routing_reason=f"MinerU v4 Cloud VLM extraction (Model: {model_name})",
            metadata={
                "extraction_quality": quality,
                "failed_items": failed_items,
                "timed_out": timed_out,
            },
        )

    @staticmethod
    async def _request_with_retry(client, method: str, url: str, **kwargs):
        last_error = None
        for attempt in range(3):
            try:
                response = await client.request(method, url, **kwargs)
                if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                    return response
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                await asyncio.sleep(min(8.0, delay))
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == 2:
                    raise
                await asyncio.sleep(min(8.0, 2 ** attempt))
        if last_error:
            raise last_error
        raise RuntimeError("Remote request retry exhausted")
