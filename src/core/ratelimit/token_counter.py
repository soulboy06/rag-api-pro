"""
Level 2: Token Counter and Usage Metering Adapter
Provides conservative token pre-estimation and actual API token reconciliation.
Fixes: P1-REMOTE-05
"""
import re
from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    is_estimated: bool = False
    estimation_method: str = "actual"


class TokenCounter:
    # Regex to identify CJK (Chinese, Japanese, Korean) characters
    CJK_REGEX = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")

    @classmethod
    def estimate_text_tokens(cls, text: str) -> int:
        """
        Conservative pre-estimation of tokens for mixed Chinese and English text.
        - CJK characters: 1 character ~ 1.0 token (conservative)
        - Non-CJK words/characters: 4 characters ~ 1 token (or 1 word ~ 1.3 tokens)
        """
        if not text:
            return 0

        cjk_chars = len(cls.CJK_REGEX.findall(text))
        non_cjk_text = cls.CJK_REGEX.sub("", text)
        non_cjk_words = len(non_cjk_text.split())
        non_cjk_tokens = max(int(len(non_cjk_text) / 3.5), int(non_cjk_words * 1.3))

        # Base prompt overhead
        return max(1, cjk_chars + non_cjk_tokens + 4)

    @classmethod
    def parse_llm_response_usage(
        cls,
        response_data: Any,
        fallback_prompt_text: Optional[str] = None,
        fallback_completion_text: Optional[str] = None
    ) -> TokenUsage:
        """
        Extracts actual token counts from provider response (OpenAI/Zhipu BigModel/DeepSeek).
        Falls back to conservative estimation if provider does not return usage.
        """
        # 1. Check if response object has usage attribute
        usage_obj = getattr(response_data, "usage", None)
        if isinstance(response_data, dict):
            usage_obj = response_data.get("usage")

        if usage_obj:
            prompt_tokens = getattr(usage_obj, "prompt_tokens", None) or (
                usage_obj.get("prompt_tokens") if isinstance(usage_obj, dict) else 0
            ) or 0
            completion_tokens = getattr(usage_obj, "completion_tokens", None) or (
                usage_obj.get("completion_tokens") if isinstance(usage_obj, dict) else 0
            ) or 0
            total_tokens = getattr(usage_obj, "total_tokens", None) or (
                usage_obj.get("total_tokens") if isinstance(usage_obj, dict) else 0
            ) or (prompt_tokens + completion_tokens)

            return TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                is_estimated=False,
                estimation_method="provider_usage_header"
            )

        # 2. Conservative fallback estimation
        p_est = cls.estimate_text_tokens(fallback_prompt_text or "")
        c_est = cls.estimate_text_tokens(fallback_completion_text or "")
        return TokenUsage(
            prompt_tokens=p_est,
            completion_tokens=c_est,
            total_tokens=p_est + c_est,
            is_estimated=True,
            estimation_method="heuristic_cjk_char_count"
        )
