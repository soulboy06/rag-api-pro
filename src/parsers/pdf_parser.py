"""
Level 3: Multi-Page PDF Parser with Page-Order Preservation and Loss Prevention
Extracts text from all pages sequentially and injects structured page anchors.
Fixes: P0-CORE-02 (Prevents discarding multi-page content), P1-REMOTE-06
"""
import io
from typing import Optional, Dict, Any, List
from src.parsers.base import BaseParser, ParseResult


class PDFParser(BaseParser):
    @property
    def name(self) -> str:
        return "pdf_parser"

    async def parse(
        self,
        file_bytes: bytes,
        filename: str,
        task_id: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> ParseResult:
        """
        Parses all pages in a PDF document sequentially using PyMuPDF (fitz) or pypdf fallback.
        Preserves original page ordering and formats structured page anchors.
        """
        page_texts = []
        page_details = []
        total_images = 0
        total_tables = 0
        extraction_backend = "pymupdf"

        # Try PyMuPDF first for high quality extraction and structured table recovery
        try:
            import pymupdf
            doc = pymupdf.open(stream=file_bytes, filetype="pdf")
            total_pages = len(doc)

            for page_idx in range(total_pages):
                page = doc[page_idx]
                page_num = page_idx + 1
                try:
                    text = page.get_text("text").strip()
                    images = page.get_images()
                    total_images += len(images)

                    # Structured vector table extraction with quality gate
                    page_tables_md: List[str] = []
                    try:
                        finder = page.find_tables()
                        for table in finder.tables:
                            rows = table.extract()
                            if rows and len(rows) >= 2:
                                total_cells = sum(len(r) for r in rows)
                                non_empty = sum(1 for r in rows for c in r if c and str(c).strip())
                                # Quality gate: require at least 30% non-empty cells
                                if total_cells > 0 and (non_empty / total_cells) >= 0.3:
                                    md_tbl = table.to_markdown().strip()
                                    if md_tbl:
                                        page_tables_md.append(md_tbl)
                    except Exception:
                        pass

                    total_tables += len(page_tables_md)

                    # Combine narrative text and structured tables
                    page_content_parts = []
                    if text:
                        page_content_parts.append(text)
                    if page_tables_md:
                        page_content_parts.append("\n\n".join(page_tables_md))

                    combined_text = "\n\n".join(page_content_parts).strip()
                    if combined_text:
                        page_texts.append(f"<!-- Page {page_num} -->\n{combined_text}")
                    else:
                        page_texts.append(f"<!-- Page {page_num} -->\n[Scanned / Image Page]")

                    page_details.append({
                        "page_number": page_num,
                        "character_count": len(combined_text),
                        "image_count": len(images),
                        "table_count": len(page_tables_md),
                        "status": "success" if combined_text else "image_only"
                    })
                except Exception as page_err:
                    # Single page failure does NOT terminate whole document
                    page_texts.append(f"<!-- Page {page_num} -->\n[Page extraction warning: {str(page_err)}]")
                    page_details.append({
                        "page_number": page_num,
                        "status": "warning",
                        "error": str(page_err)
                    })

            doc.close()

        except Exception:
            # Fallback to pypdf
            try:
                import pypdf
                extraction_backend = "pypdf"
                reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                total_pages = len(reader.pages)
                for page_idx in range(total_pages):
                    page_num = page_idx + 1
                    page = reader.pages[page_idx]
                    text = (page.extract_text() or "").strip()
                    page_texts.append(f"<!-- Page {page_num} -->\n{text}")
                    page_details.append({
                        "page_number": page_num,
                        "character_count": len(text),
                        "image_count": 0,
                        "status": "success"
                    })
            except Exception as e:
                raise ValueError(f"Unable to parse PDF '{filename}': {e}") from e

        full_content = "\n\n".join(page_texts).strip()
        has_text = any(detail.get("character_count", 0) > 0 for detail in page_details)

        return ParseResult(
            content=full_content,
            page_count=total_pages,
            tables_count=total_tables,
            images_count=total_images,
            complexity_score=10.0 + min(30.0, total_images * 5.0) + min(40.0, total_tables * 5.0),
            parser_used=self.name,
            routing_reason=f"Sequential multi-page PDF extraction with structured table recovery ({total_pages} pages, {total_tables} tables)",
            page_details=page_details,
            metadata={
                "extraction_quality": "text" if has_text else "image_only",
                "extraction_backend": extraction_backend,
                "page_details": page_details,
            },
        )
