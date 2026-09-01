"""
Level 3: Text and Markdown Parser
High-speed cleaner and normalizer for plain text and markdown documents.
Fixes: P1-PARSER-07, P1-PARSER-08
"""
import re
from typing import Optional, Dict, Any
from src.parsers.base import BaseParser, ParseResult


class TextParser(BaseParser):
    @property
    def name(self) -> str:
        return "text_parser"

    async def parse(
        self,
        file_bytes: bytes,
        filename: str,
        task_id: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> ParseResult:
        # 1. Decode with robust fallback
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = file_bytes.decode("gbk")
            except Exception:
                text = file_bytes.decode("utf-8", errors="ignore")

        # 2. Normalize whitespace and newlines
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Remove consecutive blank lines (> 3)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        # 3. Detect tables and headings
        table_matches = re.findall(r"\n\|[-: |]+\|\n", text)
        tables_count = len(table_matches)

        return ParseResult(
            content=text,
            page_count=max(1, len(text) // 3000),
            tables_count=tables_count,
            images_count=0,
            complexity_score=0.0,
            parser_used=self.name,
            routing_reason="Direct text sanitization",
            metadata={"character_count": len(text)}
        )
