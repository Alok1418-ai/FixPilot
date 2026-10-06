/* FixPilot PWA — thin, dependency-free client for the local agent. */
(() => {
  "use strict";

  const TOKEN = document.body.dataset.token || new URLSearchParams(location.search).get("token") || "";
  const $ = (id) => document.getElementById(id);
  const state = { sessionId: null, session: null, overview: null, replay: null, timeline: [] };

  /* ------------------------------------------------------------------ */
  /* transport                                                          */
  /* ------------------------------------------------------------------ */
  async function api(method, path, body) {
    const headers = { "X-FixPilot-Token": TOKEN };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let payload = {};
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = { raw: text }; }
    if (!response.ok) {
      const message = payload.error || payload.detail || `HTTP ${response.status}`;
      if (response.status === 401 && !path.startsWith("/api/auth/")) {
        toast("Your session expired — returning to sign-in…", 1800);
        setTimeout(() => location.reload(), 1200);
      }
      throw Object.assign(new Error(message), { status: response.status, payload });
    }
    return payload;
  }

  function toast(message, ms = 2600) {
    const el = $("toast");
    el.textContent = message;
    el.classList.remove("hidden");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add("hidden"), ms);
  }

  function busy(text) {
    $("overlay-text").textContent = text || "working…";
    $("overlay").classList.remove("hidden");
  }
  function idle() { $("overlay").classList.add("hidden"); }

  /* ------------------------------------------------------------------ */
  /* tiny markdown renderer (bold, code, lists, headings, quotes)       */
  /* ------------------------------------------------------------------ */
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function inline(text) {
    return escapeHtml(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])_([^_]+)_(?=[\s).,:]|$)/g, "$1<em>$2</em>");
  }
  function md(source) {
    const lines = String(source || "").split("\n");
    let html = "";
    let inList = false;
    let inCode = false;
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (line.trim().startsWith("```")) {
        html += inCode ? "</pre>" : "<pre class='terminal'>";
        inCode = !inCode;
        continue;
      }
      if (inCode) { html += escapeHtml(line) + "\n"; continue; }
      const listMatch = /^\s*[-*]\s+(.*)$/.exec(line);
      if (listMatch) {
        if (!inList) { html += "<ul>"; inList = true; }
        html += `<li>${inline(listMatch[1])}</li>`;
        continue;
      }
      if (inList) { html += "</ul>"; inList = false; }
      if (/^#{1,6}\s/.test(line)) { html += `<h3>${inline(line.replace(/^#+\s*/, ""))}</h3>`; continue; }
      if (/^>\s?/.test(line)) { html += `<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`; continue; }
      if (!line.trim()) { continue; }
      html += `<p>${inline(line)}</p>`;
    }
    if (inList) html += "</ul>";
    if (inCode) html += "</pre>";
    return html;
  }

  function renderDiff(text) {
    return String(text || "")
      .split("\n")
      .map((line) => {
        const safe = escapeHtml(line);
        if (line.startsWith("+") && !line.startsWith("+++")) return `<span class="diff-add">${safe}</span>`;
        if (line.startsWith("-") && !line.startsWith("---")) return `<span class="diff-del">${safe}</span>`;
        return safe;
      })
      .join("\n");
  }

  /* ------------------------------------------------------------------ */
  /* views                                                              */
  /* ------------------------------------------------------------------ */
  /* Views reached from the "More" hub keep that tab highlighted. */
  const SUB_VIEWS = { console: "more", safety: "more", system: "more" };

  function showView(name) {
    document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
    $(`view-${name}`).classList.add("active");
    const tabName = SUB_VIEWS[name] || name;
    document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === tabName));
    window.scrollTo({ top: 0 });
    if (name === "home") loadDashboard();
    if (name === "sessions") loadSessions();
    if (name === "repo") loadRepo();
    if (name === "safety") loadSafety();
    if (name === "system") loadSystem();
    if (name === "more") loadAccount();
  }

  function renderTimeline(items, completedCount) {
    const list = $("timeline");
    list.innerHTML = "";
    (items || []).forEach((item, index) => {
      const li = document.createElement("li");
      li.className = index < (completedCount ?? items.length) ? "done" : "";
      li.innerHTML = `<strong>${escapeHtml(item.label)}</strong>
        <span class="detail">${escapeHtml(item.detail || "")}</span>
        <span class="when">${escapeHtml((item.ts || "").replace("T", " ").replace("Z", ""))}</span>`;
      list.appendChild(li);
    });
    $("pipeline").classList.toggle("hidden", !items || !items.length);
  }

  function renderSession(payload) {
    const session = payload.session || payload;
    const explain = payload.explain || {};
    state.session = session;
    state.sessionId = session.id;

    $("pipeline").classList.remove("hidden");
    renderSessionStatus(session);
    if (payload.timeline) state.timeline = payload.timeline;
    renderTimeline(state.timeline, state.timeline.length);

    if (session.understanding && session.understanding.summary) {
      $("understanding").classList.remove("hidden");
      $("understanding-body").innerHTML = md(explain.understanding || session.understanding.summary);
    }
    if (session.hypotheses && session.hypotheses.length) {
      $("cause").classList.remove("hidden");
      $("cause-body").innerHTML = md(explain.cause || "");
      $("evidence-count").textContent = (session.evidence || []).length;
      $("evidence-list").innerHTML = (session.evidence || [])
        .slice(0, 12)
        .map((item) => `<li><span class="kind">${escapeHtml(item.kind)}</span>
            ${inline(item.claim)}
            ${item.path ? `<span class="where">${escapeHtml(item.path)}:${item.lineno || ""}</span>` : ""}</li>`)
        .join("");
    }
    const patchText = (session.patch && session.patch.text) || "";
    if (patchText) {
      $("patch").classList.remove("hidden");
      $("patch-body").innerHTML = md(explain.patch || session.patch.summary || "");
      $("patch-diff").innerHTML = renderDiff(patchText);
      const risk = (session.patch.risk && session.patch.risk.level) || "";
      const badge = $("risk-badge");
      badge.textContent = risk ? `${risk} risk` : "";
      badge.className = `pill ${risk === "low" ? "ok" : risk === "medium" ? "warn" : risk ? "bad" : ""}`;

      const stats = session.patch.stats || {};
      $("patch-stats").innerHTML = [
        `<span class="pill">${stats.files || 0} file${stats.files === 1 ? "" : "s"}</span>`,
        `<span class="pill ok">+${stats.additions || 0}</span>`,
        `<span class="pill bad">&minus;${stats.deletions || 0}</span>`,
        stats.bytes ? `<span class="pill">${stats.bytes} B</span>` : "",
        (session.patch.strategies || []).map((s) => `<span class="pill">${escapeHtml(s)}</span>`).join(""),
      ].join("");

      const riskInfo = session.patch.risk || {};
      const blast = riskInfo.blast_radius || {};
      const changed = (blast.changed || []).map((p) => `\`${p}\``).join(", ");
      const dependents = (blast.dependent_files || []).map((p) => `\`${p}\``).join(", ");
      const noteLines = (riskInfo.notes || []).map((n) => `- ${n}`).join("\n");
      const hasBlast = Boolean(changed) || Boolean(noteLines);
      $("blast-radius-box").classList.toggle("hidden", !hasBlast);
      if (hasBlast) {
        $("blast-radius-body").innerHTML = md(
          `- **Changed:** ${changed || "n/a"}\n` +
          `- **Dependent files:** ${dependents || "none detected"}\n` +
          `- **Behaviour change:** ${riskInfo.behaviour_change ? "yes — read the note before approving" : "no"}\n` +
          (noteLines ? "\n" + noteLines : "")
        );
      }
    } else if (session.patch && session.patch.summary) {
      $("patch").classList.remove("hidden");
      $("patch-body").innerHTML = md(explain.patch || session.patch.summary);
      $("patch-diff").innerHTML = "";
      $("patch-stats").innerHTML = "";
      $("blast-radius-box").classList.add("hidden");
    }
    const verification = session.verification || {};
    if (verification.status) {
      $("verification").classList.remove("hidden");
      $("verification-body").innerHTML = md(explain.verification || "");
      const runs = verification.runs || [];
      const last = runs[runs.length - 1] || {};
      $("test-output").textContent =
        `$ ${last.command || ""}\nexit ${last.exit_code ?? "-"} in ${last.duration_ms ?? 0}ms\n\n` +
        `${last.stdout_tail || ""}\n${last.stderr_tail || ""}`.trim();
    }
    const awaiting = session.status === "awaiting_approval" || session.status === "patch_proposed";
    $("btn-approve").classList.toggle("hidden", !awaiting || !patchText);
    $("btn-reject").classList.toggle("hidden", !awaiting);
    $("btn-rollback").classList.toggle("hidden", !(session.rollback && session.rollback.files && session.rollback.files.length));
    $("btn-report").classList.toggle("hidden", !state.sessionId);
    $("btn-refine").classList.toggle("hidden", !(session.patch && session.patch.files && session.patch.files.length));
    const isTerminal = Boolean(session.terminal);
    const hasPatchFiles = Boolean(session.patch && session.patch.files && session.patch.files.length);
    $("btn-apply").classList.toggle("hidden", !state.sessionId || isTerminal || !hasPatchFiles);
    $("btn-cancel").classList.toggle("hidden", !state.sessionId || isTerminal);
  }

  function renderSessionStatus(session) {
    $("pill-link").textContent = session ? session.status_label || session.status : "idle";
    $("pill-link").className = `pill ${
      session && session.status === "verified" ? "ok"
        : session && ["failed", "rolled_back", "rejected"].includes(session.status) ? "bad"
        : session && session.status === "awaiting_approval" ? "warn" : ""
    }`;
  }

  function renderReplay(payload) {
    state.replay = payload;
    const events = payload.events || [];
    $("replay").classList.remove("hidden");
    const range = $("replay-range");
    range.max = String(Math.max(0, events.length - 1));
    range.value = String(Math.max(0, events.length - 1));
    const paint = () => {
      const upto = Number(range.value);
      const visible = events.slice(0, upto + 1);
      const last = visible[visible.length - 1] || {};
      $("replay-body").innerHTML =
        md(`**Step ${upto + 1} of ${events.length}** — \`${last.kind || ""}\`\n\n` +
           (last.payload ? "```\n" + JSON.stringify(last.payload, null, 1).slice(0, 1600) + "\n```" : ""));
    };
    range.oninput = paint;
    paint();
  }

  /* ------------------------------------------------------------------ */
  /* actions                                                            */
  /* ------------------------------------------------------------------ */
  async function investigate() {
    const text = $("report-text").value.trim();
    const attachments = state.attachments || [];
    if (!text && !attachments.length) { toast("Describe the bug or attach a screenshot first."); return; }
    busy("Investigating — reading the traceback, the repo and the git history…");
    try {
      const payload = await api("POST", "/api/sessions", { text, attachments });
      state.attachments = [];
      $("attachments").innerHTML = "";
      renderSession(payload);
      const top = (payload.session.hypotheses || [])[0];
      if (top) toast(`Root cause: ${top.cause.slice(0, 90)}`);
      else toast("No confident root cause — see the notes.");
      toast("Tip: approve the patch to run your tests.", 3200);
    } catch (error) {
      toast(`Failed: ${error.message}`, 4000);
    } finally {
      idle();
    }
  }

  async function previewIntake() {
    const text = $("report-text").value.trim();
    const attachments = state.attachments || [];
    if (!text && !attachments.length) { toast("Describe the bug or attach a screenshot first."); return; }
    busy("Normalising the report…");
    try {
      const payload = await api("POST", "/api/input", { text, attachments });
      const input = payload.input || {};
      const signals = input.signals || [];
      const frames = signals.filter((signal) => signal.kind === "frame");
      $("intake-box").classList.remove("hidden");
      $("intake-body").innerHTML = md(
        `- **Channel:** \`${input.channel || "?"}\` · **language:** \`${input.language || "?"}\` · **intent:** \`${input.hint_intent || "?"}\`\n` +
        `- **Confidence:** ${input.confidence ?? "?"} · **raw length:** ${input.raw_length ?? 0} chars\n` +
        `- **Signals:** ${signals.length} (${frames.length} stack frame${frames.length === 1 ? "" : "s"})\n` +
        (frames.length
          ? "\n**Frames it will chase:**\n" + frames.slice(0, 8).map((frame) =>
              `- \`${String(frame.path || "").split("/").slice(-2).join("/")}:${frame.lineno || "?"}\`` +
              `${frame.function ? ` in \`${frame.function}\`` : ""}${frame.message ? ` — ${frame.message}` : ""}`).join("\n") + "\n"
          : "") +
        ((input.warnings || []).length ? "\n**Warnings:**\n" + input.warnings.map((w) => `- ${w}`).join("\n") : "")
      );
      toast(frames.length ? `Read ${frames.length} stack frame(s).` : "No stack frames found in that report.");
    } catch (error) { toast(error.message, 3000); } finally { idle(); }
  }

  async function approve(approved) {
    if (!state.sessionId) return;
    busy(approved ? "Approving, applying and running your tests…" : "Rejecting…");
    try {
      const payload = await api("POST", `/api/sessions/${state.sessionId}/approve`, { approved, apply: true, note: approved ? "approved from phone" : "rejected from phone" });
      await refreshSession();
      if (approved && payload.verified) toast("✅ Verified — tests pass on the patched tree.");
      else if (approved && payload.rolled_back) toast("⚠️ Tests failed; the patch was rolled back automatically.", 4200);
      else if (approved) toast("Applied, but see the verification notes.", 4000);
      else toast("Rejected — nothing was applied.");
    } catch (error) {
      toast(`Failed: ${error.message}`, 4000);
    } finally {
      idle();
    }
  }

  async function refreshSession() {
    if (!state.sessionId) return;
    const payload = await api("GET", `/api/sessions/${state.sessionId}`);
    renderSession(payload);
    return payload;
  }

  async function rollback() {
    if (!state.sessionId) return;
    busy("Restoring the previous file contents…");
    try {
      const payload = await api("POST", `/api/sessions/${state.sessionId}/rollback`, { note: "rollback from phone" });
      await refreshSession();
      toast(payload.ok ? "↩︎ Rolled back — your tree is as it was." : `Rollback issue: ${payload.error || ""}`);
    } catch (error) {
      toast(`Rollback failed: ${error.message}`, 4000);
    } finally { idle(); }
  }

  async function refine() {
    if (!state.sessionId) return;
    busy("Asking for another candidate patch…");
    try {
      const payload = await api("POST", `/api/sessions/${state.sessionId}/refine`, { instruction: "try a different strategy" });
      await refreshSession();
      toast(payload.candidates_added ? `${payload.candidates_added} new candidate(s) queued — approve to try them.` : "No new candidate available.");
    } catch (error) { toast(error.message, 4000); } finally { idle(); }
  }

  async function applyAgain() {
    if (!state.sessionId) return;
    busy("Applying the patch and running the tests again…");
    try {
      const payload = await api("POST", `/api/sessions/${state.sessionId}/apply`, { auto_rollback: true });
      await refreshSession();
      if (payload.verified) toast("✅ Verified — tests pass on the patched tree.");
      else if (payload.rolled_back) toast("⚠️ Tests failed; rolled back automatically.", 4200);
      else toast(payload.error ? `Not applied: ${payload.error}` : "Applied — see the verification notes.", 4000);
    } catch (error) { toast(`Apply failed: ${error.message}`, 4000); } finally { idle(); }
  }

  async function cancelSession() {
    if (!state.sessionId) return;
    if (!window.confirm("Cancel this session? Nothing is applied to your tree.")) return;
    busy("Cancelling the session…");
    try {
      const payload = await api("POST", `/api/sessions/${state.sessionId}/cancel`, {});
      await refreshSession();
      toast(payload.ok ? "Session cancelled." : "Could not cancel.");
    } catch (error) { toast(`Cancel failed: ${error.message}`, 4000); } finally { idle(); }
  }

  async function reinvestigate() {
    if (!state.sessionId) return;
    busy("Re-running the skills against the current tree…");
    try {
      const payload = await api("POST", `/api/sessions/${state.sessionId}/investigate`, { cost_budget: "deep", propose: true });
      state.timeline = payload.timeline || state.timeline;
      renderSession(payload);
      toast("Investigation refreshed.");
    } catch (error) { toast(`Re-investigate failed: ${error.message}`, 4000); } finally { idle(); }
  }

  async function replay() {
    if (!state.sessionId) return;
    try {
      const payload = await api("GET", `/api/sessions/${state.sessionId}/replay?since_seq=0`);
      renderReplay(payload);
      $("replay").scrollIntoView({ behavior: "smooth" });
    } catch (error) { toast(error.message, 3000); }
  }

  async function loadSessions() {
    try {
      const payload = await api("GET", "/api/sessions?limit=40");
      $("session-list").innerHTML = (payload.sessions || [])
        .map((session) => `<li data-id="${session.id}">
            <div class="title">${escapeHtml(session.title.slice(0, 90))}</div>
            <div class="meta">${escapeHtml(session.status_label)} · ${escapeHtml(session.channel)} · ${escapeHtml((session.updated_at || "").replace("T", " ").replace("Z", ""))}</div>
            ${session.root_cause ? `<div class="meta">↳ ${escapeHtml(session.root_cause.slice(0, 110))}</div>` : ""}
          </li>`)
        .join("") || "<li><div class='meta'>No sessions yet.</div></li>";
      $("session-list").querySelectorAll("li[data-id]").forEach((item) => {
        item.addEventListener("click", async () => {
          const payload = await api("GET", `/api/sessions/${item.dataset.id}`);
          state.timeline = payload.timeline || [];
          renderSession(payload);
          showView("report");
          window.scrollTo({ top: 0, behavior: "smooth" });
        });
      });
    } catch (error) { toast(error.message, 3000); }
  }

  async function runCommand() {
    const command = $("command-input").value.trim();
    if (!command) return;
    busy("Checking policy and running…");
    try {
      const payload = await api("POST", "/api/command", { command, approved: true });
      const verdict = payload.verdict || {};
      const box = $("verdict");
      box.classList.remove("hidden");
      box.className = `verdict ${verdict.decision === "deny" ? "deny" : verdict.decision === "allow" ? "allow" : "confirm"}`;
      box.innerHTML = md(`**${verdict.decision}** · ${verdict.category} · risk ${verdict.risk}\n\n` +
        (verdict.reasons || []).map((reason) => `- ${reason}`).join("\n"));
      const result = payload.result || {};
      $("console-output").textContent =
        `$ ${command}\nexit ${result.exit_code} in ${result.duration_ms || 0}ms\n\n${result.stdout || ""}${result.stderr ? "\n" + result.stderr : ""}`;
      if (result.error) $("console-output").textContent = `blocked: ${result.error}\n\n` + $("console-output").textContent;
    } catch (error) {
      const payload = error.payload || {};
      const box = $("verdict");
      box.classList.remove("hidden");
      box.className = "verdict deny";
      box.innerHTML = md(`**blocked**\n\n` + ((payload.verdict && payload.verdict.reasons) || [error.message]).map((r) => `- ${r}`).join("\n"));
      $("console-output").textContent = "";
    } finally { idle(); }
  }

  async function ask() {
    const question = $("ask-input").value.trim();
    if (!question) return;
    busy("Searching the codebase…");
    try {
      const payload = await api("POST", "/api/ask", { question });
      $("ask-body").innerHTML = md(payload.answer) +
        ((payload.citations || []).length
          ? "<details open><summary>Sources</summary><ul>" +
            payload.citations.map((c) => `<li><code>${escapeHtml(c.path)}${c.lineno ? ":" + c.lineno : ""}</code> ${escapeHtml((c.snippet || "").slice(0, 80))}</li>`).join("") +
            "</ul></details>"
          : "");
    } catch (error) { toast(error.message, 3000); } finally { idle(); }
  }

  async function loadSystem() {
    try {
      const [overview, models, skills, memory] = await Promise.all([
        api("GET", "/api/overview"),
        api("GET", "/api/models"),
        api("GET", "/api/skills"),
        api("GET", "/api/memory"),
      ]);
      state.overview = overview;
      const stats = overview.repo.stats || {};
      $("repo-line").textContent = `${overview.repo.root.split("/").slice(-1)[0]} · ${stats.files || 0} files · ${stats.symbols || 0} symbols`;
      $("system-body").innerHTML = md(
        `**Repo:** \`${overview.repo.root}\`\n\n` +
        `- branch \`${overview.repo.git_branch || "n/a"}\` @ \`${(overview.repo.git_head || "").slice(0, 10)}\`${overview.repo.dirty ? " *(dirty working tree)*" : ""}\n` +
        `- languages: ${Object.entries(stats.languages || {}).map(([k, v]) => `${k} (${v})`).join(", ") || "n/a"}\n` +
        `- tests: ${overview.repo.test_command && overview.repo.test_command.length ? "`" + overview.repo.test_command.join(" ") + "`" : "none detected"}\n` +
        `- memory: ${overview.memory.facts || 0} facts, ${overview.lessons.total || 0} lessons (${overview.lessons.verified || 0} verified)\n` +
        `- sandbox: timeouts ${overview.sandbox.limits.timeout_seconds}s, network ${overview.sandbox.limits.allow_network ? "enabled" : "blocked"}, ${overview.sandbox.commands_run} commands run\n` +
        `- approval gate: ${overview.settings.require_approval ? "required" : "disabled"}`
      );
      $("pill-model").textContent = models.strategy;
      $("pill-model").className = `pill ${models.strategy === "local-first" ? "ok" : "warn"}`;
      $("models-body").innerHTML = md(
        `Strategy: **${models.strategy}**${models.cloud_allowed ? " (cloud escalation available)" : " (on-device only)"}\n\n` +
        (models.profiles || []).map((profile) => {
          const status = profile.available === true ? "✅" : profile.available === false ? "⛔" : "❔";
          const usage = profile.usage && profile.usage.calls ? ` · ${profile.usage.calls} calls, ${profile.usage.avg_latency_ms}ms avg` : "";
          return `- ${status} \`${profile.model}\` — ${profile.label}${profile.local ? " *(local)*" : " *(cloud)*"}${usage}`;
        }).join("\n") +
        "\n\n**Task routing:**\n" + Object.entries(models.tasks || {}).map(([task, info]) => `- \`${task}\` needs ${info.requires.join("/")}`).join("\n")
      );
      $("skills-body").innerHTML = md((skills.skills || []).map((skill) =>
        `- **${skill.title}** (\`${skill.name}\`, priority ${skill.priority}, ${skill.cost})\n  - ${skill.description}`).join("\n"));
      $("memory-body").innerHTML = md(
        `**Project:** ${memory.memory.name || "unnamed"}\n\n` +
        (memory.memory.conventions || []).map((c) => `- convention: \`${c}\``).join("\n") +
        "\n\n**Recent facts:**\n" + (memory.facts || []).slice(-8).map((f) => `- [${f.kind}] ${f.statement} _(conf ${f.confidence})_`).join("\n")
      );
      await loadOfficeKit();
    } catch (error) { toast(error.message, 3000); }
  }

  /* ------------------------------------------------------------------ */
  /* attachments + voice                                                */
  /* ------------------------------------------------------------------ */
  state.attachments = [];
  $("file-input").addEventListener("change", async (event) => {
    for (const file of Array.from(event.target.files || [])) {
      const dataUrl = await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.readAsDataURL(file);
      });
      state.attachments.push({ filename: file.name, data: dataUrl, ocr_text: "" });
    }
    $("attachments").innerHTML = state.attachments
      .map((a, index) => `<span class="pill">📎 ${escapeHtml(a.filename)} <button data-drop="${index}" class="btn small ghost">✕</button></span>`)
      .join("");
    $("attachments").querySelectorAll("[data-drop]").forEach((button) => {
      button.addEventListener("click", () => {
        state.attachments.splice(Number(button.dataset.drop), 1);
        $("file-input").dispatchEvent(new Event("change"));
      });
    });
    event.target.value = "";
  });

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    $("btn-voice").disabled = true;
    $("voice-status").textContent = "Voice input needs Chrome or Safari on this device.";
  } else {
    $("btn-voice").addEventListener("click", () => {
      const recognition = new SpeechRecognition();
      recognition.lang = navigator.language || "en-US";
      recognition.continuous = false;
      recognition.interimResults = true;
      $("voice-status").textContent = "Listening…";
      let finalText = "";
      recognition.onresult = (event) => {
        let interim = "";
        for (const result of event.results) {
          if (result.isFinal) finalText += result[0].transcript;
          else interim += result[0].transcript;
        }
        $("voice-status").textContent = `Heard: ${(finalText + interim).slice(0, 120)}`;
      };
      recognition.onerror = (event) => { $("voice-status").textContent = `Voice error: ${event.error}`; };
      recognition.onend = () => {
        const text = (finalText || "").trim();
        if (text) {
          $("report-text").value = ($("report-text").value ? $("report-text").value + "\n" : "") + text;
          $("voice-status").textContent = "Transcript added — FixPilot repairs spoken file paths and identifiers automatically.";
        } else if ($("voice-status").textContent === "Listening…") {
          $("voice-status").textContent = "Nothing captured.";
        }
      };
      recognition.start();
    });
  }

  $("btn-sample").addEventListener("click", async () => {
    try {
      const payload = await api("GET", "/api/samples");
      const samples = payload.samples || [];
      if (!samples.length) { toast("No sample reports bundled."); return; }
      const index = (state.sampleIndex = ((state.sampleIndex || -1) + 1) % samples.length);
      const sample = samples[index];
      $("report-text").value = sample.text;
      toast(`Loaded: ${sample.label}`);
    } catch (error) { toast(error.message, 3000); }
  });

  /* ------------------------------------------------------------------ */
  /* repo: tree · file · search · worktree diff                         */
  /* ------------------------------------------------------------------ */
  state.tree = [];

  function statGrid(items) {
    return (items || []).map((item) =>
      `<div class="stat ${item[2] || ""}"><span class="v">${escapeHtml(String(item[1]))}</span>` +
      `<span class="k">${escapeHtml(String(item[0]))}</span></div>`).join("");
  }

  function renderTree() {
    const filter = ($("tree-filter").value || "").trim().toLowerCase();
    const rows = state.tree.filter((file) => !filter || file.path.toLowerCase().includes(filter));
    $("file-count").textContent = filter ? `${rows.length}/${state.tree.length}` : String(state.tree.length);
    $("file-tree").innerHTML = rows.slice(0, 400).map((file) =>
      `<li data-path="${escapeHtml(file.path)}">
         <span class="fp">${escapeHtml(file.path)}</span>
         <span class="fmeta">${escapeHtml(file.language)} · ${file.lines}L · ${file.symbols} sym${file.tests ? " · test" : ""}</span>
       </li>`).join("") || "<li class=\"empty\">No files match.</li>";
    $("file-tree").querySelectorAll("li[data-path]").forEach((li) =>
      li.addEventListener("click", () => openFile(li.dataset.path)));
  }

  async function loadRepo() {
    try {
      const [tree, diff] = await Promise.all([
        api("GET", "/api/tree?limit=800"),
        api("GET", "/api/diff/worktree"),
      ]);
      state.tree = tree.files || [];
      renderTree();
      $("worktree-dirty").textContent = diff.dirty ? "dirty — uncommitted changes" : "clean working tree";
      $("worktree-dirty").className = `pill ${diff.dirty ? "warn" : "ok"}`;
      $("worktree-diff").classList.toggle("hidden", !diff.diff);
      $("worktree-diff").innerHTML = renderDiff(diff.diff);
    } catch (error) { toast(error.message, 3000); }
  }

  async function openFile(path) {
    busy(`Reading ${path}…`);
    try {
      const file = await api("GET", `/api/file?path=${encodeURIComponent(path)}`);
      $("file-viewer").classList.remove("hidden");
      $("file-name").textContent = file.path;
      $("file-meta").innerHTML = [
        `<span class="pill">${file.bytes} B</span>`,
        file.secrets_redacted ? `<span class="pill warn">${file.secrets_redacted} secret(s) redacted</span>` : "",
      ].join("");
      $("file-body").textContent = file.text;
      $("file-viewer").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) { toast(error.message, 3000); } finally { idle(); }
  }

  async function searchCode() {
    const q = ($("search-input").value || "").trim();
    if (!q) { toast("Type something to search for."); return; }
    busy(`Searching for “${q}”…`);
    try {
      const res = await api("GET", `/api/search?q=${encodeURIComponent(q)}&limit=25`);
      const symbols = res.symbols || [];
      const code = res.code || [];
      const paths = res.files || [];
      const box = $("search-results");
      box.classList.remove("hidden");
      box.innerHTML =
        (symbols.length ? `<div class="card-title">Symbols (${symbols.length})</div><ul class="hits">` +
          symbols.map((s) => `<li data-path="${escapeHtml(s.path)}">
              <span class="kind">${escapeHtml(s.kind || "")}</span> <code>${escapeHtml(s.name || "")}</code>
              <span class="where">${escapeHtml(s.path)}:${s.lineno || ""}</span>
              ${s.signature ? `<pre class="snippet">${escapeHtml(s.signature)}</pre>` : ""}
            </li>`).join("") + "</ul>" : "") +
        (code.length ? `<div class="card-title">Code (${code.length})</div><ul class="hits">` +
          code.map((c) => `<li data-path="${escapeHtml(c.path)}">
              <span class="where">${escapeHtml(c.path)}:${c.lineno || ""}</span>
              <pre class="snippet">${escapeHtml(String(c.line || "").trim())}</pre>
            </li>`).join("") + "</ul>" : "") +
        (paths.length ? `<div class="card-title">Paths (${paths.length})</div><ul class="hits">` +
          paths.map((p) => `<li data-path="${escapeHtml(p)}"><span class="where">📄 ${escapeHtml(p)}</span></li>`).join("") +
          "</ul>" : "") +
        (!symbols.length && !code.length && !paths.length ? "<p class=\"hint\">No matches.</p>" : "");
      box.querySelectorAll("li[data-path]").forEach((li) =>
        li.addEventListener("click", () => openFile(li.dataset.path)));
    } catch (error) { toast(error.message, 3000); } finally { idle(); }
  }

  /* ------------------------------------------------------------------ */
  /* safety: sandbox · audit log · lessons                              */
  /* ------------------------------------------------------------------ */
  async function loadSandbox() {
    const sandbox = await api("GET", "/api/sandbox");
    const stats = sandbox.stats || {};
    const limits = stats.limits || {};
    const policy = sandbox.policy || {};
    const verdicts = Object.entries(stats.by_decision || {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "no commands yet";
    $("sandbox-stats").innerHTML = statGrid([
      ["commands run", stats.commands_run ?? 0],
      ["policy rules", policy.rules ?? 0],
      ["categories", (policy.categories || []).length],
      ["timeout", `${limits.timeout_seconds ?? "?"}s`],
    ]);
    $("sandbox-body").innerHTML = md(
      `- **Verdicts so far:** ${verdicts}\n` +
      `- **Network:** ${limits.allow_network ? "enabled ⚠️" : "blocked"} · **dependency install:** ${limits.allow_dependency_install ? "enabled ⚠️" : "blocked"}\n` +
      `- **Quick timeout:** ${limits.quick_timeout_seconds}s · **output cap:** ${Math.round((limits.max_output_bytes || 0) / 1024)} KB\n` +
      `- **Patch caps:** ${Math.round((limits.max_patch_bytes || 0) / 1024)} KB · ${limits.max_patch_files} files\n` +
      `- **Extra allowlist:** ${(policy.extra_allow || []).map((a) => `\`${a}\``).join(", ") || "empty"}`
    );
  }

  async function loadAudit() {
    const audit = await api("GET", "/api/audit?limit=100");
    const entries = audit.entries || [];
    $("audit-count").textContent = String(entries.length);
    $("audit-list").innerHTML = entries.map((entry) => {
      const detail = Object.entries(entry)
        .filter(([key]) => key !== "ts" && key !== "kind")
        .map(([key, value]) =>
          `<span class="kv"><b>${escapeHtml(key)}</b> ${escapeHtml(typeof value === "object" ? JSON.stringify(value) : String(value))}</span>`)
        .join("");
      return `<li><span class="kind">${escapeHtml(entry.kind || "")}</span>` +
        `<span class="when">${escapeHtml((entry.ts || "").replace("T", " ").replace("Z", ""))}</span>` +
        `<div class="kvrow">${detail}</div></li>`;
    }).join("") || "<li class=\"empty\">Nothing recorded yet.</li>";
  }

  async function loadLessons() {
    const lessons = await api("GET", "/api/lessons");
    $("lesson-stats").innerHTML = statGrid([
      ["total", lessons.total ?? 0],
      ["verified", lessons.verified ?? 0, "ok"],
      ["failed", lessons.failed ?? 0, "bad"],
      ["reused", lessons.reused ?? 0],
    ]);
    $("lesson-list").innerHTML = (lessons.recent || []).map((lesson) =>
      `<li>
         <div class="row between">
           <span class="pill ${lesson.outcome === "verified" ? "ok" : "bad"}">${escapeHtml(lesson.outcome || "?")}</span>
           <span class="where">conf ${lesson.confidence ?? "?"} · reused ${lesson.reuse_count ?? 0}</span>
         </div>
         <div class="cause">${escapeHtml(lesson.root_cause || "")}</div>
         <div class="meta">${escapeHtml(lesson.strategy || "")} · ${(lesson.files || []).map(escapeHtml).join(", ")}</div>
       </li>`).join("") || "<li class=\"empty\">No lessons yet — verified fixes are recorded here.</li>";
  }

  async function loadSafety() {
    busy("Reading the safety state…");
    try {
      await Promise.all([loadSandbox(), loadAudit(), loadLessons()]);
    } catch (error) { toast(error.message, 3000); } finally { idle(); }
  }

  /* ------------------------------------------------------------------ */
  /* iQOO Office Kit: pairing + job queue                               */
  /* ------------------------------------------------------------------ */
  function renderOfficeKit(data) {
    $("officekit-mode").textContent = data.mode || "?";
    $("officekit-mode").className = `pill ${data.mode === "direct" ? "ok" : data.mode === "relay" ? "warn" : ""}`;
    $("officekit-body").innerHTML = md(
      `Mode: **${data.mode}** · devices online: ${data.online_devices}\n\n` +
      (((data.devices || []).map((device) =>
        `- ${device.status === "online" ? "🟢" : "⚪️"} ${escapeHtml(device.name)} (\`${device.status}\`) — ${(device.capabilities || []).join(", ")}`).join("\n")) ||
        "- no devices paired") +
      "\n\n" + (data.how_it_works || []).map((line) => `- ${line}`).join("\n")
    );
    const kinds = $("job-kind");
    if (!kinds.options.length) {
      kinds.innerHTML = (data.job_kinds || [])
        .map((kind) => `<option value="${escapeHtml(kind)}">${escapeHtml(kind)}</option>`).join("");
    }
    const depth = Object.values(data.queue || {}).reduce((a, b) => a + b, 0);
    $("queue-count").textContent = String(depth);
  }

  async function loadOfficeKit() {
    try {
      const [data, jobs] = await Promise.all([
        api("GET", "/api/officekit"),
        api("GET", "/api/officekit/jobs?limit=30"),
      ]);
      renderOfficeKit(data);
      $("job-list").innerHTML = (jobs.jobs || []).map((job) =>
        `<li>
           <div class="row between">
             <code>${escapeHtml(job.kind)}</code>
             <span class="pill ${job.status === "completed" ? "ok" : job.status === "failed" ? "bad" : job.status === "claimed" ? "warn" : ""}">${escapeHtml(job.status)}</span>
           </div>
           <div class="meta">${escapeHtml(job.id)}${job.claimed_by ? ` · claimed by ${escapeHtml(job.claimed_by)}` : ""}</div>
         </li>`).join("") || "<li class=\"empty\">Queue is empty.</li>";
    } catch (error) { toast(error.message, 3000); }
  }

  async function pairDevice() {
    const name = ($("pair-name").value || "").trim() || "iQOO phone";
    busy("Pairing the device…");
    try {
      const res = await api("POST", "/api/officekit/pair", {
        device_name: name,
        capabilities: ["screen", "voice", "camera", "officekit"],
      });
      $("pair-result").classList.remove("hidden");
      $("pair-code").textContent = res.pairing_code || "";
      $("pair-note").textContent = (res.next || [])[0] || "";
      toast(`Paired ${name}.`);
      await loadOfficeKit();
    } catch (error) { toast(`Pairing failed: ${error.message}`, 4000); } finally { idle(); }
  }

  async function enqueueJob() {
    const kind = $("job-kind").value || "verify";
    busy("Queueing the job…");
    try {
      const job = await api("POST", "/api/officekit/jobs", { kind, payload: {}, session_id: state.sessionId || "" });
      toast(`Queued ${job.kind} (${job.id}).`);
      await loadOfficeKit();
    } catch (error) { toast(`Could not queue: ${error.message}`, 4000); } finally { idle(); }
  }

  /* ------------------------------------------------------------------ */
  /* dashboard                                                          */
  /* ------------------------------------------------------------------ */
  const STATUS_CLASS = {
    verified: "ok", failed: "bad", rolled_back: "bad", rejected: "bad", cancelled: "bad",
    awaiting_approval: "warn", patch_proposed: "warn", refining: "warn",
  };

  async function loadDashboard() {
    try {
      const data = await api("GET", "/api/dashboard");
      state.dashboard = data;
      const sessions = data.sessions || {};
      const safety = data.safety || {};
      const knowledge = data.knowledge || {};
      const code = data.codebase || {};
      const server = data.server || {};

      $("kpi-sessions").textContent = String(sessions.total ?? 0);
      $("kpi-sessions-sub").textContent = `${sessions.last_24h ?? 0} in the last 24h`;
      $("kpi-verified").textContent = String(sessions.verified ?? 0);
      $("kpi-rate").textContent = sessions.verify_rate == null ? "–" : `${Math.round(sessions.verify_rate * 100)}%`;
      $("kpi-rate-sub").textContent = sessions.verify_rate == null ? "no fix attempted yet" : "of attempted fixes";
      $("kpi-lessons").textContent = String(knowledge.lessons ?? 0);
      $("kpi-lessons-sub").textContent = `${knowledge.verified_lessons ?? 0} verified · reused ${knowledge.reused ?? 0}`;

      $("home-repo").innerHTML = md(
        `**${escapeHtml(server.repo || "?")}**\n` +
        `- branch \`${server.branch || "n/a"}\`${server.dirty ? " · **dirty working tree**" : " · clean"}\n` +
        `- ${code.files ?? 0} files · ${code.symbols ?? 0} symbols · ${code.lines ?? 0} lines\n` +
        `- languages: ${Object.entries(code.languages || {}).map(([k, v]) => `${k} (${v})`).join(", ") || "n/a"}\n` +
        `- approval gate: ${server.approval_gate ? "required" : "disabled"} · models: **${server.model_strategy || "?"}**` +
        `${server.cloud_allowed ? " (cloud allowed)" : " (local only)"}`
      );

      const entries = Object.entries(sessions.by_status || {}).sort((a, b) => b[1] - a[1]);
      const max = entries.reduce((top, entry) => Math.max(top, entry[1]), 0) || 1;
      $("home-bars").innerHTML = entries.length
        ? entries.map(([status, count]) =>
            `<div class="bar-row"><span class="lbl">${escapeHtml(status.replace(/_/g, " "))}</span>` +
            `<span class="bar-track"><span class="bar-fill ${STATUS_CLASS[status] || ""}" ` +
            `style="width:${Math.round((count / max) * 100)}%"></span></span>` +
            `<span class="num">${count}</span></div>`).join("")
        : "<p class=\"hint\">No sessions yet — report a bug on the Fix tab.</p>";

      const latest = sessions.latest || [];
      $("home-activity").innerHTML = latest.length
        ? latest.map((session) =>
            `<li><span class="dot ${escapeHtml(session.status || "")}"></span>` +
            `<span><span class="t">${escapeHtml(session.title || session.id)}</span>` +
            `<span class="s">${escapeHtml(session.status_label || session.status || "")} · ${escapeHtml(session.channel || "")}</span></span>` +
            `<span class="when">${escapeHtml(String(session.updated_at || "").slice(5, 16).replace("T", " "))}</span></li>`).join("")
        : "<li><span class=\"dot\"></span><span><span class=\"t\">Nothing yet</span></span><span class=\"when\"></span></li>";

      $("home-safety-stats").innerHTML = statGrid([
        ["commands", safety.commands_run ?? 0],
        ["policy rules", safety.policy_rules ?? 0],
        ["lessons", knowledge.lessons ?? 0],
        ["facts", knowledge.facts ?? 0],
      ]);
      const verdicts = Object.entries(safety.by_decision || {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "none yet";
      $("home-safety").innerHTML = md(
        `- **Sandbox:** ${verdicts}\n` +
        `- **Network:** ${safety.network_allowed ? "enabled ⚠️" : "blocked"} · ` +
        `**dependency install:** ${safety.dep_install_allowed ? "enabled ⚠️" : "blocked"}`
      );
    } catch (error) { toast(error.message, 3000); }
  }

  /* ------------------------------------------------------------------ */
  /* account                                                            */
  /* ------------------------------------------------------------------ */
  async function loadAccount() {
    try {
      const me = await api("GET", "/api/auth/me");
      state.user = me.user;
      const users = me.users || [];
      $("account-body").innerHTML = me.user
        ? md(`**${escapeHtml(me.user.display_name || me.user.username)}** · \`${escapeHtml(me.user.role)}\`\n` +
             `- signed in as \`${escapeHtml(me.user.username)}\`\n` +
             `- last login: ${escapeHtml((me.user.last_login || "now").replace("T", " ").replace("Z", ""))}\n` +
             `- ${users.length} account(s) on this server`)
        : md("Authentication is disabled on this server — run without `--no-auth` to enable it.");
      $("pill-user").textContent = me.user ? `👤 ${me.user.username}` : "👤 open";
    } catch (error) {
      $("account-body").innerHTML = md(`Could not read the account state: ${error.message}`);
    }
  }

  async function logout() {
    if (!window.confirm("Sign out of FixPilot?")) return;
    try { await api("POST", "/api/auth/logout", {}); } catch { /* the cookie is cleared server-side either way */ }
    location.reload();
  }

  /* ------------------------------------------------------------------ */
  /* liveness                                                           */
  /* ------------------------------------------------------------------ */
  async function checkHealth() {
    try {
      const health = await api("GET", "/api/health");
      state.online = health.ok === true;
      if (!state.session) {
        $("pill-link").textContent = "online";
        $("pill-link").className = "pill ok";
      }
    } catch {
      state.online = false;
      $("pill-link").textContent = "offline";
      $("pill-link").className = "pill bad";
    }
  }

  /* ------------------------------------------------------------------ */
  /* wiring                                                             */
  /* ------------------------------------------------------------------ */
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => showView(tab.dataset.view)));
  $("btn-investigate").addEventListener("click", investigate);
  $("btn-approve").addEventListener("click", () => approve(true));
  $("btn-reject").addEventListener("click", () => approve(false));
  $("btn-rollback").addEventListener("click", rollback);
  $("btn-refine").addEventListener("click", refine);
  $("btn-replay").addEventListener("click", replay);
  $("btn-report").addEventListener("click", () => {
    if (state.sessionId) window.open(`/api/sessions/${state.sessionId}/report?format=markdown&token=${TOKEN}`, "_blank");
  });
  $("btn-clear").addEventListener("click", () => {
    $("report-text").value = "";
    state.attachments = [];
    $("attachments").innerHTML = "";
    ["pipeline", "understanding", "cause", "patch", "verification", "replay", "intake-box"].forEach((id) => $(id).classList.add("hidden"));
  });
  $("btn-refresh-sessions").addEventListener("click", loadSessions);
  $("btn-refresh-system").addEventListener("click", loadSystem);
  $("btn-run").addEventListener("click", runCommand);
  $("command-input").addEventListener("keydown", (event) => { if (event.key === "Enter") runCommand(); });
  $("btn-ask").addEventListener("click", ask);
  $("ask-input").addEventListener("keydown", (event) => { if (event.key === "Enter") ask(); });

  /* session controls */
  $("btn-apply").addEventListener("click", applyAgain);
  $("btn-cancel").addEventListener("click", cancelSession);
  $("btn-reinvestigate").addEventListener("click", reinvestigate);
  $("btn-intake").addEventListener("click", previewIntake);

  /* repo */
  $("btn-refresh-repo").addEventListener("click", loadRepo);
  $("btn-refresh-diff").addEventListener("click", loadRepo);
  $("btn-search").addEventListener("click", searchCode);
  $("search-input").addEventListener("keydown", (event) => { if (event.key === "Enter") searchCode(); });
  $("tree-filter").addEventListener("input", renderTree);
  $("btn-close-file").addEventListener("click", () => $("file-viewer").classList.add("hidden"));

  /* safety */
  $("btn-refresh-sandbox").addEventListener("click", () => loadSandbox().catch((e) => toast(e.message, 3000)));
  $("btn-refresh-audit").addEventListener("click", () => loadAudit().catch((e) => toast(e.message, 3000)));
  $("btn-refresh-lessons").addEventListener("click", () => loadLessons().catch((e) => toast(e.message, 3000)));

  /* office kit */
  $("btn-refresh-officekit").addEventListener("click", loadOfficeKit);
  $("btn-refresh-jobs").addEventListener("click", loadOfficeKit);
  $("btn-pair").addEventListener("click", pairDevice);
  $("btn-enqueue").addEventListener("click", enqueueJob);

  /* dashboard, "More" hub and account */
  $("btn-refresh-home").addEventListener("click", loadDashboard);
  $("btn-logout").addEventListener("click", logout);
  $("pill-user").addEventListener("click", () => showView("more"));
  document.querySelectorAll("[data-goto]").forEach((item) =>
    item.addEventListener("click", () => showView(item.dataset.goto)));
  document.querySelectorAll("[data-back]").forEach((button) =>
    button.addEventListener("click", () => showView(button.dataset.back)));

  (async function boot() {
    try {
      const overview = await api("GET", "/api/overview");
      state.overview = overview;
      $("repo-line").textContent = `${overview.repo.root.split("/").slice(-1)[0]} · ${overview.repo.stats.files} files indexed`;
      $("pill-model").textContent = overview.models.strategy;
    } catch (error) {
      if (error.status === 401) return;   // the reload above is already on its way
      $("repo-line").textContent = "backend unreachable";
      $("pill-link").className = "pill bad";
      $("pill-link").textContent = "offline";
    }
    await Promise.all([loadDashboard(), loadAccount()]);
    await checkHealth();
    setInterval(checkHealth, 15000);
  })();

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
})();
