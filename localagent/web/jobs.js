"use strict";
// Long-running jobs: sidebar items, job view, new-job dialog. Uses helpers and state from app.js.

const JOB_STATUS = {
  planning: { label: "Planning", cls: "", icon: "✎" },
  awaiting_approval: { label: "Plan ready: needs your approval", cls: "warn", icon: "!" },
  running: { label: "Running", cls: "", icon: "▶" },
  paused: { label: "Paused", cls: "warn", icon: "⏸" },
  waiting_user: { label: "Waiting for you", cls: "warn", icon: "?" },
  done: { label: "Done", cls: "ok", icon: "✓" },
  failed: { label: "Failed", cls: "bad", icon: "✗" },
  cancelled: { label: "Cancelled", cls: "", icon: "–" },
};
const TASK_ICON = { pending: "○", running: "◐", done: "●", failed: "✗", skipped: "–", waiting_user: "?" };
const PERMISSIONS = [
  ["cmd:network", "Use the network (downloads, web requests, git clone)"],
  ["cmd:package-install", "Install Python/Node packages into the environment"],
  ["cmd:process-control", "Stop processes"],
];

const J = { data: null, activity: "", expanded: new Set(), refreshTimer: null };

function jobItem(j) {
  const st = JOB_STATUS[j.status] || { icon: "•", cls: "" };
  const attention = ["awaiting_approval", "waiting_user"].includes(j.status) ||
    (j.status === "paused" && (j.status_reason || "").startsWith("Needs your decision"));
  return h("li", {
    class: "chat-item job-item" + (j.id === S.currentJobId ? " active" : ""),
    title: `${j.title} — ${st.label || j.status}${j.status_reason ? `: ${j.status_reason}` : ""}`,
    onclick: () => selectJob(j.id),
  },
  j.status === "running" || j.status === "planning" ? h("span", { class: "dot" })
    : h("span", { class: `job-icon ${attention ? "attn" : st.cls}` }, st.icon),
  h("span", { class: "name" }, j.title));
}

function closeJobView() {
  S.currentJobId = null;
  store.set("currentJobId", null);
  J.data = null;
  $("#job-view").hidden = true;
}

async function selectJob(id) {
  S.currentJobId = id;
  S.currentChatId = null;
  store.set("currentJobId", id);
  store.set("currentChatId", null);
  removeStream();
  for (const sel of ["#chat-header", "#messages", "#composer", "#empty", "#tasks-panel"]) $(sel).hidden = true;
  $("#job-view").hidden = false;
  J.activity = "";
  await loadJob();
  renderSidebar();
}

async function loadJob() {
  if (!S.currentJobId) return;
  try {
    J.data = await api("GET", `/api/jobs/${S.currentJobId}`);
  } catch (e) {
    toast(e.message);
    closeJobView();
    renderChat();
    return;
  }
  renderJob();
}

function scheduleJobRefresh() {
  clearTimeout(J.refreshTimer);
  J.refreshTimer = setTimeout(loadJob, 250);
}

function handleJobEvent(ev) {
  if (ev.type === "job") {
    const known = S.jobs.find((j) => j.id === ev.job_id);
    if (ev.deleted || !known) refreshState();
    else { Object.assign(known, { status: ev.status, status_reason: ev.status_reason }); renderSidebar(); }
    if (ev.job_id === S.currentJobId) {
      if (ev.deleted) { closeJobView(); renderChat(); } else scheduleJobRefresh();
    }
    return;
  }
  if (ev.job_id !== S.currentJobId) return;
  if (ev.type === "job_activity") setActivity(ev.activity);
  else if (ev.type === "tool_start") setActivity(`Running ${ev.call.name}${argSummary(ev.call.name, ev.call.arguments) ? `: ${argSummary(ev.call.name, ev.call.arguments).slice(0, 80)}` : ""}`);
  else if (ev.type === "generation_start") setActivity(ev.task_key ? `Thinking about [${ev.task_key}]…` : "Planning…");
  else if (ev.type === "status" && ev.state === "loading_model") setActivity("Loading model…");
  else if (ev.type === "status" && ev.state === "queued") setActivity("Waiting for the model…");
  else if (ev.type === "status" && ev.state === "paused_gpu_busy") setActivity("Paused: another app is using the GPU");
  else if (ev.type === "status" && ev.state === "idle") { setActivity(""); scheduleJobRefresh(); }
}

