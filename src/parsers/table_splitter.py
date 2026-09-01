"""
Table-Aware Document Splitter
Preserves table boundaries (HTML <table> and Markdown tables), maintains section header context,
and avoids blind character-slicing through data rows.
"""
import re
from typing import List, Dict, Any, Tuple, Optional


class ProcessedChunk:
    def __init__(
        self,
        content: str,
        is_table: bool = False,
        table_title: str = "",
        search_index_text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        self.content = content.strip()
        self.is_table = is_table
        self.table_title = table_title
        self.search_index_text = (search_index_text or content).strip()
        self.metadata = metadata or {}


class TableAwareSplitter:
    """
    Intelligent document splitter that treats tables as first-class citizens.
    - Preserves HTML tables (<table>...</table>) and Markdown tables (| ... |) as atomic chunks.
    - Captures immediate preceding heading (e.g. ## 产业视角) as table context.
    - Slices normal text by paragraph/sentence boundaries.
    """
    
    HTML_TABLE_PATTERN = re.compile(r"(<table[\s\S]*?</table>)", re.IGNORECASE)
    HEADING_PATTERN = re.compile(r"^(#{1,6}\s+.+)$", re.MULTILINE)

    @classmethod
    def split_document(
        cls,
        text: str,
        target_chunk_size: int = 600,
        chunk_overlap: int = 80
    ) -> List[ProcessedChunk]:
        if not text or not text.strip():
            return []

        text = text.strip()
        chunks: List[ProcessedChunk] = []
        
        # 1. Split text into segments: tables and non-table text blocks
        parts = cls.HTML_TABLE_PATTERN.split(text)
        current_heading = "文档正文"

        for part in parts:
            if not part.strip():
                continue
            
            # Check if this part is an HTML table
            if part.strip().lower().startswith("<table") and part.strip().lower().endswith("</table>"):
                table_chunk = cls._process_table_chunk(part.strip(), current_heading)
                chunks.append(table_chunk)
            else:
                # Track latest section heading in non-table text
                headings = cls.HEADING_PATTERN.findall(part)
                if headings:
                    current_heading = headings[-1].strip()
                
                # Split regular text paragraphs
                text_chunks = cls._split_text_block(part.strip(), current_heading, target_chunk_size, chunk_overlap)
                chunks.extend(text_chunks)

        return chunks

    @classmethod
    def _process_table_chunk(cls, table_html: str, current_heading: str) -> ProcessedChunk:
        """Processes an HTML table, extracts schema hints, and builds search index text."""
        from src.parsers.table_profiler import TableProfiler
        return TableProfiler.profile_html_table(table_html, current_heading)

    @classmethod
    def _split_text_block(
        cls,
        text: str,
        current_heading: str,
        chunk_size: int,
        overlap: int
    ) -> List[ProcessedChunk]:
        """Splits narrative text by paragraphs or sentence boundaries."""
        if not text:
            return []

        paragraphs = text.split("\n\n")
        chunks: List[ProcessedChunk] = []
        current_buf: List[str] = []
        current_len = 0

        for p in paragraphs:
            p = p.strip()
            if not p:
                continue

            if current_len + len(p) > chunk_size and current_buf:
                chunk_str = "\n\n".join(current_buf).strip()
                if chunk_str:
                    chunks.append(ProcessedChunk(
                        content=chunk_str,
                        is_table=False,
                        table_title=current_heading,
                        metadata={"heading": current_heading}
                    ))
                
                # Retain overlap from the last paragraph if possible
                last_p = current_buf[-1] if current_buf else ""
                if len(last_p) < overlap:
                    current_buf = [last_p, p]
                    current_len = len(last_p) + len(p)
                else:
                    current_buf = [p]
                    current_len = len(p)
            else:
                current_buf.append(p)
                current_len += len(p)

        if current_buf:
            chunk_str = "\n\n".join(current_buf).strip()
            if chunk_str:
                chunks.append(ProcessedChunk(
                    content=chunk_str,
                    is_table=False,
                    table_title=current_heading,
                    metadata={"heading": current_heading}
                ))

        return chunks
