"""
Unified Parsers Package
Exports BaseParser, ParseResult, DocumentAnalyzer, and ParserRouter.
"""
from src.parsers.base import BaseParser, ParseResult, DocumentFeatures
from src.parsers.analyzer import DocumentAnalyzer
from src.parsers.text_parser import TextParser
from src.parsers.docx_parser import DocxParser
from src.parsers.pdf_parser import PDFParser
from src.parsers.mineru_adapter import MinerUAdapter
from src.parsers.ds_ocr_adapter import DeepSeekOCRAdapter
from src.parsers.router import ParserRouter

__all__ = [
    "BaseParser",
    "ParseResult",
    "DocumentFeatures",
    "DocumentAnalyzer",
    "TextParser",
    "DocxParser",
    "PDFParser",
    "MinerUAdapter",
    "DeepSeekOCRAdapter",
    "ParserRouter",
]