function setActivity(text) {
  J.activity = text;
  const el = $("#job-activity");
  if (el) { el.textContent = text; el.hidden = !text; }
}

// ---------- rendering ----------
function renderJob() {
  const view = $("#job-view");
  const { job, tasks, journal, runs } = J.data;
  const st = JOB_STATUS[job.status] || { label: job.status, cls: "" };
  const project = S.projects.find((p) => p.id === job.project_id);
  view.replaceChildren(
    h("header", { class: "job-header" },
      h("div", { class: "title-wrap" },
        h("h1", {}, job.title),
        h("span", { class: "badge" }, project ? project.name : ""),
        h("span", { class: `pill ${st.cls}` }, st.label)),
      h("div", { class: "header-actions" }, jobControls(job),
        h("button", { class: "icon-btn", title: "Job options", onclick: (e) => jobMenu(e.currentTarget, job) }, "⋯"))),
    h("div", { class: "job-body" },
      job.status_reason ? h("div", { class: `job-reason ${st.cls}` }, job.status_reason) : null,
      h("div", { id: "job-activity", class: "job-activity", hidden: !J.activity }, J.activity),
      h("section", { class: "job-goal" }, h("div", { class: "section-label" }, "Goal"), h("p", {}, job.goal)),
      budgetLine(job),
      questionsPanel(job, tasks),
      scratchpadPanel(job, J.data.context || [], J.data.context_limit || 6000),
      J.data.note_count ? notesPanel(job, J.data.note_count) : null,
      h("section", {}, h("div", { class: "section-label" }, `Plan${tasks.length ? ` · ${planCounts(tasks)}` : ""}`),
        tasks.length ? planTree(job, tasks, runs) : h("p", { class: "muted" },
          job.status === "planning" ? "The agent is writing a plan. You'll review it before anything runs." : "No plan.")),
      h("details", { class: "job-journal", open: journal.length <= 12 },
        h("summary", {}, `Journal (${journal.length})`),
        h("ol", {}, journal.slice().reverse().map((e) => h("li", {},
          h("time", {}, new Date(e.created_at * 1000).toLocaleString()),
          e.task_key ? h("span", { class: "tkey" }, `[${e.task_key}]`) : null,
          h("span", {}, e.text)))))));
}

function planCounts(tasks) {
  const parents = new Set(tasks.map((t) => t.parent_key).filter(Boolean));
  const leaves = tasks.filter((t) => !parents.has(t.key));
  const done = leaves.filter((t) => t.status === "done").length;
  const failed = leaves.filter((t) => t.status === "failed").length;
  return `${done} of ${leaves.length} done${failed ? `, ${failed} failed` : ""}`;
}

function budgetLine(job) {
  const b = job.budget, u = job.usage || {};
  const hours = (u.seconds || 0) / 3600;
  if (b.indefinite) return h("div", { class: "job-budget" }, `Budget: no limit · used ${hours.toFixed(2)} h, ${u.steps || 0} model steps`);
  const pctH = b.max_hours ? Math.min(100, (hours / b.max_hours) * 100) : 0;
  const pctS = b.max_steps ? Math.min(100, ((u.steps || 0) / b.max_steps) * 100) : 0;
  const bar = (pct) => h("span", { class: "bar" }, h("span", { class: "fill", style: `width:${pct}%` }));
  return h("div", { class: "job-budget" },
    h("span", {}, "Time"), bar(pctH), h("span", {}, `${hours.toFixed(2)} / ${b.max_hours} h`),
    h("span", {}, "Steps"), bar(pctS), h("span", {}, `${u.steps || 0} / ${b.max_steps}`));
}

