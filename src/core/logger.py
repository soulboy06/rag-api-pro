"""
Level 0: Structured Sanitized Logging Engine
Outputs structured JSON logs, injects request/tenant context, and masks sensitive credentials.
Fixes: P2-OBS-04
"""
import os
import sys
import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from contextvars import ContextVar

# Ensure utf-8 stdout on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Context variables for distributed tracing
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
tenant_id_ctx: ContextVar[str] = ContextVar("tenant_id", default="-")
user_id_ctx: ContextVar[str] = ContextVar("user_id", default="-")
task_id_ctx: ContextVar[str] = ContextVar("task_id", default="-")


class MaskingFilter(logging.Filter):
    """
    Regex filter to mask passwords, API keys, Bearer tokens, and secrets.
    """
    SENSITIVE_PATTERNS = [
        # Bearer tokens
        (re.compile(r"(Bearer\s+)[A-Za-z0-9\-\._~+/]+=*", re.IGNORECASE), r"\1[MASKED_TOKEN]"),
        # OpenAI / BigModel / standard API Keys
        (re.compile(r"(sk-[A-Za-z0-9_\-]{8,})", re.IGNORECASE), r"sk-***[MASKED_KEY]***"),
        (re.compile(r"([0-9a-f]{32}\.[A-Za-z0-9]{16})", re.IGNORECASE), r"***[MASKED_KEY]***"),
        # JSON / Key-value password & secret patterns
        (re.compile(r'("(?:password|secret|api_key|token|access_key|secret_key)":\s*")[^"]+(")', re.IGNORECASE), r'\1[MASKED]\2'),
        (re.compile(r'((?:password|secret|api_key|token|access_key|secret_key)=)[^\s&]+', re.IGNORECASE), r'\1[MASKED]'),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.mask_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self.mask_text(str(v)) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self.mask_text(str(a)) if isinstance(a, str) else a for a in record.args)
        return True

    @classmethod
    def mask_text(cls, text: str) -> str:
        if not text:
            return text
        for pattern, replacement in cls.SENSITIVE_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


class JSONFormatter(logging.Formatter):
    """
    Formats log records into structured JSON lines.
    """
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "context": {
                "request_id": request_id_ctx.get(),
                "tenant_id": tenant_id_ctx.get(),
                "user_id": user_id_ctx.get(),
                "task_id": task_id_ctx.get(),
            },
            "location": f"{record.filename}:{record.lineno}",
        }

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # `logging(..., extra={...})` attaches fields directly to the record;
        # it does not create a record.extra dictionary.  Keep an allowlist so
        # structured context is emitted without accidentally serializing the
        # whole LogRecord (which can contain request internals).
        for key in ("endpoint", "method", "status_code", "latency_ms", "client_ip", "error"):
            if hasattr(record, key):
                value = getattr(record, key)
                log_data[key] = MaskingFilter.mask_text(str(value)) if isinstance(value, str) else value

        return json.dumps(log_data, ensure_ascii=False)


def setup_logging(log_level: str = "INFO", json_format: bool = True) -> logging.Logger:
    """Configures the root logger with MaskingFilter and JSONFormatter."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Clear existing handlers
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(MaskingFilter())

    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] [%(filename)s:%(lineno)d] %(message)s")
        )

    root_logger.addHandler(handler)
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Returns a named logger."""
    return logging.getLogger(name)
