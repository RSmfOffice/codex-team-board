let selectedProject = "";
let selectedTaskId = "";
let includeArchived = false;
let projectsById = new Map();
let tasksById = new Map();
let eventsByTask = new Map();
let timer = null;

const lanes = ["ready", "running", "review", "blocked", "done"];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function timeText(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

function ageText(seconds) {
  if (!seconds) return "刚刚";
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时`;
}

function syncTopbarHeight() {
  const topbar = document.querySelector(".topbar");
  document.documentElement.style.setProperty(
    "--topbar-height",
    `${topbar.getBoundingClientRect().height}px`,
  );
}

async function loadProjects() {
  const query = includeArchived ? "?include_archived=1" : "";
  const response = await fetch(`/api/projects${query}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`projects ${response.status}`);
  const data = await response.json();
  projectsById = new Map(
    data.projects.map((project) => [project.project_id, project]),
  );
  if (selectedProject && !projectsById.has(selectedProject)) {
    selectedProject = "";
  }
  const select = document.getElementById("project-select");
  select.innerHTML = "";
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = includeArchived ? "所有项目（含归档）" : "所有活动项目";
  select.append(allOption);
  for (const project of data.projects) {
    const option = document.createElement("option");
    option.value = project.project_id;
    const archivedLabel = project.archived_at ? " · 已归档" : "";
    option.textContent = `${project.name} · ${project.project_id}${archivedLabel}`;
    select.append(option);
  }
  select.value = selectedProject;
  updateArchiveButton();
}

function updateArchiveButton() {
  const button = document.getElementById("archive-target");
  const task = selectedTaskId ? tasksById.get(selectedTaskId) : null;
  if (task) {
    button.disabled = false;
    button.textContent = task.archived_at
      ? "恢复当前任务"
      : "归档当前任务";
    button.classList.toggle("restore", Boolean(task.archived_at));
    return;
  }
  const project = projectsById.get(selectedProject);
  button.disabled = !project;
  button.textContent = project?.archived_at
    ? "恢复当前项目"
    : "归档当前项目";
  button.classList.toggle("restore", Boolean(project?.archived_at));
}

function renderCard(task) {
  const blocker = task.blocked_by?.length
    ? `<span class="chip blocked">等待 ${task.blocked_by.length}</span>`
    : "";
  const lease = task.status === "running" && task.lease_until
    ? `<span class="chip">lease ${escapeHtml(timeText(task.lease_until))}</span>`
    : "";
  const archived = task.archived_at
    ? '<span class="chip archived">已归档</span>'
    : "";
  const assigned = task.active_job
    ? `<span class="chip owner">已派发 ${escapeHtml(task.active_job.role)}</span>`
    : "";
  const claimWait = task.status === "ready" && task.claim_wait_seconds
    ? `<span class="chip ${escapeHtml(task.claim_wait_state)}">等待认领 ${escapeHtml(ageText(task.claim_wait_seconds))}</span>`
    : "";
  return `
    <article class="card" data-task-id="${escapeHtml(task.task_id)}">
      <div class="card-top">
        <span class="task-id">${escapeHtml(task.task_id)}</span>
        <span class="kind">${escapeHtml(task.kind)}</span>
      </div>
      <h3>${escapeHtml(task.title)}</h3>
      <div class="card-meta">
        <span class="chip owner">${escapeHtml(task.owner_role)}</span>
        <span class="chip">P${escapeHtml(task.priority)}</span>
        <span class="chip">${escapeHtml(task.project_name || "")}</span>
        ${blocker}${lease}${assigned}${claimWait}${archived}
      </div>
    </article>`;
}

function renderBoard(data) {
  tasksById = new Map(data.tasks.map((task) => [task.task_id, task]));
  if (selectedTaskId && !tasksById.has(selectedTaskId)) {
    closeDetail();
  }
  eventsByTask = new Map();
  for (const event of data.events || []) {
    const list = eventsByTask.get(event.task_id) || [];
    list.push(event);
    eventsByTask.set(event.task_id, list);
  }
  for (const lane of lanes) {
    const tasks = data.tasks.filter((task) => task.status === lane);
    document.getElementById(`count-${lane}`).textContent = tasks.length;
    const container = document.getElementById(`cards-${lane}`);
    container.innerHTML = tasks.length
      ? tasks.map(renderCard).join("")
      : '<div class="empty">暂无任务</div>';
  }

  const summary = [
    ["READY", data.counts.ready || 0],
    ["RUNNING", data.counts.running || 0],
    ["REVIEW", data.counts.review || 0],
    ["BLOCKED", data.counts.blocked || 0],
    ["DONE", data.counts.done || 0],
    ["PROJECTS", data.projects.length],
    ["MESSAGES", data.messages.length],
  ];
  document.getElementById("summary").innerHTML = summary
    .map(([label, value]) => `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`)
    .join("");

  renderCluster(data.cluster_goals || [], data.cluster_jobs || [], data);
  renderMessages(data.messages || []);
  document.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("click", () => showDetail(card.dataset.taskId));
  });
  updateArchiveButton();
}