function jobControls(job) {
  const btn = (label, cls, fn, title) => h("button", { class: `btn small ${cls}`, title, onclick: fn }, label);
  const post = (action) => async () => {
    try { J.data.job = await api("POST", `/api/jobs/${job.id}/${action}`); renderJob(); refreshState(); }
    catch (e) { toast(e.message); }
  };
  const out = [];
  if (job.status === "awaiting_approval") {
    out.push(btn("Request changes", "ghost", () => replanDialog(job), "Describe what to change; the agent re-plans"));
    out.push(btn("Approve plan & start", "primary", post("approve")));
  }
  if (["running", "waiting_user", "planning"].includes(job.status)) {
    out.push(btn("Pause", "", post("pause"), "Finish the current step, then pause"));
    out.push(btn("Stop", "danger", post("stop"), "Stop immediately; progress is kept and you can resume"));
  }
  if (["paused", "failed"].includes(job.status)) out.push(btn("Resume", "primary", post("resume")));
  return out;
}

function jobMenu(anchor, job) {
  const items = [{ label: "Edit budget & permissions", action: () => jobSettingsDialog(job) }];
  if (!["done", "cancelled", "failed"].includes(job.status)) {
    items.push({ label: "Cancel job", danger: true, action: async () => {
      if (!confirm(`Cancel "${job.title}"? It can't be resumed afterwards. Files it made stay in the workspace.`)) return;
      try { await api("POST", `/api/jobs/${job.id}/cancel`); loadJob(); } catch (e) { toast(e.message); }
    } });
  }
  items.push("-", { label: "Delete job", danger: true, action: async () => {
    if (!confirm(`Delete "${job.title}" and its history? Files in the workspace (including its jobs/ folder) are not touched.`)) return;
    try { await api("DELETE", `/api/jobs/${job.id}`); closeJobView(); refreshState(); renderChat(); } catch (e) { toast(e.message); }
  } });
  openMenu(anchor, items);
}

function questionsPanel(job, tasks) {
  const questions = [];
  if (job.inputs && job.inputs.pending_question) questions.push({ text: job.inputs.pending_question, taskId: null, label: "About the goal" });
  if (job.inputs && job.inputs.pending_approval) questions.push({ text: `Approval needed: ${job.inputs.pending_approval}`, approval: true, label: "Planning" });
  for (const t of tasks) {
    if (t.status === "waiting_user" && t.question) {
      questions.push({ text: t.question, taskId: t.id, label: `[${t.key}] ${t.title}`,
        approval: t.waiting_kind === "approval", gate: t.waiting_kind === "gate" });
    }
  }
  if (!questions.length) return null;
  return h("section", { class: "job-questions" }, h("div", { class: "section-label" }, "Waiting on you"),
    questions.map((q) => {
      if (q.gate) {
        const feedback = h("textarea", { rows: 2, placeholder: "Or describe what to change…" });
        const send = async (text) => {
          try { await api("POST", `/api/jobs/${job.id}/answer`, { text, task_id: q.taskId }); loadJob(); }
          catch (e) { toast(e.message); }
        };
        return h("div", { class: "ask" },
          h("strong", {}, q.label), h("div", {}, q.text), feedback,
          h("div", { class: "modal-actions" },
            h("button", { class: "btn small", onclick: () => feedback.value.trim() && send(feedback.value.trim()) }, "Request changes"),
            h("button", { class: "btn primary small", onclick: () => send("approve") }, "Approve")));
      }
      if (q.approval) {
        return h("div", { class: "ask" },
          h("strong", {}, q.label), h("div", {}, q.text),
          h("div", { class: "hint" }, "Only this task waits. The rest of the job keeps running."),
          h("div", { class: "modal-actions" }, h("button", { class: "btn primary small", onclick: () => {
            if (S.approvals.size) showNextApproval(); else refreshState().then(showNextApproval);
          } }, "Open approval")));
      }
      const input = h("textarea", { rows: 2, placeholder: "Your answer…" });
      return h("div", { class: "ask" },
        h("strong", {}, q.label), h("div", {}, q.text), input,
        h("div", { class: "modal-actions" }, h("button", {
          class: "btn primary small", onclick: async () => {
            if (!input.value.trim()) return;
            try { await api("POST", `/api/jobs/${job.id}/answer`, { text: input.value.trim(), task_id: q.taskId }); loadJob(); }
            catch (e) { toast(e.message); }
          },
        }, "Send answer")));
    }));
}

