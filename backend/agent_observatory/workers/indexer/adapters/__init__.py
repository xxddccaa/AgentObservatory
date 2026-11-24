"""Read-only source adapters for supported local coding agents."""
from __future__ import annotations

from .agy import AgyTranscriptSource
from .base import PlanAction, SourceAdapter, SourceDescriptor, SourcePlan
from .claude import ClaudeJsonlSource
from .codex import CodexJsonlSource, StepCodeSqliteSource, XDCodeJsonlSource
from .dsh import DshJsonlSource
from .gemini import GeminiCliSource
from .generic import GenericJsonlSource
from .opencode import OpenCodeJsonSource
from .pi import PiJsonlSource
from .stepcli import StepCliNativeSource
from .stepcode import StepCodeImporter, StepCodeItem, StepCodeTurn
from .stepgo_host import StepGoHostSource

__all__ = [
    "AgyTranscriptSource",
    "ClaudeJsonlSource",
    "CodexJsonlSource",
    "DshJsonlSource",
    "GeminiCliSource",
    "GenericJsonlSource",
    "OpenCodeJsonSource",
    "PiJsonlSource",
    "PlanAction",
    "SourceAdapter",
    "SourceDescriptor",
    "SourcePlan",
    "StepCliNativeSource",
    "StepCodeImporter",
    "StepCodeItem",
    "StepCodeSqliteSource",
    "StepCodeTurn",
    "StepGoHostSource",
    "XDCodeJsonlSource",
]
