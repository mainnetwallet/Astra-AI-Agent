"""Explicit execution state for the agent loop and task engine.

States are plain strings (JSON-friendly for the frontend): the orchestrator
holds an execution in one state at a time and reports `state` transitions in
events so the Live view can follow what the agent is doing.
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