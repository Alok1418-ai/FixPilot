"""Security sandbox: secret scanning, command policy, sandboxed execution, patch safety."""

from .executor import ExecResult, SandboxExecutor
from .patch import (
    ApplyResult,
    FileProposal,
    ParsedPatch,
    PatchError,
    apply_patch,
    apply_to_content,
    diff_stats,
    make_diff,
    parse_unified,
    patch_preview,
    proposals_to_patch,
    revert_patch,
    validate_patch,
)
from .policy import ALLOW, CONFIRM, DENY, CommandPolicy, Verdict
from .secrets import SecretFinding, SecretScanner, is_sensitive_path, scan_text

__all__ = [
    "ALLOW",
    "CONFIRM",
    "DENY",
    "ApplyResult",
    "CommandPolicy",
    "ExecResult",
    "FileProposal",
    "ParsedPatch",
    "PatchError",
    "SandboxExecutor",
    "SecretFinding",
    "SecretScanner",
    "Verdict",
    "apply_patch",
    "apply_to_content",
    "diff_stats",
    "is_sensitive_path",
    "make_diff",
    "parse_unified",
    "patch_preview",
    "proposals_to_patch",
    "revert_patch",
    "scan_text",
    "validate_patch",
]
