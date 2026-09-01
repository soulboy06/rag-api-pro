"""
Level 3: Multi-Modal Visual OCR Adapter (Powered by BigModel GLM-4V / OpenAI Vision)
Replaces brittle traditional OCR with modern Multi-Modal Vision Language Models (VLM).
Extracts high-fidelity structured text, tables, and charts directly into clean Markdown.
"""
import io
import base64
import asyncio
from typing import Optional, Dict, Any, List
import httpx

from src.core.config import settings
from src.core.clients import InfrastructureClients
from src.core.ratelimit import global_rate_limiter
from src.parsers.base import BaseParser, ParseResult


class DeepSeekOCRAdapter(BaseParser):
    _max_concurrency = 4

    @property
    def name(self) -> str:
        return "multimodal_vlm_ocr"

    @staticmethod
    def encode_image_base64(image_bytes: bytes) -> str:
        return base64.b64encode(image_bytes).decode("utf-8")

    async def parse(
        self,
        file_bytes: bytes,
        filename: str,
        task_id: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> ParseResult:
        """
        Extracts structured Markdown from images and scanned documents using Multi-Modal VLM.
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
        opts = options or {}

        # If it's a PDF, first render pages to high-res image via PyMuPDF
        image_bytes_list = []
        if ext == "pdf":
            try:
                import fitz
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                for page in doc:
                    pix = page.get_pixmap(dpi=150)
                    image_bytes_list.append(pix.tobytes("png"))
                doc.close()
            except Exception:
                image_bytes_list = [file_bytes]
        else:
            image_bytes_list = [file_bytes]

        # Check if real BigModel VLM credentials are ready
        if not settings.USE_MOCK_MODELS and settings.OPENAI_API_KEY:
            try:
                vlm_model = "glm-4v-flash"  # High-speed multimodal visual model from Zhipu
                max_pages = opts.get("max_pages")
                pages_to_process = image_bytes_list
                if max_pages is not None:
                    pages_to_process = image_bytes_list[:max(1, int(max_pages))]

                client = await InfrastructureClients.get_http_client()
                headers = {
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                }
                concurrency = max(1, min(self._max_concurrency, int(opts.get("concurrency", self._max_concurrency))))
                semaphore = asyncio.Semaphore(concurrency)

                async def parse_page(page_idx: int, img_b: bytes):
                    await global_rate_limiter.acquire(
                        tenant_id=str(opts.get("tenant_id", "global")),
                        estimated_tokens=max(1, len(img_b) // 4096),
                        service="ocr",
                    )
                    async with semaphore:
                        return await self._parse_page_with_retry(
                            client=client,
                            headers=headers,
                            model=vlm_model,
                            base_url=settings.OPENAI_BASE_URL,
                            extension=ext,
                            page_idx=page_idx,
                            image_bytes=img_b,
                        )

                page_results = await asyncio.gather(
                    *(parse_page(page_idx, img_b) for page_idx, img_b in enumerate(pages_to_process)),
                    return_exceptions=True,
                )
                successful_pages = []
                failed_pages = []
                page_details: List[Dict[str, Any]] = []
                for page_idx, page_result in enumerate(page_results):
                    page_number = page_idx + 1
                    if isinstance(page_result, Exception):
                        failed_pages.append({"page_number": page_number, "error": str(page_result)[:500]})
                        page_details.append({
                            "page_number": page_number,
                            "status": "failed",
                            "error": str(page_result)[:500],
                        })
                        continue
                    content = page_result
                    successful_pages.append(f"<!-- Page {page_number} -->\n{content}")
                    page_details.append({
                        "page_number": page_number,
                        "character_count": len(content),
                        "status": "success",
                    })

                if not successful_pages:
                    raise RuntimeError("Visual OCR did not successfully process any page")

                full_md = "\n\n".join(successful_pages)
                truncated = len(pages_to_process) < len(image_bytes_list)
                quality = "visual" if not failed_pages and not truncated else "partial"
                return ParseResult(
                    content=full_md,
                    page_count=len(image_bytes_list),
                    tables_count=full_md.count("|---|"),
                    images_count=len(image_bytes_list),
                    complexity_score=60.0,
                    parser_used=self.name,
                    routing_reason=f"Zhipu Multi-Modal VLM ({vlm_model}) page-bounded visual recognition",
                    page_details=page_details,
                    metadata={
                        "extraction_quality": quality,
                        "truncated": truncated,
                        "failed_pages": failed_pages,
                        "successful_pages": len(successful_pages),
                        "total_pages": len(image_bytes_list),
                    },
                )
            except Exception:
                # Log VLM error and fall back to local extraction
                pass

        # Never claim success with a fabricated placeholder.  The router or
        # worker can now expose a retryable, actionable configuration error.
        raise RuntimeError(
            "Visual OCR is not configured or did not successfully process every page"
        )

    @staticmethod
    async def _parse_page_with_retry(
        *,
        client,
        headers: Dict[str, str],
        model: str,
        base_url: str,
        extension: str,
        page_idx: int,
        image_bytes: bytes,
    ) -> str:
        """Calls the VLM for one page with bounded retries outside locks."""
        b64_img = DeepSeekOCRAdapter.encode_image_base64(image_bytes)
        mime_type = "image/png" if extension == "pdf" else f"image/{extension}"
        prompt = (
            "你是一个高精度的文档多模态识别助手。请完整识别这幅图像中的所有文字内容。"
            "如果有表格，请严格转换为标准 Markdown 表格；保留原本的标题层级（#、##）和段落结构。"
            "只输出识别后的 Markdown 文本内容，不要输出任何多余的寒暄或解释。"
        )
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_img}"}},
                ],
            }],
            "temperature": 0.1,
        }
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60.0,
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    await asyncio.sleep(min(8.0, 2 ** attempt))
                    continue
                if response.status_code != 200:
                    raise RuntimeError(
                        f"visual model returned HTTP {response.status_code} for page {page_idx + 1}"
                    )
                data = response.json()
                choices = data.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise RuntimeError(f"visual model response has no choices for page {page_idx + 1}")
                message = choices[0].get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError(f"visual model response has no content for page {page_idx + 1}")
                return content
            except (httpx.RequestError, RuntimeError, ValueError, KeyError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                await asyncio.sleep(min(8.0, 2 ** attempt))
        raise RuntimeError(f"visual OCR page {page_idx + 1} failed: {last_error}")
