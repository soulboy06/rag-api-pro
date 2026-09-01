"""
Level 3: Multi-Modal Parser Base Architecture & Contracts
Defines standard contracts for all parsers, feature analyzers, and router decisions.
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field


@dataclass
class DocumentFeatures:
    file_type: str = "text"
    page_count: int = 1
    table_count: int = 0
    image_count: int = 0
    formula_count: int = 0
    total_characters: int = 0
    non_text_ratio: float = 0.0
    has_scanned_pages: bool = False
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    content: str
    page_count: int = 1
    tables_count: int = 0
    images_count: int = 0
    complexity_score: float = 0.0
    parser_used: str = "direct"
    routing_reason: str = ""
    page_details: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseParser(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Name identifier of the parser."""
        pass

    @abstractmethod
    async def parse(
        self,
        file_bytes: bytes,
        filename: str,
        task_id: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> ParseResult:
        """Parses binary file bytes into structured Markdown ParseResult."""
        pass
