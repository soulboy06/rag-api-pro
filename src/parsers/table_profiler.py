"""
Table Profiler & Semantic Schema Extractor
Extracts headers, row entities, units, and builds rich search index text for dual-representation indexing.
"""
import re
from typing import List, Dict, Any, Tuple, Optional
from src.parsers.table_splitter import ProcessedChunk


class TableProfiler:
    """
    Analyzes HTML table structures and generates rich semantic profiles for RAG retrieval.
    """
    ROW_PATTERN = re.compile(r"<tr[\s\S]*?</tr>", re.IGNORECASE)
    CELL_PATTERN = re.compile(r"<(?:td|th)[\s\S]*?>([\s\S]*?)</(?:td|th)>", re.IGNORECASE)
    TAG_CLEANER = re.compile(r"<[^>]+>")

    @classmethod
    def profile_html_table(cls, table_html: str, heading: str = "") -> ProcessedChunk:
        """Extracts schema, entities, unit, and builds dual-representation ProcessedChunk."""
        heading_clean = heading.replace("#", "").strip() if heading else "数据统计表"
        
        # 1. Parse rows and cells
        rows = cls.ROW_PATTERN.findall(table_html)
        parsed_rows: List[List[str]] = []
        for r in rows:
            cells = cls.CELL_PATTERN.findall(r)
            clean_cells = [cls.TAG_CLEANER.sub("", c).strip() for c in cells]
            if any(clean_cells):
                parsed_rows.append(clean_cells)

        if not parsed_rows:
            return ProcessedChunk(
                content=f"## {heading_clean}\n\n{table_html}",
                is_table=True,
                table_title=heading_clean,
                search_index_text=f"{heading_clean}\n{table_html[:300]}"
            )

        # 2. Extract column headers (usually row 0)
        headers = parsed_rows[0] if parsed_rows else []
        col_names = [h for h in headers if h and len(h) < 30]
        
        # 3. Detect measurement units
        unit = "未明确"
        unit_match = re.search(r"(\((?:人民币|美元|港元|元|百万元|亿元|万元|万件|%)\)|人民币[百亿万]*元|亿元|百万元|万元|万件)", table_html)
        if unit_match:
            unit = unit_match.group(1).replace("(", "").replace(")", "").strip()

        # 4. Extract first-column entities (row labels)
        entities = []
        for r in parsed_rows[1:]:
            if r and len(r) > 0:
                first_col = r[0].strip()
                # Filter out pure numbers or empty cells
                if first_col and not re.match(r"^[\d\.,\s%]+$", first_col) and len(first_col) < 40:
                    entities.append(first_col)

        # De-duplicate while preserving order
        unique_entities = list(dict.fromkeys(entities))
        entities_str = ", ".join(unique_entities[:15]) if unique_entities else "无命名行实体"
        columns_str = ", ".join(col_names[:10]) if col_names else "通用指标"

        # 5. Build rich Search Index Text (Embedding & Keyword Retrieval input)
        search_index_text = (
            f"【章节与表名】: {heading_clean} 财务/统计数据汇总对比表\n"
            f"【计量单位】: {unit}\n"
            f"【指标列名】: {columns_str}\n"
            f"【包含业务实体与各行项目】: {entities_str}\n"
            f"【表格概要与对比】: 本表汇总记录了关于 {heading_clean} 下各业务板块（包括 {entities_str}）的 {columns_str} 等指标与同比增速横向对比数据。"
        )

        # 6. Build full formatted content for LLM grounding
        formatted_content = f"## {heading_clean}\n[数据统计汇总表 | 计量单位: {unit}]\n\n{table_html}"

        return ProcessedChunk(
            content=formatted_content,
            is_table=True,
            table_title=heading_clean,
            search_index_text=search_index_text,
            metadata={
                "is_table": True,
                "table_title": heading_clean,
                "unit": unit,
                "entities": unique_entities,
                "columns": col_names,
                "row_count": len(parsed_rows)
            }
        )
