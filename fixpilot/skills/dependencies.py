"""Dependency and environment triage.

A large share of "it broke overnight" reports are environment drift: a package
that was never declared, a lockfile that disagrees with the manifest, or a
version range that silently resolved differently in CI.  This skill makes those
visible without installing anything.
"""

from __future__ import annotations

import re

from .base import Hypothesis, Skill, SkillContext, SkillResult

IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))", re.M)

STDLIB = {
    "abc", "argparse", "ast", "asyncio", "base64", "binascii", "bisect", "calendar", "collections", "concurrent",
    "contextlib", "copy", "csv", "ctypes", "dataclasses", "datetime", "decimal", "difflib", "email", "enum",
    "errno", "faulthandler", "fnmatch", "fractions", "functools", "gc", "getpass", "glob", "gzip", "hashlib",
    "heapq", "hmac", "html", "http", "imaplib", "importlib", "inspect", "io", "ipaddress", "itertools", "json",
    "keyword", "linecache", "locale", "logging", "lzma", "math", "mimetypes", "multiprocessing", "operator",
    "os", "pathlib", "pickle", "pkgutil", "platform", "plistlib", "pprint", "profile", "pty", "queue", "random",
    "re", "readline", "resource", "secrets", "select", "shlex", "shutil", "signal", "site", "smtplib", "socket",
    "sqlite3", "ssl", "stat", "statistics", "string", "struct", "subprocess", "sys", "sysconfig", "tarfile",
    "tempfile", "textwrap", "threading", "time", "timeit", "token", "tokenize", "traceback", "types", "typing",
    "unicodedata", "unittest", "urllib", "uuid", "venv", "warnings", "wave", "weakref", "webbrowser", "xml",
    "zipfile", "zlib", "fixpilot",
}

#: import name -> distribution name, for the common mismatches.
IMPORT_TO_PACKAGE = {
    "yaml": "PyYAML",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "jwt": "PyJWT",
    "OpenSSL": "pyOpenSSL",
    "dateutil": "python-dateutil",
    "psycopg2": "psycopg2-binary",
    "MySQLdb": "mysqlclient",
    "rest_framework": "djangorestframework",
    "attr": "attrs",
    "serial": "pyserial",
}

LOCAL_ROOTS = {"app", "src", "lib", "tests", "test", "api", "core", "utils", "scripts", "internal", "pkg", "cmd"}


class DependencyDoctor(Skill):
    name = "dependency_doctor"
    title = "Dependency triage"
    description = "Compares what the code imports with what the manifest declares and flags environment drift."
    priority = 68
    cost = "fast"
    triggers = (r"\b(ModuleNotFoundError|ImportError|no module named|cannot find module|version|install|dependency|requirements|lockfile)\b",)

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        declared = {
            name.lower().split("[")[0].split(">")[0].split("=")[0].strip()
            for values in ctx.index.dependencies.values()
            for name in values
            if name and not name.startswith("npm:")
        }
        manifests = [
            path
            for path in ctx.index.files
            if path.rsplit("/", 1)[-1]
            in {"requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "package.json", "go.mod", "Cargo.toml", "Gemfile"}
        ]
        lockfiles = [
            path
            for path in ctx.index.files
            if path.rsplit("/", 1)[-1] in {"poetry.lock", "Pipfile.lock", "yarn.lock", "pnpm-lock.yaml", "package-lock.json", "Cargo.lock", "go.sum", "Gemfile.lock"}
        ]
        imported = self._imported_modules(ctx)
        missing: list[str] = []
        for module in sorted(imported):
            root = module.split(".")[0]
            if root in STDLIB or root.startswith("_") or root in LOCAL_ROOTS:
                continue
            if root in ctx.index.files or f"{root}.py" in ctx.index.files or any(p.startswith(f"{root}/") for p in ctx.index.files):
                continue
            package = IMPORT_TO_PACKAGE.get(root, root).lower()
            if package in declared or root.lower() in declared:
                continue
            if not declared:
                # No manifest parsed at all: report drift without accusing the import.
                missing.append(root)
                continue
            missing.append(root)

        if missing:
            evidence = ctx.evidence(
                kind="dependency",
                claim=f"{len(missing)} imported module(s) are not declared in any manifest: {', '.join(missing[:6])}",
                detail=(
                    "Declared: " + (", ".join(sorted(declared)[:8]) if declared else "nothing parsed")
                    + (f" · manifests: {', '.join(manifests[:3])}" if manifests else " · no manifest found")
                ),
                confidence=0.7 if declared else 0.45,
                source="import scan vs manifest",
                skill=self.name,
            )
            result.evidence.append(evidence)
            result.hypotheses.append(
                Hypothesis(
                    cause=f"environment drift: {', '.join(missing[:3])} is imported but not declared",
                    category="dependency",
                    explanation=(
                        "Code that imports an undeclared package works only on the machine where it happens to be installed. "
                        "The fix belongs in the dependency manifest, so the environment can reproduce it."
                    ),
                    confidence=0.62 if declared else 0.4,
                    evidence_ids=[evidence.id],
                    file=manifests[0] if manifests else "",
                    strategy="declare-dependency",
                    fix_plan=(
                        f"Add {', '.join(missing[:3])} to {manifests[0] if manifests else 'the dependency manifest'} "
                        "(or make the import optional if it is only needed for an extra)."
                    ),
                    verification_plan="reinstall from the manifest in a clean environment, then re-run the failing test",
                    falsifier="if the package is vendored or provided by the runtime image, the import is fine as-is",
                    skill=self.name,
                )
            )
        if manifests and not lockfiles and any(path.endswith("package.json") for path in manifests):
            result.notes.append("package.json present without a lockfile — dependency resolution is not reproducible")
        if ctx.index.dependencies.get("poetry.lock") or ctx.index.dependencies.get("Pipfile.lock"):
            result.notes.append("lockfile detected; verify the failed environment used the same lock revision")
        result.outputs = {
            "manifests": manifests,
            "lockfiles": lockfiles,
            "declared_count": len(declared),
            "undeclared_imports": missing[:20],
        }
        return result

    @staticmethod
    def _imported_modules(ctx: SkillContext) -> list[str]:
        modules: set[str] = set()
        for path, facts in ctx.index.files.items():
            if facts.language not in {"python", "javascript", "typescript"}:
                continue
            for module in facts.imports:
                if not module or module.startswith("."):
                    continue
                root = module.split("/")[0].split(".")[0]
                if root.startswith("@"):
                    root = "/".join(module.split("/")[:2])
                if root:
                    modules.add(root)
        return sorted(modules)