function scratchpadPanel(job, items, limit) {
  const used = items.reduce((n, i) => n + i.text.length, 0);
  const input = h("input", { type: "text", placeholder: "Add a note every task should know (e.g. a constraint or where a file is)…" });
  const add = async () => {
    if (!input.value.trim()) return;
    try { await api("POST", `/api/jobs/${job.id}/context`, { text: input.value.trim() }); loadJob(); }
    catch (e) { toast(e.message); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  return h("section", { class: "scratchpad" },
    h("div", { class: "section-label" }, `Scratchpad context · ${used}/${limit} characters`),
    h("p", { class: "hint" }, "The job's working memory. Every task reads it first; the agent adds facts, decisions, and dead ends as it learns them."),
    items.length ? h("ul", { class: "context-items" }, items.map((i) => h("li", {},
      h("span", { class: "tkey" }, `c${i.id}`),
      h("span", { class: "ctext" }, i.text),
      h("span", { class: "corigin" }, i.author === "user" ? "you" : (i.task_key ? `[${i.task_key}]` : "planner")),
      h("button", { class: "icon-btn small", title: "Remove", onclick: async () => {
        try { await api("DELETE", `/api/jobs/${job.id}/context/${i.id}`); loadJob(); } catch (e) { toast(e.message); }
      } }, "✕")))) : h("p", { class: "muted" }, "Empty so far."),
    h("div", { class: "context-add" }, input, h("button", { class: "btn small", onclick: add }, "Add")));
}

function notesPanel(job, count) {
  const list = h("div", { class: "notes-list" });
  const search = h("input", { type: "text", placeholder: "Search notes…" });
  const load = async () => {
    try {
      const notes = await api("GET", `/api/jobs/${job.id}/notes?q=${encodeURIComponent(search.value.trim())}`);
      list.replaceChildren(...(notes.length ? notes.map((n) => h("div", { class: "note" },
        h("div", {}, h("span", { class: "tkey" }, `n${n.id}`), " ", h("strong", {}, n.claim)),
        h("blockquote", {}, `“${n.quote}”`),
        h("div", { class: "muted" }, `${n.source}${n.location ? ` · ${n.location}` : ""}${n.task_key ? ` · [${n.task_key}]` : ""}`)))
        : [h("p", { class: "muted" }, "No matching notes.")]));
    } catch (e) { toast(e.message); }
  };
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); load(); } });
  const details = h("details", { class: "job-notes" },
    h("summary", {}, `Evidence notes (${count}) · quotes verified against their sources`),
    h("div", { class: "context-add" }, search, h("button", { class: "btn small", onclick: load }, "Search")), list);
  details.addEventListener("toggle", () => { if (details.open && !list.children.length) load(); });
  return details;
}

function planTree(job, tasks, runs) {
  const kids = new Map();
  for (const t of tasks) {
    const k = t.parent_key || "";
    if (!kids.has(k)) kids.set(k, []);
    kids.get(k).push(t);
  }
  const walk = (parent) => h("ul", { class: "plan-tree" }, (kids.get(parent) || []).map((t) => {
    const isParent = kids.has(t.key);
    if (isParent) return h("li", { class: "plan-group" }, h("div", { class: "plan-row group" }, h("span", { class: "tkey" }, `[${t.key}]`), t.title), walk(t.key));
    const open = J.expanded.has(t.id);
    return h("li", { class: `plan-leaf ${t.status}` },
      h("div", { class: "plan-row", onclick: () => { open ? J.expanded.delete(t.id) : J.expanded.add(t.id); renderJob(); } },
        h("span", { class: "ticon" }, TASK_ICON[t.status] || "○"),
        h("span", { class: "tkey" }, `[${t.key}]`),
        h("span", { class: "ttitle" }, t.title),
        t.attempts ? h("span", { class: "attempts", title: "Attempts used" }, `${t.attempts}/${t.max_attempts}`) : null),
      open ? taskDetails(job, t, runs.filter((r) => r.task_id === t.id)) : null);
  }));
  return walk("");
}

