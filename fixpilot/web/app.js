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
  function showView(name) {
    document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
    $(`view-${name}`).classList.add("active");
    document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
    if (name === "sessions") loadSessions();
    if (name === "system") loadSystem();
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
    } else if (session.patch && session.patch.summary) {
      $("patch").classList.remove("hidden");
      $("patch-body").innerHTML = md(explain.patch || session.patch.summary);
      $("patch-diff").innerHTML = "";
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
      const [overview, models, skills, memory, officekit] = await Promise.all([
        api("GET", "/api/overview"),
        api("GET", "/api/models"),
        api("GET", "/api/skills"),
        api("GET", "/api/memory"),
        api("GET", "/api/officekit"),
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
      $("officekit-body").innerHTML = md(
        `Mode: **${officekit.mode}** · devices online: ${officekit.online_devices}\n\n` +
        (officekit.devices || []).map((device) => `- ${device.status === "online" ? "🟢" : "⚪️"} ${device.name} (\`${device.status}\`) — ${(device.capabilities || []).join(", ")}`).join("\n") +
        "\n\n**Queue:** " + JSON.stringify(officekit.queue) + "\n\n" +
        (officekit.how_it_works || []).map((line) => `- ${line}`).join("\n") +
        "\n\n_Pair a phone:_ `POST /api/officekit/pair`"
      );
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
    ["pipeline", "understanding", "cause", "patch", "verification", "replay"].forEach((id) => $(id).classList.add("hidden"));
  });
  $("btn-refresh-sessions").addEventListener("click", loadSessions);
  $("btn-refresh-system").addEventListener("click", loadSystem);
  $("btn-run").addEventListener("click", runCommand);
  $("command-input").addEventListener("keydown", (event) => { if (event.key === "Enter") runCommand(); });
  $("btn-ask").addEventListener("click", ask);
  $("ask-input").addEventListener("keydown", (event) => { if (event.key === "Enter") ask(); });

  (async function boot() {
    try {
      const overview = await api("GET", "/api/overview");
      state.overview = overview;
      $("repo-line").textContent = `${overview.repo.root.split("/").slice(-1)[0]} · ${overview.repo.stats.files} files indexed`;
      $("pill-model").textContent = overview.models.strategy;
    } catch (error) {
      $("repo-line").textContent = "backend unreachable";
      $("pill-link").className = "pill bad";
      $("pill-link").textContent = "offline";
    }
  })();

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
})();
