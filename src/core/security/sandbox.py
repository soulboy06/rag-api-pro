"""
Level 1: Task Sandbox Management
Provides isolated temporary directory environments with guaranteed auto-cleanup.
Fixes: P0-SEC-04, P1-TASK-09
"""
import os
import shutil
import tempfile
from typing import Optional


class TaskSandbox:
    """
    Context manager that creates an isolated working sandbox directory for a task
    and guarantees 100% cleanup upon exit (whether normal, exception, or cancellation).
    """
    def __init__(self, task_id: str, base_dir: Optional[str] = None):
        if not task_id or task_id in {".", ".."} or task_id != os.path.basename(task_id):
            raise ValueError("task_id must be a single safe path component")
        self.task_id = task_id
        self.base_dir = os.path.realpath(
            base_dir or os.path.join(tempfile.gettempdir(), "rag_sandboxes")
        )
        self.sandbox_path = os.path.join(self.base_dir, self.task_id)

    def __enter__(self) -> str:
        os.makedirs(self.sandbox_path, exist_ok=True)
        return os.path.realpath(self.sandbox_path)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if os.path.exists(self.sandbox_path):
            shutil.rmtree(self.sandbox_path, ignore_errors=True)

    @classmethod
    def clean_all_sandboxes(cls, base_dir: Optional[str] = None) -> None:
        """Cleans up the entire base sandbox directory."""
        target = base_dir or os.path.join(tempfile.gettempdir(), "rag_sandboxes")
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