function renderCluster(goals, jobs, data) {
  const container = document.getElementById("cluster-overview");
  const selectedName = data.projects.find(
    (project) => project.project_id === data.selected_project_id,
  )?.name;
  if (!goals.length) {
    const scope = selectedName ? `“${escapeHtml(selectedName)}”项目` : "当前范围";
    container.innerHTML = `
      <div class="cluster-empty">
        ${scope}没有自动集群目标；下方泳道展示直接创建的普通任务。
      </div>`;
    return;
  }
  const activeJobs = jobs.filter((job) => ["starting", "running"].includes(job.status));
  const cards = goals.slice(0, 4).map((goal) => {
    const goalJobs = activeJobs.filter((job) => job.goal_id === goal.goal_id);
    const runText = goalJobs.length
      ? goalJobs.map((job) => `${job.kind}:${job.role} PID ${job.pid || "—"}`).join(" · ")
      : "无活动进程";
    return `
      <article class="cluster-card">
        <div class="card-top">
          <span class="task-id">${escapeHtml(goal.goal_id)}</span>
          <span class="kind">${escapeHtml(goal.status)}</span>
        </div>
        <h3>${escapeHtml(goal.objective)}</h3>
        <div class="card-meta">
          <span class="chip">${escapeHtml(goal.project_name || "未知项目")}</span>
          <span class="chip owner">${escapeHtml(goal.autonomy)}</span>
          <span class="chip">lead ticks ${escapeHtml(goal.lead_ticks)}</span>
          <span class="chip">${escapeHtml(runText)}</span>
        </div>
      </article>`;
  }).join("");
  container.innerHTML = `<div class="cluster-title">自动集群目标 · ${activeJobs.length} 个活动进程</div>${cards}`;
}

function renderMessages(messages) {
  document.getElementById("message-count").textContent = messages.length;
  const container = document.getElementById("messages");
  if (!messages.length) {
    container.innerHTML = '<div class="empty">暂无团队消息</div>';
    return;
  }
  container.innerHTML = [...messages].reverse().slice(0, 12).map((message) => `
    <article class="message">
      <strong>${escapeHtml(message.sender)}${message.recipient ? ` → ${escapeHtml(message.recipient)}` : ""}</strong>
      <p>${escapeHtml(message.body)}</p>
      <time>${escapeHtml(timeText(message.created_at))}${message.task_id ? ` · ${escapeHtml(message.task_id)}` : ""}</time>
    </article>`).join("");
}

