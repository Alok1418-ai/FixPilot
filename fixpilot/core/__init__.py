"""Core agent loop: engine (investigate/plan), fixes, verifier, narration, agent.

Exports are resolved lazily (PEP 562) so that ``fixpilot.core.fixes`` can be
imported from the skills package without pulling in the whole agent — the two
packages depend on each other, and a lazy facade keeps that acyclic at import
time.
"""

from __future__ import annotations

from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    # name: (module, attribute)
    "FixPilotAgent": (".agent", "FixPilotAgent"),
    "FixPlanner": (".engine", "FixPlanner"),
    "Investigation": (".engine", "Investigation"),
    "Investigator": (".engine", "Investigator"),
    "PatchPlan": (".engine", "PatchPlan"),
    "FixCandidate": (".fixes", "FixCandidate"),
    "generate_candidates": (".fixes", "generate_candidates"),
    "describe_strategies": (".fixes", "describe_strategies"),
    "explain_cause": (".narrator", "explain_cause"),
    "explain_patch": (".narrator", "explain_patch"),
    "explain_verification": (".narrator", "explain_verification"),
    "session_report": (".narrator", "session_report"),
    "spoken_summary": (".narrator", "spoken_summary"),
    "Verifier": (".verifier", "Verifier"),
    "parse_test_output": (".verifier", "parse_test_output"),
    "syntax_check": (".verifier", "syntax_check"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:  # pragma: no cover - thin import shim
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:  # pragma: no cover - introspection helper
    return sorted(_EXPORTS)
