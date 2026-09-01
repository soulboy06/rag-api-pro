"""
Level 1: Zip-Defense Guard
Protects against ZipSlip path traversal, symlink attacks, and ZipBomb expansion explosions.
Fixes: P0-SEC-04
"""
import os
import zipfile
from typing import List, Optional
from src.core.exceptions import SecurityViolationError

# Safe defaults
MAX_ZIP_ENTRIES = 1000
MAX_SINGLE_FILE_SIZE = 50 * 1024 * 1024  # 50MB
MAX_TOTAL_UNCOMPRESSED_SIZE = 200 * 1024 * 1024  # 200MB
MAX_COMPRESSION_RATIO = 100.0  # 100:1 ratio limit


class ZipGuard:
    @classmethod
    def validate_and_extract(
        cls,
        zip_path_or_bytes,
        target_dir: str,
        max_entries: int = MAX_ZIP_ENTRIES,
        max_single_size: int = MAX_SINGLE_FILE_SIZE,
        max_total_size: int = MAX_TOTAL_UNCOMPRESSED_SIZE,
        max_ratio: float = MAX_COMPRESSION_RATIO,
    ) -> List[str]:
        """
        Safely inspects and extracts zip contents into target_dir.
        Enforces path containment, symlink rejection, and compression ratio limits.
        Returns list of safely extracted relative file paths.
        """
        real_target_dir = os.path.realpath(target_dir)
        os.makedirs(real_target_dir, exist_ok=True)

        extracted_files = []
        total_uncompressed = 0

        with zipfile.ZipFile(zip_path_or_bytes, "r") as zf:
            infolist = zf.infolist()

            # 1. Check entry count
            if len(infolist) > max_entries:
                raise SecurityViolationError(
                    f"Zip contains {len(infolist)} files, exceeding safety limit of {max_entries}"
                )

            # 2. Pre-scan all entries for ZipSlip, symlinks, and ZipBomb metrics
            for member in infolist:
                # ZipSlip Protection: Ensure resolved path stays strictly inside target_dir
                target_path = os.path.realpath(os.path.join(real_target_dir, member.filename))
                if not (target_path == real_target_dir or target_path.startswith(real_target_dir + os.sep)):
                    raise SecurityViolationError(
                        f"ZipSlip directory traversal detected in entry: '{member.filename}'"
                    )

                # Symlink Protection: Reject Unix symlinks (0o120000 mode)
                mode = member.external_attr >> 16
                if mode & 0o120000 == 0o120000:
                    raise SecurityViolationError(
                        f"Symbolic link detected in zip entry: '{member.filename}'"
                    )

                # ZipBomb Protection: Check individual file size
                if member.file_size > max_single_size:
                    raise SecurityViolationError(
                        f"Zip entry '{member.filename}' uncompressed size ({member.file_size // (1024*1024)}MB) exceeds limit of {max_single_size // (1024*1024)}MB"
                    )

                total_uncompressed += member.file_size
                if total_uncompressed > max_total_size:
                    raise SecurityViolationError(
                        f"Zip total uncompressed size exceeds limit of {max_total_size // (1024*1024)}MB"
                    )

                # Compression ratio check (for non-empty files)
                if member.compress_size > 0:
                    ratio = member.file_size / member.compress_size
                    if ratio > max_ratio and member.file_size > 1024 * 1024:
                        raise SecurityViolationError(
                            f"Suspicious compression ratio ({ratio:.1f}:1) detected for entry '{member.filename}'"
                        )

            # 3. Perform Safe Extraction
            for member in infolist:
                dest_path = os.path.realpath(os.path.join(real_target_dir, member.filename))
                if member.is_dir():
                    os.makedirs(dest_path, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    with zf.open(member) as source, open(dest_path, "wb") as target:
                        # Stream in 64KB chunks
                        while True:
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            target.write(chunk)
                    extracted_files.append(os.path.relpath(dest_path, real_target_dir))

        return extracted_files
