"""Patch authoring skill.

Two producers feed the same candidate pool:

* the **deterministic engine** (``core.fixes``) — grounded, explainable, offline;
* an **optional code model** — used when a capable local model is available, and
  only ever accepted if the result parses, stays inside the repository, and
  survives the same validation the deterministic candidates do.

A model suggestion that fails validation is discarded with a note, never applied.
"""

from __future__ import annotations

import json
import re

from .base import Evidence, Hypothesis, Skill, SkillContext, SkillResult
from ..core.fixes import (
    FixCandidate,
    generate_candidates,
    strategies_for_category,
    STRATEGY_DESCRIPTIONS,
)
from ..models.registry import CAP_CODE, TASK_PATCH
from ..security.secrets import is_sensitive_path
from ..util import excerpt

MAX_FILE_CHARS = 24_000

SYSTEM_PROMPT = (
    "You are FixPilot, a surgical code-fixing engine embedded in a developer's toolchain. "
    "You receive a verified root cause and the exact source of the failing region. "
    "You reply with JSON only — no prose, no markdown fences. "
    "Apply the smallest change that fixes the root cause. Preserve the existing style, imports and public API. "
    "Never add dependencies, never touch secrets or configuration files, never reformat unrelated code."
)

OUTPUT_CONTRACT = """Reply with this JSON object and nothing else:
{
  "root_cause": "<one sentence>",
  "confidence": 0.0,
  "files": [
    {
      "path": "<repo-relative path, must be one of the files shown>",
      "reason": "<why this change fixes the root cause>",
      "edits": [
        {"find": "<exact existing snippet, copied verbatim>", "replace": "<new snippet>"}
      ]
    }
  ]
}
Rules: `find` must appear exactly once in the file; keep edits minimal; use an empty `edits` list if you are not confident."""


