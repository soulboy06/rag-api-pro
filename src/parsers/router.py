"""
Level 3: Intelligent Parser Router and Fallback Chain
Routes documents to optimal parsers based on 0-100 complexity scores with multi-tiered fallback.
Fixes: P0-CORE-05, P1-PARSER-01, P1-PARSER-02
"""
from typing import Optional, Dict, Any
from src.parsers.base import BaseParser, ParseResult
from src.parsers.analyzer import DocumentAnalyzer
from src.parsers.text_parser import TextParser
from src.parsers.docx_parser import DocxParser
from src.parsers.pdf_parser import PDFParser
from src.parsers.mineru_adapter import MinerUAdapter
from src.parsers.ds_ocr_adapter import DeepSeekOCRAdapter
from src.parsers.archive_parser import ArchiveParser


class ParserRouter:
    _parsers: Dict[str, BaseParser] = {
        "text_parser": TextParser(),
        "docx_parser": DocxParser(),
        "pdf_parser": PDFParser(),
        "mineru": MinerUAdapter(),
        "deepseek_ocr": DeepSeekOCRAdapter(),
        "archive_parser": ArchiveParser(),
    }

    @classmethod
    async def route_and_parse(
        cls,
        file_bytes: bytes,
        filename: str,
        task_id: Optional[str] = None,
        force_parser: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> ParseResult:
        """
        Analyzes document complexity, chooses the best parser according to scores,
        and manages multi-tier fallback if higher-tier parsers fail.
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
        features, score, analysis_reason = DocumentAnalyzer.analyze_bytes(file_bytes, filename)

        # 1. Check for manual parser override
        if force_parser:
            if force_parser not in cls._parsers:
                raise ValueError(f"Unsupported parser override: '{force_parser}'")
            selected_parser = cls._parsers[force_parser]
            res = await selected_parser.parse(file_bytes, filename, task_id=task_id, options=options)
            res.complexity_score = score
            res.routing_reason = f"Manual override to '{force_parser}'"
            return res

        # 2. Plain Text / Markdown formats
        if ext in {"txt", "md", "markdown", "json", "csv"}:
            res = await cls._parsers["text_parser"].parse(file_bytes, filename, task_id=task_id, options=options)
            res.complexity_score = score
            res.routing_reason = f"Direct text parser: {analysis_reason}"
            return res

        # ZIP batches are validated and extracted in an isolated sandbox.
        if ext == "zip":
            res = await cls._parsers["archive_parser"].parse(
                file_bytes,
                filename,
                task_id=task_id,
                options=options,
            )
            res.complexity_score = score
            res.routing_reason = f"Safe archive parser: {analysis_reason}"
            return res

        # 3. DOCX documents
        if ext == "docx":
            res = await cls._parsers["docx_parser"].parse(file_bytes, filename, task_id=task_id, options=options)
            res.complexity_score = score
            res.routing_reason = f"DOCX structured parser: {analysis_reason}"
            return res

        # 4. Images
        if ext in {"png", "jpg", "jpeg"}:
            res = await cls._parsers["deepseek_ocr"].parse(
                file_bytes,
                filename,
                task_id=task_id,
                options=options,
            )
            res.complexity_score = score
            res.routing_reason = f"Visual OCR parser: {analysis_reason}"
            return res

        # 5. PDF Documents (Score-Driven Intelligent Routing)
        # Tier 1: Score >= 60 -> MinerU VLM
        # Tier 2: 20 <= Score < 60 -> DeepSeek-OCR Table Grounding
        # Tier 3: Score < 20 -> Fast Sequential PDFParser
        parser_sequence = []
        if score >= 60.0:
            parser_sequence = ["mineru", "deepseek_ocr", "pdf_parser"]
            chosen_tier = "High Complexity (Score >= 60) -> MinerU VLM"
        elif score >= 20.0:
            parser_sequence = ["deepseek_ocr", "pdf_parser"]
            chosen_tier = "Medium Complexity (20 <= Score < 60) -> DeepSeek-OCR"
        else:
            parser_sequence = ["pdf_parser"]
            chosen_tier = "Low Complexity (Score < 20) -> Fast PDFParser"

        # Execute fallback chain
        last_error = None
        for parser_name in parser_sequence:
            parser = cls._parsers[parser_name]
            try:
                result = await parser.parse(file_bytes, filename, task_id=task_id, options=options)
                result.complexity_score = score
                result.routing_reason = f"{chosen_tier} (Selected '{parser_name}'): {analysis_reason}"
                return result
            except Exception as e:
                last_error = e
                # Fallback to next parser in sequence
                continue

        # A recognized document type must never be reported as successfully
        # parsed when every real parser failed. Returning arbitrary UTF-8
        # bytes here used to turn corrupt PDFs into "successful" ingestions,
        # which made downstream retrieval silently incomplete. Keep the
        # emergency fallback only for unknown extensions, where best-effort
        # text extraction is an explicit compatibility behavior.
        if last_error is not None and ext in {"pdf", "docx", "png", "jpg", "jpeg", "zip"}:
            raise last_error

        # Ultimate fallback to raw text extraction for unknown extensions.
        raw_text = file_bytes.decode("utf-8", errors="ignore")
        return ParseResult(
            content=raw_text,
            page_count=features.page_count,
            tables_count=features.table_count,
            images_count=features.image_count,
            complexity_score=score,
            parser_used="emergency_fallback",
            routing_reason=f"Emergency fallback due to: {str(last_error)}"
        )
