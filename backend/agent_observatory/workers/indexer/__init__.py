"""Local agent session discovery and incremental indexing."""

from . import discovery
from .adapters import (
    CodexJsonlSource,
    PlanAction,
    SourceAdapter,
    SourceDescriptor,
    SourcePlan,
    StepCodeSqliteSource,
    XDCodeJsonlSource,
)
from .checkpoints import CheckpointRepository
from .ingestion import EventIngestor
from .linking import SessionLinker
from .local_sources import LocalInsightsIndexer
from .manager import IndexerWorker
from .orchestrator import IndexerOrchestrator
from .parser import NormalizedEvent, parse_json_line
from .pi import PiJsonlSource
from .registry import build_registry
from .scanner import RolloutScanner
from .stepcli import StepCliNativeSource
from .stepcode import StepCodeImporter, StepCodeItem, StepCodeTurn
from .stepgo_host import StepGoHostSource

__all__ = [
    "IndexerWorker",
    "IndexerOrchestrator",
    "CheckpointRepository",
    "CodexJsonlSource",
    "EventIngestor",
    "NormalizedEvent",
    "PlanAction",
    "PiJsonlSource",
    "RolloutScanner",
    "SessionLinker",
    "LocalInsightsIndexer",
    "SourceDescriptor",
    "SourceAdapter",
    "SourcePlan",
    "StepCodeImporter",
    "StepCodeItem",
    "StepCodeSqliteSource",
    "StepCodeTurn",
    "StepCliNativeSource",
    "StepGoHostSource",
    "XDCodeJsonlSource",
    "build_registry",
    "discovery",
    "parse_json_line",
]