function taskDetails(job, t, runs) {
  const row = (label, content) => content ? h("div", { class: "td-row" }, h("span", { class: "td-label" }, label), h("div", {}, content)) : null;
  const act = (label, action, cls = "") => h("button", {
    class: `btn small ${cls}`, onclick: async () => {
      try { await api("POST", `/api/jobs/${job.id}/tasks/${t.id}/${action}`); loadJob(); } catch (e) { toast(e.message); }
    },
  }, label);
  return h("div", { class: "task-details" },
    row("Instructions", t.instructions),
    row("Done when", t.done_when),
    t.checks.length ? row("Checks", h("ul", {}, t.checks.map((c) => h("li", {}, `${c.type}: ${c.path || c.command || ""}${c.text ? ` contains "${c.text}"` : ""}`)))) : null,
    t.checklist && t.checklist.length ? row("Checklist", h("ul", { class: "checklist" }, t.checklist.map((c) =>
      h("li", { class: c.done ? "done" : "" }, `${c.done ? "☑" : "☐"} ${c.text}`)))) : null,
    row("Result", t.result_summary),
    row("Waiting on you", t.question),
    t.guidance.length ? row("Notes for next attempt", h("ul", {}, t.guidance.map((g) => h("li", { class: "guidance" }, g)))) : null,
    runs.length ? row("Attempts", h("ul", { class: "runs" }, runs.map((r) => h("li", {},
      `#${r.attempt || "?"} · ${r.outcome || r.status} · ${r.steps} steps `,
      h("button", { class: "btn ghost small", onclick: () => transcriptDialog(job, r) }, "View transcript"))))) : null,
    h("div", { class: "modal-actions" },
      ["failed", "skipped", "done"].includes(t.status) ? act("Retry", "retry") : null,
      ["pending", "failed", "waiting_user"].includes(t.status) ? act("Skip", "skip", "ghost") : null));
}

