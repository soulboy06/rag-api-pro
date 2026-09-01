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
        extraction_backend = "pymupdf"

        # Try PyMuPDF (fitz) first for high quality extraction
        try:
            import fitz
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            total_pages = len(doc)

            for page_idx in range(total_pages):
                page = doc[page_idx]
                page_num = page_idx + 1
                try:
                    text = page.get_text("text").strip()
                    images = page.get_images()
                    total_images += len(images)

                    if text:
                        page_texts.append(f"<!-- Page {page_num} -->\n{text}")
                    else:
                        page_texts.append(f"<!-- Page {page_num} -->\n[Scanned / Image Page]")

                    page_details.append({
                        "page_number": page_num,
                        "character_count": len(text),
                        "image_count": len(images),
                        "status": "success" if text else "image_only"
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
            tables_count=0,
            images_count=total_images,
            complexity_score=10.0 + min(30.0, total_images * 5.0),
            parser_used=self.name,
            routing_reason=f"Sequential multi-page PDF extraction ({total_pages} pages)",
            page_details=page_details,
            metadata={
                "extraction_quality": "text" if has_text else "image_only",
                "extraction_backend": extraction_backend,
                "page_details": page_details,
            },
        )
