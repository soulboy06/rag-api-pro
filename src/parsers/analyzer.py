"""
Level 3: Document Feature Analyzer & Complexity Scoring Engine
Extracts layout features (pages, tables, images, formulas) and calculates 0-100 complexity scores.
Fixes: P1-PARSER-03, P1-PARSER-04, P1-PARSER-05, P1-PARSER-06
"""
import io
import re
from typing import Tuple, Dict, Any
from src.parsers.base import DocumentFeatures


class DocumentAnalyzer:
    FORMULA_REGEX = re.compile(r"(\$\$.*?\$\$|\$.*?\$|\\\[.*?\\\]|\\\(.*?\\\))", re.DOTALL)
    TABLE_TAG_REGEX = re.compile(r"(\|.*?\||<table.*?>)", re.IGNORECASE)

    @classmethod
    def analyze_bytes(cls, file_bytes: bytes, filename: str) -> Tuple[DocumentFeatures, float, str]:
        """
        Analyzes binary file features and calculates a normalized 0-100 complexity score.
        Returns: (DocumentFeatures, complexity_score, explanation_reason)
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
        features = DocumentFeatures(file_type=ext)

        # 1. Plain text / Markdown
        if ext in {"txt", "md", "markdown", "json", "csv"}:
            try:
                text = file_bytes.decode("utf-8", errors="ignore")
            except Exception:
                text = str(file_bytes)
            
            features.total_characters = len(text)
            features.page_count = max(1, len(text) // 3000)
            features.formula_count = len(cls.FORMULA_REGEX.findall(text))
            features.table_count = len(re.findall(r"\n\|[-: |]+\|\n", text))

            # Plain text is inherently low complexity (< 10) unless packed with complex tables/formulas
            base_score = 0.0
            score = min(100.0, base_score + features.table_count * 10 + features.formula_count * 15)
            reason = f"Plain text document with {len(text)} chars, {features.table_count} tables, {features.formula_count} formulas"
            return features, round(score, 2), reason

        # 2. Images (PNG / JPG / JPEG)
        elif ext in {"png", "jpg", "jpeg"}:
            features.image_count = 1
            features.page_count = 1
            features.has_scanned_pages = True
            # Images require OCR/VLM
            score = 50.0
            reason = "Raster image requiring visual OCR or Multimodal VLM processing"
            return features, score, reason

        # 3. PDF Documents
        elif ext == "pdf":
            page_count = 1
            image_count = 0
            table_count = 0
            formula_count = 0

            # Prefer the actual page tree and extracted text when PyMuPDF is
            # available. Raw byte markers are only a conservative fallback;
            # they can overcount shared PDF resources and cannot distinguish a
            # page with a text layer from an image-only page.
            pdf_doc = None
            total_tables = 0
            pages_with_tables = 0
            max_table_area_ratio = 0.0
            try:
                import pymupdf

                pdf_doc = pymupdf.open(stream=file_bytes, filetype="pdf")
                page_count = max(1, len(pdf_doc))
                pages_with_text = 0
                for pdf_page in pdf_doc:
                    page_text = pdf_page.get_text("text") or ""
                    if page_text.strip():
                        pages_with_text += 1
                    image_count += len(pdf_page.get_images(full=True))
                    formula_count += len(cls.FORMULA_REGEX.findall(page_text))
                    
                    # Lightweight vector table detection via PyMuPDF TableFinder
                    try:
                        page_area = float(pdf_page.rect.width * pdf_page.rect.height)
                        finder = pdf_page.find_tables()
                        if finder.tables:
                            page_table_count = len(finder.tables)
                            total_tables += page_table_count
                            pages_with_tables += 1
                            page_table_area = 0.0
                            for table in finder.tables:
                                x0, y0, x1, y1 = table.bbox
                                page_table_area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
                            ratio = min(1.0, page_table_area / page_area) if page_area > 0 else 0.0
                            if ratio > max_table_area_ratio:
                                max_table_area_ratio = ratio
                    except Exception:
                        pass
                has_text_layer = pages_with_text > 0
            except Exception:
                # Optional inspection dependency failed; retain byte-level
                # heuristics below so routing still has a deterministic result.
                page_matches = re.findall(rb"/Type\s*/Page\b", file_bytes)
                if page_matches:
                    page_count = max(1, len(page_matches))
                image_matches = re.findall(rb"/Subtype\s*/Image\b", file_bytes)
                image_count = len(image_matches)
                font_matches = re.findall(rb"/Type\s*/Font\b", file_bytes)
                has_text_layer = len(font_matches) > 0
            finally:
                if pdf_doc is not None:
                    pdf_doc.close()

            # Inspect PDF structure markers in raw bytes
            # Look for /Type /Page
            # These byte markers are intentionally used only when the
            # PyMuPDF inspection above was unavailable.

            features.page_count = page_count
            features.image_count = image_count
            features.table_count = total_tables
            features.has_scanned_pages = not has_text_layer

            # Calculate complexity score with table awareness
            table_page_ratio = (pages_with_tables / page_count) if page_count > 0 else 0.0
            if not has_text_layer:
                score = 70.0  # Scanned PDF requires OCR/VLM
            elif max_table_area_ratio >= 0.4 or table_page_ratio >= 0.5:
                # Dense table layout: substantial tables across the document
                score = 60.0
            elif max_table_area_ratio >= 0.1 or pages_with_tables > 0:
                # Moderate table presence
                score = 35.0
            elif image_count > 0:
                score = 20.0 + min(40.0, image_count * 5.0)  # Visual/Hybrid PDF
            else:
                score = 5.0  # Pure native text PDF -> Fast PyMuPDF extraction

            reason = (
                f"PDF with {page_count} pages, {total_tables} tables (max area {max_table_area_ratio*100:.1f}%, "
                f"{pages_with_tables}/{page_count} pages), {image_count} images, "
                f"text_layer={'present' if has_text_layer else 'scanned'}"
            )
            return features, min(100.0, round(score, 2)), reason

        # 4. DOCX Documents
        elif ext == "docx":
            # DOCX zip contains word/document.xml
            import zipfile
            table_count = 0
            image_count = 0
            try:
                with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
                    for name in zf.namelist():
                        if name.startswith("word/media/"):
                            image_count += 1
                        elif name == "word/document.xml":
                            xml_content = zf.read(name).decode("utf-8", errors="ignore")
                            table_count += xml_content.count("<w:tbl>")
            except Exception:
                pass

            features.table_count = table_count
            features.image_count = image_count
            score = 15.0 + table_count * 10.0 + min(30.0, image_count * 5.0)
            reason = f"DOCX document with {table_count} tables and {image_count} media images"
            return features, min(100.0, round(score, 2)), reason

        # Default fallback
        return features, 5.0, "Standard document format"
