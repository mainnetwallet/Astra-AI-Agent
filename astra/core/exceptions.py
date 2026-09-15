"""Categorized, recoverable errors shared across Astra subsystems.

Every error knows its category and whether it is retryable so the orchestrator
and executor can choose recovery (retry / backoff / fallback / alternative tool
/ alternative provider / resume) without guessing from message text.
"""
from __future__ import annotations


class AstraError(Exception):
    category = "GenericError"
    retryable = False

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class NetworkError(AstraError):
    category = "NetworkError"
    retryable = True


class TimeoutError(AstraError):
    category = "TimeoutError"
    retryable = True


class ProviderError(AstraError):
    category = "ProviderError"
    retryable = True


class BrowserError(AstraError):
    category = "BrowserError"
    retryable = False


class ValidationError(AstraError):
    category = "ValidationError"
    retryable = False


class PermissionError(AstraError):
    category = "PermissionError"
    retryable = False


class TransactionError(AstraError):
    category = "TransactionError"
    retryable = False


class PluginError(AstraError):
    category = "PluginError"
    retryable = True


class ToolError(AstraError):
    category = "ToolError"
    retryable = False


class WorkflowError(AstraError):
    category = "WorkflowError"
    retryable = True