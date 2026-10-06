"""Model layer: registry, providers, routing, multimodal ingestion."""

from .media import IngestedInput, InputAdapter, LogSignal, extract_signals, repair_transcript
from .providers import Completion, build_providers
from .registry import ModelProfile, ModelRegistry, default_profiles, TASKS
from .router import ModelRouter, RouteDecision

__all__ = [
    "Completion",
    "IngestedInput",
    "InputAdapter",
    "LogSignal",
    "ModelProfile",
    "ModelRegistry",
    "ModelRouter",
    "RouteDecision",
    "TASKS",
    "build_providers",
    "default_profiles",
    "extract_signals",
    "repair_transcript",
]
