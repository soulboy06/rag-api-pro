"""
Level 1: File Validator with Magic Number Inspection and Streaming Quota Limiter
Validates file extensions, content types, and binary magic bytes.
Applies streaming size truncation to prevent OOM and payload spoofing.
Fixes: P1-API-07, P1-API-08
"""
import io
import hashlib
from typing import Tuple, Set, Dict, Optional
from fastapi import UploadFile
from src.core.exceptions import SecurityViolationError, ValidationError

# Default maximum file size: 50MB
DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024
STREAM_CHUNK_SIZE = 64 * 1024  # 64KB chunks


class FileValidator:
    # Magic bytes signatures
    MAGIC_SIGNATURES: Dict[str, bytes] = {
        "pdf": b"%PDF-",
        "png": b"\x89PNG\r\n\x1a\n",
        "jpeg": b"\xff\xd8\xff",
        "jpg": b"\xff\xd8\xff",
        "docx": b"PK\x03\x04",
        "zip": b"PK\x03\x04",
    }

    ALLOWED_EXTENSIONS: Set[str] = {
        "pdf", "docx", "txt", "md", "markdown", "json", "csv", "png", "jpg", "jpeg", "zip"
    }

    @classmethod
    def get_extension(cls, filename: str) -> str:
        if not filename or "." not in filename:
            return ""
        return filename.rsplit(".", 1)[1].lower()

    @classmethod
    def validate_magic_number(cls, extension: str, header_bytes: bytes) -> None:
        """
        Validates binary header against expected magic bytes signature.
        Rejects executable binaries and spoofed files.
        """
        ext = extension.lower()

        # Text files (.txt, .md): must not contain binary NULL bytes in header
        if ext in {"txt", "md", "markdown", "json", "csv"}:
            if b"\x00" in header_bytes[:512]:
                raise SecurityViolationError(
                    f"Binary content detected in text file '{ext}': Null byte in header"
                )
            return

        # Binary files with known signatures
        expected_magic = cls.MAGIC_SIGNATURES.get(ext)
        if expected_magic:
            if not header_bytes.startswith(expected_magic):
                raise SecurityViolationError(
                    f"Magic number mismatch for extension '.{ext}'. Header does not match valid {ext.upper()} format."
                )

    @classmethod
    async def validate_and_read_stream(
        cls,
        file: UploadFile,
        max_size_bytes: int = DEFAULT_MAX_FILE_SIZE
    ) -> Tuple[bytes, str, int]:
        """
        Streams file in chunks, accumulates size, calculates SHA-256, and validates magic bytes.
        Truncates immediately if max_size_bytes is exceeded.
        Returns: (file_bytes, content_hash, total_size)
        """
        filename = file.filename or "unnamed_file.txt"
        extension = cls.get_extension(filename)

        if not extension or extension not in cls.ALLOWED_EXTENSIONS:
            raise ValidationError(
                f"Unsupported file extension '{extension}'. Allowed extensions: {', '.join(sorted(cls.ALLOWED_EXTENSIONS))}"
            )

        buffer = bytearray()
        hasher = hashlib.sha256()
        total_read = 0
        header_checked = False

        while True:
            chunk = await file.read(STREAM_CHUNK_SIZE)
            if not chunk:
                break

            total_read += len(chunk)
            if total_read > max_size_bytes:
                # Immediate stream truncation
                raise SecurityViolationError(
                    f"File '{filename}' exceeds maximum allowed size of {max_size_bytes // (1024 * 1024)}MB"
                )

            buffer.extend(chunk)
            hasher.update(chunk)

            # Check magic number as soon as we have the first chunk
            if not header_checked and len(buffer) >= 16:
                cls.validate_magic_number(extension, bytes(buffer[:64]))
                header_checked = True

        if not header_checked and buffer:
            cls.validate_magic_number(extension, bytes(buffer[:64]))

        if total_read == 0:
            raise ValidationError(f"File '{filename}' is empty (0 bytes)")

        file_bytes = bytes(buffer)
        content_hash = hasher.hexdigest()
        return file_bytes, content_hash, total_read