// ---------- dialogs ----------
async function transcriptDialog(job, run) {
  const data = await api("GET", `/api/jobs/${job.id}/runs/${run.id}`);
  const results = new Map(data.messages.filter((m) => m.role === "tool").map((m) => [m.tool_call_id, m]));
  const saved = S.messages;
  S.messages = data.messages;          // renderMessage looks up tool calls in S.messages
  const body = data.messages.map((m) => renderMessage(m, results)).filter(Boolean);
  S.messages = saved;
  const dlg = $("#modal");
  dlg.oncancel = null;
  openModal(
    h("h3", {}, `Transcript: ${run.kind === "plan" ? "planning" : `attempt #${run.attempt}`}`),
    h("p", {}, `${run.outcome || run.status} · ${run.steps} steps${run.summary ? ` · ${run.summary.slice(0, 200)}` : ""}`),
    h("div", { class: "transcript" }, body),
    h("div", { class: "modal-actions" }, h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Close")));
}

function replanDialog(job) {
  const text = h("textarea", { rows: 4, placeholder: "e.g. Split the reading into one task per document, and add a final check that report.md exists." });
  const dlg = $("#modal");
  dlg.oncancel = null;
  openModal(
    h("h3", {}, "Request changes to the plan"),
    h("p", {}, "The agent writes a new plan using your feedback, and you review it again."),
    text,
    h("div", { class: "modal-actions" },
      h("button", { type: "button", class: "btn ghost", onclick: () => dlg.close() }, "Cancel"),
      h("button", { type: "button", class: "btn primary", onclick: async () => {
        if (!text.value.trim()) return;
        try { await api("POST", `/api/jobs/${job.id}/replan`, { feedback: text.value.trim() }); dlg.close(); loadJob(); }
        catch (e) { toast(e.message); }
      } }, "Re-plan")));
}

function budgetFields(budget = { max_hours: 4, max_steps: 400, indefinite: false }) {
  const hours = h("input", { type: "number", min: "0.1", step: "0.5", value: budget.max_hours ?? 4 });
  const steps = h("input", { type: "number", min: "1", step: "50", value: budget.max_steps ?? 400 });
  const indefinite = h("input", { type: "checkbox" });
  indefinite.checked = !!budget.indefinite;
  const sync = () => { hours.disabled = steps.disabled = indefinite.checked; };
  indefinite.addEventListener("change", sync);
  sync();
  return {
    el: h("div", { class: "grid2" },
      field("Time budget (hours)", hours), field("Model steps budget", steps),
      field("No limit", h("label", { class: "check" }, indefinite, "run indefinitely"),
        "Stops on its own only when finished, or after repeated re-planning without progress.")),
    read: () => ({ max_hours: Number(hours.value), max_steps: Number(steps.value), indefinite: indefinite.checked }),
  };
}

function permissionFields(selected = []) {
  const boxes = PERMISSIONS.map(([key, label]) => {
    const cb = h("input", { type: "checkbox", value: key });
    cb.checked = selected.includes(key);
    return { cb, el: h("label", { class: "check" }, cb, label) };
  });
  return {
    el: field("Pre-approve for this job", h("div", { class: "perm-list" }, boxes.map((b) => b.el)),
      "Jobs run unattended. Anything not pre-approved still asks you, and only that task waits."),
    read: () => boxes.filter((b) => b.cb.checked).map((b) => b.cb.value),
  };
}

async function newJobDialog(project) {
  let templates = [];
  try { templates = await api("GET", "/api/job_templates"); } catch { templates = [{ name: "generic", label: "General task", description: "", inputs: {} }]; }
  const title = h("input", { type: "text", placeholder: "e.g. Lake report" });
  const goal = h("textarea", { rows: 5, placeholder: "Describe the outcome you want, where the inputs are, and what the result should look like." });
  const typeSel = h("select", {}, templates.map((t) => h("option", { value: t.name }, t.label)));
  const typeHint = h("div", { class: "hint" });
  const inputsBox = h("div", { class: "grid2" });
  const inputEls = {};
  const renderInputs = () => {
    const t = templates.find((x) => x.name === typeSel.value) || templates[0];
    typeHint.textContent = t.description;
    inputsBox.replaceChildren();
    for (const k of Object.keys(inputEls)) delete inputEls[k];
    for (const [key, spec] of Object.entries(t.inputs || {})) {
      const el = spec.enum
        ? h("select", {}, spec.enum.map((v) => h("option", { value: v, selected: v === spec.default }, v)))
        : h("input", { type: "text", value: spec.default ?? "" });
      inputEls[key] = el;
      inputsBox.append(field(spec.label || key, el));
    }
  };
  typeSel.addEventListener("change", renderInputs);
  renderInputs();
  const schedule = h("select", {},
    h("option", { value: "now" }, "Run whenever possible"),
    h("option", { value: "background_hours" }, "Only during background hours (Settings → Resources)"));
  const budget = budgetFields();
  const perms = permissionFields();
  const dlg = $("#modal");
  dlg.oncancel = null;
  openModal(
    h("h3", {}, `New job in ${project.name}`),
    h("p", {}, "A job is a long-running task. The agent first writes a plan for you to approve, then works through it in the background. Chats always come first; you can pause or stop it anytime."),
    field("Job type", typeSel, typeHint),
    field("Title", title),
    field("Goal", goal),
    inputsBox,
    budget.el,
    field("Schedule", schedule),
    perms.el,
    h("div", { class: "modal-actions" },
      h("button", { type: "button", class: "btn ghost", onclick: () => dlg.close() }, "Cancel"),
      h("button", { type: "button", class: "btn primary", onclick: async () => {
        try {
          const inputs = Object.fromEntries(Object.entries(inputEls).map(([k, el]) => [k, el.value.trim()]));
          const job = await api("POST", "/api/jobs", { project_id: project.id, title: title.value.trim(), goal: goal.value.trim(),
            template: typeSel.value, inputs, budget: budget.read(), schedule: schedule.value, permissions: perms.read() });
          dlg.close();
          S.collapsed[project.id] = false;
          store.set("collapsedProjects", S.collapsed);
          await refreshState();
          selectJob(job.id);
        } catch (e) { toast(e.message); }
      } }, "Create job & start planning")));
}

function jobSettingsDialog(job) {
  const budget = budgetFields(job.budget);
  const perms = permissionFields(job.permissions || []);
  const dlg = $("#modal");
  dlg.oncancel = null;
  openModal(
    h("h3", {}, "Budget & permissions"),
    budget.el,
    perms.el,
    h("div", { class: "modal-actions" },
      h("button", { type: "button", class: "btn ghost", onclick: () => dlg.close() }, "Cancel"),
      h("button", { type: "button", class: "btn primary", onclick: async () => {
        try { await api("PATCH", `/api/jobs/${job.id}`, { budget: budget.read(), permissions: perms.read() }); dlg.close(); loadJob(); }
        catch (e) { toast(e.message); }
      } }, "Save")));
}
