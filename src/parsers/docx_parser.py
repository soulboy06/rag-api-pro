"""
Level 3: DOCX Document Parser
Extracts paragraphs, headings, and tables from DOCX files and formats them into Markdown.
"""
import io
import re
import zipfile
from typing import Optional, Dict, Any, List
import xml.etree.ElementTree as ET
from src.parsers.base import BaseParser, ParseResult


class DocxParser(BaseParser):
    @property
    def name(self) -> str:
        return "docx_parser"

    async def parse(
        self,
        file_bytes: bytes,
        filename: str,
        task_id: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> ParseResult:
        """Parses DOCX XML structure into clean Markdown."""
        text_blocks = []
        table_count = 0
        image_count = 0
        has_document_xml = False

        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
                # Count media images
                image_count = sum(1 for name in zf.namelist() if name.startswith("word/media/"))

                # Read word/document.xml
                if "word/document.xml" in zf.namelist():
                    has_document_xml = True
                    xml_data = zf.read("word/document.xml")
                    root = ET.fromstring(xml_data)

                    # XML namespace map
                    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

                    # Iterate over body elements (paragraphs and tables)
                    body = root.find("w:body", ns)
                    if body is not None:
                        for elem in body:
                            # 1. Paragraph
                            if elem.tag.endswith("p"):
                                p_texts = []
                                for t in elem.findall(".//w:t", ns):
                                    if t.text:
                                        p_texts.append(t.text)
                                p_full = "".join(p_texts).strip()
                                if p_full:
                                    text_blocks.append(p_full)

                            # 2. Table
                            elif elem.tag.endswith("tbl"):
                                table_count += 1
                                rows = []
                                for tr in elem.findall(".//w:tr", ns):
                                    row_cells = []
                                    for tc in tr.findall(".//w:tc", ns):
                                        cell_texts = [t.text for t in tc.findall(".//w:t", ns) if t.text]
                                        row_cells.append(" ".join(cell_texts).strip())
                                    if row_cells:
                                        rows.append(row_cells)

                                # Format into Markdown table
                                if rows:
                                    max_cols = max(len(r) for r in rows)
                                    # Normalize column lengths
                                    norm_rows = [r + [""] * (max_cols - len(r)) for r in rows]
                                    header_str = "| " + " | ".join(norm_rows[0]) + " |"
                                    separator_str = "| " + " | ".join(["---"] * max_cols) + " |"
                                    table_md = [header_str, separator_str]
                                    for r in norm_rows[1:]:
                                        table_md.append("| " + " | ".join(r) + " |")
                                    text_blocks.append("\n".join(table_md))
        except (zipfile.BadZipFile, ET.ParseError, OSError, ValueError) as e:
            # A malformed DOCX must fail the parser contract. Returning the
            # exception text as document content creates a false successful
            # ingestion and pollutes retrieval with implementation details.
            raise ValueError(f"Unable to parse DOCX '{filename}': {e}") from e

        if not has_document_xml:
            raise ValueError(f"DOCX '{filename}' does not contain word/document.xml")

        content = "\n\n".join(text_blocks).strip()
        extraction_quality = "text" if content else ("image_only" if image_count else "empty")
        return ParseResult(
            content=content,
            page_count=max(1, len(content) // 3000),
            tables_count=table_count,
            images_count=image_count,
            complexity_score=15.0 + table_count * 10,
            parser_used=self.name,
            routing_reason="Structured DOCX paragraph & table extraction",
            metadata={"extraction_quality": extraction_quality},
        )