class PatchAuthor(Skill):
    name = "patch_author"
    title = "Patch authoring"
    description = "Produces safe candidate patches from deterministic strategies and, when available, a code model."
    priority = 90
    cost = "normal"
    always = True
    post_ranking = True

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        hypothesis = self._top_hypothesis(ctx)
        if hypothesis is None:
            result.notes.append("no hypothesis available to patch")
            return result

        candidates: list[FixCandidate] = []
        for target in self._targets(ctx, hypothesis):
            strategies = []
            if target.strategy:
                strategies.append(target.strategy)
            strategies.extend(strategies_for_category(target.category))
            candidates.extend(
                generate_candidates(
                    strategy=strategies[0] if strategies else "",
                    extra_strategies=strategies[1:] if strategies else [],
                    path=target.file,
                    lineno=target.lineno or 1,
                    symbol=target.symbol,
                    reason=target.explanation or target.cause,
                    index=ctx.index,
                    history=ctx.history,
                )
            )

        # Several hypotheses can describe the same edit; queue each edit once.
        unique: dict[tuple[str, str], FixCandidate] = {}
        for candidate in candidates:
            key = (candidate.path, candidate.new_content)
            existing = unique.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                unique[key] = candidate
        candidates = sorted(unique.values(), key=lambda c: -c.confidence)
        deterministic = len(candidates)
        model_candidates, model_note = self._model_candidates(ctx, hypothesis)
        candidates.extend(model_candidates)
        if model_note:
            result.notes.append(model_note)

        ranked = sorted(candidates, key=lambda c: -c.confidence)
        ctx.scratch["patch_candidates"] = ranked
        if ranked:
            evidence = ctx.evidence(
                kind="source",
                claim=f"{len(ranked)} candidate patch(es) drafted ({deterministic} deterministic, {len(model_candidates)} model-assisted)",
                detail="; ".join(f"{c.strategy} @ {c.path}:{c.lineno}" for c in ranked[:4]),
                path=ranked[0].path,
                lineno=ranked[0].lineno,
                confidence=ranked[0].confidence,
                source="patch engine",
                skill=self.name,
            )
            result.evidence.append(evidence)
        else:
            result.notes.append(
                "no safe automated transform applies to this hypothesis — FixPilot will explain the cause and stop rather than guess"
            )
        result.outputs = {
            "candidates": [c.to_dict() for c in ranked],
            "deterministic": deterministic,
            "model_assisted": len(model_candidates),
        }
        return result

    # -- internals -----------------------------------------------------
    @staticmethod
    def _targets(ctx: SkillContext, hypothesis: Hypothesis) -> list[Hypothesis]:
        """The top hypothesis first, then other plausible fix sites.

        A single root cause can be ranked below the top one (for example when the
        top entry points at a test harness), and the verifier decides which patch
        actually holds — so offering it up to three grounded targets is strictly
        better than betting everything on one.
        """
        ranked = ctx.scratch.get("ranked_hypotheses") or []
        ordered: list[Hypothesis] = [hypothesis]
        for item in ranked[1:]:
            candidate = item if isinstance(item, Hypothesis) else None
            if candidate is None:
                continue
            if not candidate.file or candidate.file not in ctx.index.files:
                continue
            if candidate.file in {existing.file for existing in ordered} and candidate.strategy == hypothesis.strategy:
                continue
            ordered.append(candidate)
            if len(ordered) >= 3:
                break
        return ordered

    @staticmethod
    def _top_hypothesis(ctx: SkillContext) -> Hypothesis | None:
        ranked = ctx.scratch.get("ranked_hypotheses") or []
        if not ranked:
            return None
        top = ranked[0]
        if isinstance(top, dict):
            return Hypothesis(**{k: v for k, v in top.items() if k in Hypothesis.__slots__})
        return top

    def _model_candidates(self, ctx: SkillContext, hypothesis: Hypothesis) -> tuple[list[FixCandidate], str]:
        if not hypothesis.file:
            return [], ""
        decision = ctx.router.route(TASK_PATCH, context_tokens=8000)
        profile = ctx.router.registry.get(decision.profile_key)
        if profile is None or not profile.supports(CAP_CODE):
            return [], "no code-capable model reachable; authored deterministically"
        prompt = self._build_prompt(ctx, hypothesis)
        completion, used = ctx.router.call(
            TASK_PATCH,
            prompt,
            system=SYSTEM_PROMPT,
            max_tokens=1600,
            temperature=0.05,
        )
        if not completion.ok:
            return [], f"code model unavailable ({completion.error or 'empty response'}); authored deterministically"
        ctx.log(
            "model.route",
            {
                "task": TASK_PATCH,
                "model": used.profile_key if used else decision.profile_key,
                "privacy": used.privacy if used else decision.privacy,
                "latency_ms": completion.latency_ms,
                "purpose": "patch authoring",
            },
        )
        payload = _extract_json(completion.text)
        if not payload:
            return [], "model reply was not valid JSON — discarded"
        candidates: list[FixCandidate] = []
        for file_entry in payload.get("files", [])[:4]:
            path = str(file_entry.get("path", "")).strip().lstrip("./")
            if path not in ctx.index.files:
                continue
            sensitive, label = is_sensitive_path(path)
            if sensitive:
                continue
            source = "\n".join(ctx.index.source_lines(path))
            if not source.strip():
                continue
            updated = source
            edits_applied = 0
            for edit in file_entry.get("edits", [])[:6]:
                find = str(edit.get("find", ""))
                replace = str(edit.get("replace", ""))
                if not find or updated.count(find) != 1:
                    continue
                updated = updated.replace(find, replace, 1)
                edits_applied += 1
            if edits_applied == 0 or updated == source:
                continue
            candidates.append(
                FixCandidate(
                    strategy=f"model:{ctx.router.registry.get(decision.profile_key).model}",
                    path=path,
                    old_content=source,
                    new_content=updated,
                    reason=str(file_entry.get("reason") or payload.get("root_cause") or "model-authored change"),
                    confidence=min(0.72, 0.4 + 0.05 * edits_applied + float(payload.get("confidence") or 0.4) * 0.25),
                    lineno=hypothesis.lineno,
                    symbol=hypothesis.symbol,
                    behaviour_change=True,
                    verification_hint="model-authored patch: tests decide whether it is kept",
                    notes=[f"authored by {decision.profile_key} ({decision.privacy})"],
                )
            )
        note = f"model {decision.profile_key} proposed {len(candidates)} applicable patch(es)"
        return candidates, note

    def _build_prompt(self, ctx: SkillContext, hypothesis: Hypothesis) -> str:
        path = hypothesis.file
        lines = ctx.index.source_lines(path)
        symbol = ctx.symbol_at(path, hypothesis.lineno or 1)
        if symbol is not None:
            body = "\n".join(lines[symbol.lineno - 1 : symbol.end_lineno])
            window = f"{path} (symbol {symbol.name}, lines {symbol.lineno}-{symbol.end_lineno}):\n{body}"
        else:
            start = max(0, (hypothesis.lineno or 1) - 40)
            end = min(len(lines), (hypothesis.lineno or 1) + 40)
            window = f"{path} (lines {start + 1}-{end}):\n" + "\n".join(lines[start:end])
        window = window[:MAX_FILE_CHARS]

        evidence_lines = []
        for item in ctx.scratch.get("evidence", [])[:8]:
            payload = item.to_dict() if hasattr(item, "to_dict") else item
            where = f"{payload.get('path', '')}:{payload.get('lineno', 0)}".strip(":")
            evidence_lines.append(f"- [{payload.get('kind')}] {payload.get('claim', '')} {('(' + where + ')') if where else ''}")

        project = ctx.memory.load().brief(limit=8)
        relevant_tests = ctx.graph.tests_for(path)[:4]
        test_lines = [f"- {test}" for test in relevant_tests] or ["- (no test file found for this module)"]
        return "\n".join(
            [
                "## Project memory",
                project,
                "",
                "## Bug report (raw)",
                excerpt(ctx.text, 1800),
                "",
                "## Ranked root cause",
                f"- cause: {hypothesis.cause}",
                f"- category: {hypothesis.category}",
                f"- strategy under consideration: {hypothesis.strategy} "
                f"({STRATEGY_DESCRIPTIONS.get(hypothesis.strategy, 'custom')})",
                f"- explanation: {hypothesis.explanation}",
                "",
                "## Evidence",
                *evidence_lines,
                "",
                "## Relevant tests",
                *test_lines,
                "",
                "## Source under consideration",
                window,
                "",
                "## Rules for your patch",
                "- Minimal diff: change as few lines as possible.",
                "- Do not add third-party imports or change dependency declarations.",
                "- Do not modify secrets, environment files, CI config or lockfiles.",
                "- Keep the change inside the file(s) shown above.",
                "- If the evidence is insufficient, return an empty files list.",
                "",
                OUTPUT_CONTRACT,
            ]
        )


def _extract_json(text: str) -> dict | None:
    """Tolerant JSON extraction (models like to wrap payloads in prose)."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