function showDetail(taskId) {
  const task = tasksById.get(taskId);
  if (!task) return;
  selectedTaskId = taskId;
  const dependencies = task.blocked_by?.length
    ? task.blocked_by.map((item) => `${item.task_id} (${item.status})`).join(", ")
    : "无";
  const body = document.getElementById("detail-body");
  const events = (eventsByTask.get(taskId) || []).slice(-12).reverse();
  const runtime = task.active_job
    ? `${task.active_job.kind}:${task.active_job.role} · ${task.active_job.status} · PID ${task.active_job.pid || "—"}`
    : task.status === "ready"
      ? `等待认领 ${ageText(task.claim_wait_seconds)}`
      : "—";
  const timeline = events.length
    ? events.map((event) => `
        <div class="timeline-row">
          <span>${escapeHtml(event.event_type)}</span>
          <strong>${escapeHtml(event.actor)}</strong>
          <time>${escapeHtml(timeText(event.created_at))}</time>
        </div>`).join("")
    : '<div class="empty">暂无事件</div>';
  body.innerHTML = `
    <div class="eyebrow">${escapeHtml(task.task_id)} · ${escapeHtml(task.status)}${task.archived_at ? " · 已归档" : ""}</div>
    <h2>${escapeHtml(task.title)}</h2>
    <dl class="detail-grid">
      <dt>项目</dt><dd>${escapeHtml(task.project_name)}</dd>
      <dt>类型</dt><dd>${escapeHtml(task.kind)}</dd>
      <dt>负责角色</dt><dd>${escapeHtml(task.owner_role)}</dd>
      <dt>领取者</dt><dd>${escapeHtml(task.claimed_by || "—")}</dd>
      <dt>运行状态</dt><dd>${escapeHtml(runtime)}</dd>
      <dt>依赖阻塞</dt><dd>${escapeHtml(dependencies)}</dd>
      <dt>分支</dt><dd>${escapeHtml(task.branch || "—")}</dd>
      <dt>目标分支</dt><dd>${escapeHtml(task.target_branch || "—")}</dd>
      <dt>Worktree</dt><dd>${escapeHtml(task.worktree_path || "—")}</dd>
      <dt>租约</dt><dd>${escapeHtml(timeText(task.lease_until))}</dd>
      <dt>合并提交</dt><dd>${escapeHtml(task.merge_commit || "—")}</dd>
      <dt>Worker commit</dt><dd>${escapeHtml(task.commit_sha || "—")}</dd>
      <dt>创建时间</dt><dd>${escapeHtml(timeText(task.created_at))}</dd>
    </dl>
    <div class="detail-section"><h3>任务说明</h3><p>${escapeHtml(task.description || "—")}</p></div>
    <div class="detail-section"><h3>验收标准</h3><p>${escapeHtml(task.acceptance || "—")}</p></div>
    <div class="detail-section"><h3>结果</h3><p>${escapeHtml(task.result || "—")}</p></div>
    <div class="detail-section"><h3>测试</h3><p>${escapeHtml(task.tests || "—")}</p></div>
    <div class="detail-section"><h3>Review</h3><p>${escapeHtml(task.review_notes || "—")}</p></div>
    <div class="detail-section"><h3>交接事件</h3><div class="timeline">${timeline}</div></div>`;
  document.getElementById("detail").classList.remove("hidden");
  updateArchiveButton();
}

function closeDetail() {
  selectedTaskId = "";
  document.getElementById("detail").classList.add("hidden");
  updateArchiveButton();
}

async function refresh() {
  const health = document.getElementById("health");
  try {
    await loadProjects();
    const params = new URLSearchParams();
    if (selectedProject) params.set("project_id", selectedProject);
    if (includeArchived) params.set("include_archived", "1");
    const suffix = params.size ? `?${params.toString()}` : "";
    const response = await fetch(`/api/board${suffix}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`board ${response.status}`);
    const data = await response.json();
    renderBoard(data);
    health.textContent = `已同步 ${new Date().toLocaleTimeString("zh-CN")}`;
    health.className = "health ok";
  } catch (error) {
    health.textContent = `连接失败：${error.message}`;
    health.className = "health error";
  }
}

async function toggleArchiveTarget() {
  const task = selectedTaskId ? tasksById.get(selectedTaskId) : null;
  const project = projectsById.get(selectedProject);
  const health = document.getElementById("health");
  const target = task || project;
  if (!target) return;
  const archived = Boolean(target.archived_at);
  const label = task ? `任务“${task.title}”` : `项目“${project.name}”`;
  if (
    !archived
    && !window.confirm(`归档${label}？记录会保留，可随时恢复。`)
  ) {
    return;
  }
  const button = document.getElementById("archive-target");
  button.disabled = true;
  try {
    const endpoint = task ? "/api/tasks/archive" : "/api/projects/archive";
    const body = task
      ? { task_id: task.task_id, archived: !archived }
      : { project_id: project.project_id, archived: !archived };
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `archive ${response.status}`);
    if (task && !archived && !includeArchived) closeDetail();
    if (!task && !archived && !includeArchived) selectedProject = "";
    await refresh();
  } catch (error) {
    health.textContent = `归档失败：${error.message}`;
    health.className = "health error";
    updateArchiveButton();
  }
}

document.getElementById("project-select").addEventListener("change", (event) => {
  closeDetail();
  selectedProject = event.target.value;
  refresh();
});
document.getElementById("show-archived").addEventListener("change", (event) => {
  closeDetail();
  includeArchived = event.target.checked;
  const project = projectsById.get(selectedProject);
  if (!includeArchived && project?.archived_at) selectedProject = "";
  refresh();
});
document.getElementById("archive-target").addEventListener("click", toggleArchiveTarget);
document.getElementById("detail-close").addEventListener("click", closeDetail);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDetail();
});
window.addEventListener("resize", syncTopbarHeight);

syncTopbarHeight();
refresh();
timer = setInterval(refresh, 2000);
