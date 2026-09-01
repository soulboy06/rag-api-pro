"""Safe ZIP document parser for batches of supported text/PDF files."""
import io
import os
import uuid
from typing import Any, Dict, Optional

from src.core.security.sandbox import TaskSandbox
from src.core.security.zip_guard import ZipGuard
from src.parsers.base import BaseParser, ParseResult
from src.parsers.pdf_parser import PDFParser


class ArchiveParser(BaseParser):
    @property
    def name(self) -> str:
        return "archive_parser"

    async def parse(
        self,
        file_bytes: bytes,
        filename: str,
        task_id: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> ParseResult:
        supported_text = {"txt", "md", "markdown", "json", "csv"}
        parsed_parts = []
        page_details = []
        archive_id = task_id or f"archive_{uuid.uuid4().hex}"

        with TaskSandbox(archive_id) as sandbox_dir:
            extracted = ZipGuard.validate_and_extract(io.BytesIO(file_bytes), sandbox_dir)
            for relative_path in sorted(extracted):
                extension = relative_path.rsplit(".", 1)[-1].lower() if "." in relative_path else ""
                absolute_path = os.path.join(sandbox_dir, relative_path)
                if extension in supported_text:
                    with open(absolute_path, "r", encoding="utf-8", errors="replace") as handle:
                        parsed_parts.append(f"# {relative_path}\n\n{handle.read()}")
                elif extension == "pdf":
                    with open(absolute_path, "rb") as handle:
                        result = await PDFParser().parse(handle.read(), relative_path, task_id=task_id, options=options)
                    parsed_parts.append(f"# {relative_path}\n\n{result.content}")
                    page_details.extend(result.page_details)

        if not parsed_parts:
            raise ValueError("Archive contains no supported text or PDF documents")

        return ParseResult(
            content="\n\n---\n\n".join(parsed_parts),
            page_count=max(1, len(page_details)),
            parser_used=self.name,
            routing_reason="Safe ZIP extraction followed by per-file document parsing",
            page_details=page_details,
            metadata={"extraction_quality": "archive"},
        )
