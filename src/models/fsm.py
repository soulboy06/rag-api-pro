"""
Level 0: Deterministic Finite State Machine (FSM) for Ingestion Tasks
Enforces valid state transitions and terminal state protection.
Fixes: P1-API-01, P1-TASK-08, P3-CODE-01
"""
from enum import Enum
from typing import Dict, Set, Tuple
from src.core.exceptions import InvalidStateTransitionError


class TaskState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_WAITING = "RETRY_WAITING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_SUCCEEDED = "PARTIAL_SUCCEEDED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    CANCELLED = "CANCELLED"


# Terminal states that cannot transition into any other state
TERMINAL_STATES: Set[TaskState] = {
    TaskState.SUCCEEDED,
    TaskState.DEAD_LETTER,
    TaskState.CANCELLED,
}

# Explicit Allowed Transitions Table
ALLOWED_TRANSITIONS: Dict[TaskState, Set[TaskState]] = {
    TaskState.PENDING: {
        TaskState.RUNNING,
        TaskState.CANCELLED,
    },
    TaskState.RUNNING: {
        TaskState.SUCCEEDED,
        TaskState.PARTIAL_SUCCEEDED,
        TaskState.FAILED,
        TaskState.RETRY_WAITING,
        TaskState.CANCELLED,
    },
    TaskState.RETRY_WAITING: {
        TaskState.RUNNING,
        TaskState.DEAD_LETTER,
        TaskState.CANCELLED,
    },
    TaskState.FAILED: {
        TaskState.RETRY_WAITING,
        TaskState.DEAD_LETTER,
        TaskState.PENDING,  # Manual retry / re-parse
        TaskState.CANCELLED,
    },
    TaskState.PARTIAL_SUCCEEDED: {
        TaskState.PENDING,  # Manual re-parse
    },
    TaskState.SUCCEEDED: set(),  # Terminal
    TaskState.DEAD_LETTER: set(),  # Terminal
    TaskState.CANCELLED: set(),  # Terminal
}


class TaskFSM:
    @classmethod
    def can_transition(cls, from_state: str, to_state: str) -> bool:
        """Returns True if transition from from_state to to_state is valid."""
        try:
            current = TaskState(from_state)
            target = TaskState(to_state)
        except ValueError:
            return False

        return target in ALLOWED_TRANSITIONS.get(current, set())

    @classmethod
    def validate_transition(cls, task_id: str, from_state: str, to_state: str) -> None:
        """
        Validates transition and raises InvalidStateTransitionError if illegal.
        Prevents terminal state tampering and illegal rollbacks.
        """
        try:
            current = TaskState(from_state)
            target = TaskState(to_state)
        except ValueError as e:
            raise InvalidStateTransitionError(
                f"Unknown task state value in transition: {from_state} -> {to_state}"
            )

        if current in TERMINAL_STATES and current != target:
            raise InvalidStateTransitionError(
                f"Task '{task_id}' has reached terminal state '{current.value}' and cannot transition to '{target.value}'"
            )

        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise InvalidStateTransitionError(
                f"Illegal state transition for task '{task_id}': '{current.value}' -> '{target.value}'"
            )

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        """Returns True if the given state is a terminal state."""
        try:
            return TaskState(state) in TERMINAL_STATES
        except ValueError:
            return False
