"""Investigation engine: understand -> gather evidence -> rank hypotheses -> plan a patch."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import Settings
from ..memory.lessons import LessonStore
from ..memory.project import ProjectMemoryStore
from ..memory.sessions import Session
from ..models.media import IngestedInput, first_error_line, looks_like_ui_bug, summary_of, extract_quoted_identifiers
from ..models.router import ModelRouter
from ..repo.githistory import GitHistory
from ..repo.graph import CodeGraph
from ..repo.indexer import RepoIndex
from ..security.executor import SandboxExecutor
from ..security.patch import FileProposal, ParsedPatch, parse_unified, proposals_to_patch, diff_stats, validate_patch
from ..security.secrets import is_sensitive_path
from ..skills.base import Evidence, Hypothesis, SkillContext, SkillRegistry, dedupe_hypotheses, rank_hypotheses
from ..util import clamp, excerpt, now_iso, tokens
from .fixes import FixCandidate, describe_strategies


@dataclass(slots=True)
class Investigation:
    understanding: dict = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    skill_reports: list[dict] = field(default_factory=list)
    skills_run: list[str] = field(default_factory=list)
    skills_considered: list[dict] = field(default_factory=list)
    lessons_used: list[dict] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    patch_candidates: list[FixCandidate] = field(default_factory=list)
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def evidence_dicts(self) -> list[dict]:
        return [e.to_dict() for e in self.evidence]

    def hypothesis_dicts(self) -> list[dict]:
        return [h.to_dict() for h in self.hypotheses]

    def top(self) -> Hypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None


@dataclass(slots=True)
class PatchPlan:
    hypothesis: dict = field(default_factory=dict)
    candidates: list[FixCandidate] = field(default_factory=list)
    patch_text: str = ""
    patch_stats: dict = field(default_factory=dict)
    validation: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    risk: dict = field(default_factory=dict)
    summary: str = ""
    rationale: str = ""
    verification_plan: list[str] = field(default_factory=list)
    test_targets: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    repro_command: list[str] = field(default_factory=list)
    requires_review: bool = True
    parse_failed: bool = False
    strategies_tried: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hypothesis": self.hypothesis,
            "summary": self.summary,
            "rationale": self.rationale,
            "patch": self.patch_text,
            "stats": self.patch_stats,
            "validation": self.validation,
            "files": self.files,
            "risk": self.risk,
            "verification_plan": self.verification_plan,
            "test_targets": self.test_targets,
            "test_files": self.test_files,
            "repro_command": self.repro_command,
            "requires_review": self.requires_review,
            "parse_failed": self.parse_failed,
            "strategies_tried": self.strategies_tried,
            "candidates": [c.to_dict() for c in self.candidates],
        }


class Investigator:
    """Runs the skill plan for a bug report and produces ranked hypotheses."""

    def __init__(
        self,
        settings: Settings,
        *,
        index: RepoIndex,
        graph: CodeGraph,
        history: GitHistory,
        executor: SandboxExecutor,
        router: ModelRouter,
        memory: ProjectMemoryStore,
        lessons: LessonStore,
        registry: SkillRegistry,
    ) -> None:
        self.settings = settings
        self.index = index
        self.graph = graph
        self.history = history
        self.executor = executor
        self.router = router
        self.memory = memory
        self.lessons = lessons
        self.registry = registry

    # -- understanding -------------------------------------------------
    def understand(self, ingested: IngestedInput, session: Session) -> dict:
        frames = [s for s in ingested.signals if s.kind == "frame"]
        errors = [s for s in ingested.signals if s.kind in {"error", "panic", "assertion", "test_failure"}]
        files: list[str] = []
        for frame in frames:
            resolved = _resolve(ingested, frame.path, self.index)
            if resolved:
                files.append(resolved)
        for signal in ingested.signals:
            if signal.kind in {"file_ref", "test_failure"}:
                resolved = _resolve(ingested, signal.path, self.index)
                if resolved:
                    files.append(resolved)
        keywords = [w for w in tokens(ingested.text) if len(w) > 3][:16]
        summary = _understand_summary(ingested, errors, files)
        questions = self._questions(ingested, files, errors)
        return {
            "summary": summary,
            "intent": ingested.hint_intent or ("fix" if errors else "investigate"),
            "channel": ingested.channel,
            "symptom": summary_of(ingested.text, 300),
            "first_error": first_error_line(ingested.text),
            "error_types": sorted({e.value for e in errors if e.value}),
            "affected_files": list(dict.fromkeys(files))[:8],
            "keywords": keywords,
            "signals": len(ingested.signals),
            "frames": len(frames),
            "is_ui_report": looks_like_ui_bug(ingested.text),
            "mentioned_identifiers": extract_quoted_identifiers(ingested.text),
            "needs_clarification": questions,
            "model_route": self.router.route("root_cause").to_dict(),
            "understood_at": now_iso(),
        }

    def _questions(self, ingested: IngestedInput, files: list[str], errors: list) -> list[str]:
        questions: list[str] = []
        if not ingested.text.strip():
            questions.append("The report has no text — what behaviour did you expect, and what happened instead?")
        if not errors and not files:
            questions.append("I could not find a stack frame or file reference. Paste the error output or name the file/function involved.")
        if ingested.channel == "image" and not ingested.attachments:
            questions.append("The screenshot could not be stored — try attaching it again.")
        if len(ingested.text) < 25 and (errors or files):
            questions.append("The report is very short — which input triggers it, and how often does it happen?")
        if not self.index.test_command:
            questions.append("This project has no detectable test command; how do you normally check a change?")
        return questions[:3]

    # -- investigation -------------------------------------------------
    def investigate(self, session: Session, ingested: IngestedInput, *, cost_budget: str = "deep", emit=None) -> Investigation:
        started = time.perf_counter()
        understanding = self.understand(ingested, session)
        ctx = SkillContext(
            settings=self.settings,
            index=self.index,
            graph=self.graph,
            history=self.history,
            executor=self.executor,
            router=self.router,
            memory=self.memory,
            lessons=self.lessons,
            ingested=ingested,
            session=session,
            emit=emit,
            scratch={"mentioned_identifiers": understanding["mentioned_identifiers"]},
        )
        investigation = Investigation(understanding=understanding)

        # Lessons: has this project seen this symptom before?
        seeds = [s for s in ingested.signals if s.kind == "frame"] or []
        seed_files = understanding["affected_files"]
        matches = self.lessons.find_similar(
            text=ingested.text,
            signals=ingested.signals,
            files=seed_files,
            limit=3,
        )
        for match in matches:
            investigation.lessons_used.append(match.to_dict())
            investigation.evidence.append(
                ctx.evidence(
                    kind="memory",
                    claim=f"a similar failure was handled before: {excerpt(match.lesson.symptom, 140)}",
                    detail=f"{match.why}. Root cause then: {excerpt(match.lesson.root_cause, 200)}. Strategy: {match.lesson.strategy}",
                    confidence=0.5 + 0.3 * match.score,
                    source=f"lesson from session {match.lesson.session_id or 'unknown'}",
                    skill="lesson_store",
                )
            )
            if match.lesson.outcome == "verified" and match.lesson.files:
                investigation.notes.append(
                    f"past verified fix touched {', '.join(match.lesson.files[:3])} — included in the search space"
                )
        if matches:
            emit and emit("lesson.reused", {"symptom": excerpt(matches[0].lesson.symptom, 140), "score": round(matches[0].score, 2)})

        # Select and run skills.
        selected = self.registry.select(ctx, limit=8, cost_budget=cost_budget)
        investigation.plan = [{"skill": skill.name, "score": round(score, 2), "cost": skill.cost} for skill, score in selected]
        emit and emit("skills.selected", {"skills": [name for name, _, in [(s.name, sc) for s, sc in selected]]} if False else {"skills": [s.name for s, _ in selected], "plan": investigation.plan})

        hypotheses: list[Hypothesis] = []
        for skill, score in selected:
            skill_started = time.perf_counter()
            try:
                result = skill.run(ctx)
            except Exception as exc:  # a broken skill must not sink the investigation
                investigation.skill_reports.append(
                    {"skill": skill.name, "error": f"{type(exc).__name__}: {exc}", "evidence": [], "hypotheses": []}
                )
                continue
            result.duration_ms = int((time.perf_counter() - skill_started) * 1000)
            investigation.skills_run.append(skill.name)
            investigation.skill_reports.append(result.to_dict())
            investigation.evidence.extend(result.evidence)
            for hypothesis in result.hypotheses:
                hypothesis.confidence = clamp(hypothesis.confidence * (0.85 + 0.15 * score))
                hypotheses.append(hypothesis)
            investigation.notes.extend(result.notes)

        ctx.scratch["hypotheses"] = hypotheses
        ctx.scratch["evidence"] = investigation.evidence + ctx.scratch.get("evidence", [])
        ranked = rank_hypotheses(dedupe_hypotheses(hypotheses), ctx)
        investigation.hypotheses = ranked
        ctx.scratch["ranked_hypotheses"] = ranked

        # Post-ranking skills (patch authoring) run now that a top hypothesis exists.
        for skill in self.registry.all():
            if not skill.post_ranking or not ranked:
                continue
            if skill.cost not in ({"fast", "normal", "deep"} if cost_budget == "deep" else {"fast"}):
                continue
            try:
                post_result = skill.run(ctx)
                investigation.skill_reports.append(post_result.to_dict())
                investigation.evidence.extend(post_result.evidence)
                investigation.notes.extend(post_result.notes)
                investigation.skills_run.append(skill.name)
            except Exception as exc:  # pragma: no cover
                investigation.notes.append(f"{skill.name} failed safely: {exc}")
        investigation.patch_candidates = list(ctx.scratch.get("patch_candidates") or [])

        investigation.duration_ms = int((time.perf_counter() - started) * 1000)
        if emit:
            emit(
                "evidence.gathered",
                {"count": len(investigation.evidence), "skills": investigation.skills_run},
            )
            top = investigation.top()
            emit(
                "hypothesis.ranked",
                {
                    "top": top.to_dict() if top else None,
                    "count": len(ranked),
                    "alternatives": [h.to_dict() for h in ranked[1:4]],
                },
            )
        return investigation


class FixPlanner:
    """Turns the top hypothesis + patch candidates into one reviewable patch."""

    def __init__(self, settings: Settings, *, index: RepoIndex, graph: CodeGraph, registry: SkillRegistry) -> None:
        self.settings = settings
        self.index = index
        self.graph = graph
        self.registry = registry

    def plan(self, investigation: Investigation, *, candidates: list[FixCandidate] | None = None) -> PatchPlan:
        plan = PatchPlan()
        top = investigation.top()
        plan.hypothesis = top.to_dict() if top else {}
        finalists = list(candidates) if candidates else self._candidates_from(investigation)
        plan.strategies_tried = list(dict.fromkeys(candidate.strategy for candidate in finalists))

        accepted: list[FixCandidate] = []
        for candidate in finalists:
            sensitive, label = is_sensitive_path(candidate.path)
            if sensitive:
                plan.validation.append(f"rejected candidate for protected {label}: {candidate.path}")
                continue
            if not candidate.changed:
                continue
            accepted.append(candidate)
        # Prefer minimal, non-behaviour-changing changes when confidence is close,
        # and never queue the same edit twice.
        deduped: dict[tuple[str, str], FixCandidate] = {}
        for candidate in accepted:
            key = (candidate.path, candidate.new_content)
            existing = deduped.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                deduped[key] = candidate
        accepted = list(deduped.values())
        accepted.sort(key=lambda c: (-round(c.confidence, 2), c.behaviour_change, len(c.new_content)))
        plan.candidates = accepted[:4]

        if not plan.candidates:
            plan.summary = "No safe automated fix was found for this root cause."
            plan.rationale = (
                top.explanation if top else ""
            ) or "The evidence did not support a minimal, verifiable change."
            plan.requires_review = True
            plan.parse_failed = True
            plan.test_files = _locator_test_files(investigation)
            plan.verification_plan = [
                "FixPilot could not build a change it can verify, so nothing will be applied.",
                "Record the failing test as the acceptance criterion, then re-run the session with a narrower report.",
            ]
            return plan

        proposals = [
            FileProposal(
                path=candidate.path,
                old_content=candidate.old_content,
                new_content=candidate.new_content,
                reason=candidate.reason,
                strategy=candidate.strategy,
                confidence=candidate.confidence,
            )
            for candidate in plan.candidates
        ]
        patch_text, parsed = proposals_to_patch(proposals)
        plan.patch_text = patch_text
        plan.parse_failed = not parsed.files
        plan.validation.extend(validate_patch(parsed, self.settings.repo_root, self.settings) if parsed.files else ["patch could not be parsed"])
        plan.patch_stats = diff_stats(parsed) if parsed.files else {}
        plan.files = parsed.touched_paths()

        impact = self.graph.impact(plan.files, depth=2) if plan.files else None
        behaviour_change = any(c.behaviour_change for c in plan.candidates)
        risk_level = "low"
        risk_notes: list[str] = []
        if impact is not None:
            if impact.dependent_files:
                risk_notes.append(f"{len(impact.dependent_files)} file(s) import the changed code")
            if impact.dependent_tests:
                risk_notes.append(f"{len(impact.dependent_tests)} related test file(s) identified")
        if behaviour_change:
            risk_level = "medium"
            risk_notes.append("this change alters behaviour for an edge case, not just error reporting")
        if len(plan.files) > 3:
            risk_level = "medium"
            risk_notes.append("the patch touches several files")
        if plan.patch_stats.get("additions", 0) > 60:
            risk_level = "medium"
            risk_notes.append("the diff is larger than FixPilot's usual minimal change")
        if not plan.files:
            risk_level = "high"
            risk_notes.append("no application-ready diff could be produced")
        plan.risk = {
            "level": risk_level,
            "notes": risk_notes,
            "blast_radius": impact.to_dict() if impact else {},
            "behaviour_change": behaviour_change,
            "strategies": plan.strategies_tried,
            "descriptions": {name: _describe(name) for name in plan.strategies_tried},
        }
        plan.summary = (
            f"{plan.candidates[0].reason}" if plan.candidates else ""
        )
        plan.rationale = self._rationale(top, plan)
        plan.verification_plan = self._verification_plan(top, impact)
        plan.test_targets = impact.dependent_tests[:6] if impact else []
        plan.test_files = _locator_test_files(investigation)
        return plan

    @staticmethod
    def _candidates_from(investigation: Investigation) -> list[FixCandidate]:
        raw = investigation.skill_reports
        candidates: list[FixCandidate] = []
        for report in raw:
            if report.get("skill") != "patch_author":
                continue
            for item in report.get("outputs", {}).get("candidates", []):
                candidates.append(
                    FixCandidate(
                        strategy=item.get("strategy", ""),
                        path=item.get("path", ""),
                        old_content="",
                        new_content="",
                        reason=item.get("reason", ""),
                        confidence=float(item.get("confidence", 0.4)),
                        lineno=int(item.get("lineno", 0)),
                        symbol=item.get("symbol", ""),
                        behaviour_change=bool(item.get("behaviour_change")),
                        verification_hint=item.get("verification_hint", ""),
                    )
                )
        return candidates

    @staticmethod
    def _rationale(top: Hypothesis | None, plan: PatchPlan) -> str:
        if top is None:
            return "FixPilot applied the safest available transform."
        parts = [f"Root cause: {top.cause}."]
        if top.explanation:
            parts.append(top.explanation)
        if plan.candidates:
            parts.append(f"Chosen strategy: {plan.candidates[0].strategy} ({_describe(plan.candidates[0].strategy)}).")
            if len(plan.candidates) > 1:
                parts.append(
                    f"{len(plan.candidates) - 1} alternative candidate(s) are held in reserve and tried automatically if tests fail."
                )
        return " ".join(parts)

    @staticmethod
    def _verification_plan(top: Hypothesis | None, impact) -> list[str]:
        steps: list[str] = []
        if impact and impact.dependent_tests:
            steps.append(f"run the {len(impact.dependent_tests)} related test file(s) first")
        steps.append("run the project test suite")
        steps.append("re-check that every changed file still parses")
        if top and top.verification_plan:
            steps.append(top.verification_plan)
        steps.append("if anything fails, refine the patch and re-run before reporting success")
        return steps


def _locator_test_files(investigation: Investigation) -> list[str]:
    """Reuse what the test_locator skill already discovered."""
    for report in investigation.skill_reports:
        if report.get("skill") != "test_locator":
            continue
        outputs = report.get("outputs") or {}
        files = [str(path) for path in outputs.get("test_files", []) if path]
        if files:
            return files[:8]
    return []


def _describe(strategy: str) -> str:
    for item in describe_strategies():
        if item["name"] == strategy:
            return item["description"]
    if strategy.startswith("model:"):
        return "authored by a code model and validated against the same checks as deterministic candidates"
    return "targeted change"


def _resolve(ingested: IngestedInput, raw: str, index: RepoIndex) -> str:
    """Resolve a possibly-absolute path from a log onto an indexed file."""
    if not raw:
        return ""
    candidate = raw.replace("\\", "/").lstrip("./")
    if candidate in index.files:
        return candidate
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    for size in range(len(parts), 0, -1):
        suffix = "/".join(parts[-size:])
        if suffix in index.files:
            return suffix
    if parts:
        name = parts[-1]
        for path in index.files:
            if path.endswith("/" + name) or path == name:
                return path
    return ""


def _understand_summary(ingested: IngestedInput, errors: list, files: list[str]) -> str:
    error_types = ", ".join(sorted({e.value for e in errors if e.value})) or "no explicit exception"
    where = f" in {files[0]}" if files else ""
    channel = {"voice": "voice note", "log": "pasted log", "image": "screenshot", "mixed": "mixed input", "text": "text report"}.get(
        ingested.channel, ingested.channel
    )
    headline = summary_of(ingested.text.replace("\n", " "), 180) or "(no text provided)"
    return f"Read a {channel}{where}. Signal: {error_types}. Report: {headline}"
