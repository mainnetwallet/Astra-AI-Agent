"""Explicit execution state for the agent loop and task engine.

States are plain strings (JSON-friendly for the frontend). The task engine and
workflows move a unit of work through these states and report `state`
transitions in events so the Activity Log can follow what is happening.
"""
from __future__ import annotations

EXECUTION_STATES = (
    "IDLE", "THINKING", "PLANNING", "WAITING_TOOL", "EXECUTING",
    "OBSERVING", "VERIFYING", "WAITING_USER", "PAUSED",
    "COMPLETED", "FAILED", "CANCELLED",
)

TASK_STATUSES = (
    "pending", "ready", "running", "done",
    "failed", "skipped", "cancelled",
)

WORKFLOW_STATUSES = ("created", "running", "paused", "completed", "failed", "cancelled")

SCHEDULE_KINDS = ("oneshot", "interval", "daily", "weekly", "deadline")

SOURCE_LEVELS = ("OFFICIAL", "TRUSTED", "SECONDARY", "COMMUNITY", "UNKNOWN")


def valid_execution_state(s: str) -> bool:
    return s in EXECUTION_STATES
