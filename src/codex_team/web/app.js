const POLL_INTERVAL_MS = 2000;
const MAX_POLL_BACKOFF_MS = 30000;
const NATIVE_AGENT_SYNC_INTERVAL_MS = 30000;
const CACHE_TIME_BUCKET_SECONDS = 30;
const lanes = ["ready", "running", "review", "blocked", "done"];
const terminalGoalStatuses = new Set(["completed", "failed", "canceled"]);
const attentionHealthStates = new Set([
  "attention",
  "warning",
  "critical",
  "error",
  "failed",
  "degraded",
  "needs_attention",
  "unhealthy",
  "stalled",
]);
const heartbeatStates = Object.freeze({
  idle: { label: "无活动心跳", tone: "idle" },
  normal: { label: "心跳正常", tone: "normal" },
  warning: { label: "心跳延迟", tone: "warning" },
  critical: { label: "心跳超时", tone: "critical" },
});
const queueReasonLabels = Object.freeze({
  running: "运行中",
  review_pending: "等待 Review",
  blocked: "任务受阻",
  ready: "等待调度",
  global_capacity: "全局容量已满",
  goal_capacity: "目标容量已满",
  dependency_wait: "等待依赖完成",
  waiting_for_lead: "等待 Lead 规划",
  none: "无排队",
});
const agentStatusLabels = Object.freeze({
  starting: "启动中",
  busy: "忙碌",
  loaded: "已载入",
  idle: "空闲",
  stored: "已存储",
  superseded: "已替换",
  failed: "失败",
});
const nativeLifecycleLabels = Object.freeze({
  active: "运行中",
  idle: "空闲",
  completed: "已完成",
  aborted: "已中止",
  unknown: "未知",
});

let selectedProject = "";
let selectedTaskId = "";
let includeArchived = false;
let projectsById = new Map();
let tasksById = new Map();
let eventsByTask = new Map();
let projectsLoaded = false;
let pollTimer = null;
let pollInFlight = false;
let activeRequest = null;
let pollFailures = 0;
let refreshQueued = false;
let queuedForceRender = false;
let lastBoardSignature = "";
let lastBoardRevision = "";
let lastSummaryRevision = "";
let summaryEtag = "";
let boardEtag = "";
let detailRequestVersion = 0;
let lastDetailSignature = "";
let lastDetailTaskId = "";
let detailEventsEtag = "";
let lastNativeAgentSyncAt = 0;
let nativeAgentSyncInFlight = false;

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
  if (seconds === null || seconds === undefined || seconds === "") return "—";
  const parsed = Number(seconds);
  if (!Number.isFinite(parsed)) return "—";
  if (parsed <= 0) return "刚刚";
  if (parsed < 60) return `${Math.floor(parsed)} 秒`;
  if (parsed < 3600) return `${Math.floor(parsed / 60)} 分钟`;
  return `${Math.floor(parsed / 3600)} 小时`;
}

function nonNegativeNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function timeDerivedBucket(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0
    ? Math.floor(parsed / CACHE_TIME_BUCKET_SECONDS)
    : null;
}

function tokenText(value) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0
    ? Math.floor(parsed).toLocaleString("zh-CN")
    : "—";
}

function cacheRate(value, cachedValue, uncachedValue) {
  if (value !== null && value !== undefined && value !== "") {
    const explicit = Number(value);
    if (Number.isFinite(explicit) && explicit >= 0) {
      return Math.min(1, explicit);
    }
  }
  const cached = Number(cachedValue);
  const uncached = Number(uncachedValue);
  const cachedCount = cachedValue !== null
    && cachedValue !== undefined
    && Number.isFinite(cached)
    && cached >= 0
    ? cached
    : null;
  const uncachedCount = uncachedValue !== null
    && uncachedValue !== undefined
    && Number.isFinite(uncached)
    && uncached >= 0
    ? uncached
    : null;
  if (cachedCount === null || uncachedCount === null) return null;
  const total = cachedCount + uncachedCount;
  return total > 0 ? cachedCount / total : null;
}

function cacheRateText(value, cachedValue, uncachedValue) {
  const rate = cacheRate(value, cachedValue, uncachedValue);
  return rate === null ? "—" : `${Math.round(rate * 100)}%`;
}

function workflowEpochText(value) {
  const normalized = String(value ?? "").trim();
  if (!normalized) return "workflow unbound";
  const shortened = normalized.length > 22
    ? `${normalized.slice(0, 22)}…`
    : normalized;
  return `workflow ${shortened}`;
}

function jobRuntimeHtml(job) {
  if (!job) return "";
  const runtime = String(job.runtime || "").trim();
  const threadAction = String(job.thread_action || "").trim().toLowerCase();
  const thread = job.thread_id ?? job.thread;
  const turn = job.turn_id ?? job.turn;
  const cached = job.cached_input_tokens ?? job.cached_tokens;
  const uncached = job.uncached_input_tokens ?? job.uncached_tokens;
  const hasCached = cached !== undefined && cached !== null;
  const hasUncached = uncached !== undefined && uncached !== null;
  const hasCacheRate = job.cache_hit_rate !== undefined
    && job.cache_hit_rate !== null;
  const badges = [];
  const actionLabels = {
    fresh: "fresh thread",
    resume: "resume thread",
    fresh_exec: "fresh exec",
    fallback_exec: "fallback exec",
  };
  if (threadAction) {
    badges.push(
      `<span>${escapeHtml(actionLabels[threadAction] || `action ${threadAction}`)}</span>`,
    );
  } else if (runtime) {
    badges.push(
      `<span>${runtime === "app_server" ? "thread reuse" : "exec runtime"}</span>`,
    );
  }
  if (thread !== undefined && thread !== null && thread !== "") {
    badges.push(`<span>thread ${escapeHtml(thread)}</span>`);
  }
  if (turn !== undefined && turn !== null && turn !== "") {
    badges.push(`<span>turn ${escapeHtml(turn)}</span>`);
  }
  if (hasCacheRate || hasCached || hasUncached) {
    const parts = [
      `cache ${cacheRateText(job.cache_hit_rate, cached, uncached)}`,
    ];
    if (hasCached) parts.push(`${tokenText(cached)} cached`);
    if (hasUncached) parts.push(`${tokenText(uncached)} uncached`);
    badges.push(`<span>${escapeHtml(parts.join(" · "))}</span>`);
  }
  return badges.length
    ? `<div class="task-runtime">${badges.join("")}</div>`
    : "";
}

function goalHeartbeatInfo(goal) {
  const state = String(goal?.heartbeat_state || "idle").toLowerCase();
  const known = heartbeatStates[state];
  if (
    terminalGoalStatuses.has(String(goal?.status || ""))
    && nonNegativeNumber(goal?.active_job_count) === 0
  ) {
    return {
      state: "idle",
      label: "目标已结束",
      tone: "idle",
    };
  }
  return {
    state,
    label: known?.label || `心跳状态 ${state}`,
    tone: known?.tone || "unknown",
  };
}

function goalQueueReason(goal) {
  const state = String(goal?.queue_reason || "none").toLowerCase();
  if (
    terminalGoalStatuses.has(String(goal?.status || ""))
    && state === "none"
  ) {
    return {
      state,
      label: "无需调度",
    };
  }
  return {
    state,
    label: queueReasonLabels[state] || `排队状态 ${state}`,
  };
}

function waitDurationText(seconds) {
  if (seconds === undefined || seconds === null || seconds === "") return "—";
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value === 0) return "0 秒";
  return ageText(value);
}

function syncTopbarHeight() {
  const topbar = document.querySelector(".topbar");
  if (!topbar) return;
  document.documentElement.style.setProperty(
    "--topbar-height",
    `${topbar.getBoundingClientRect().height}px`,
  );
}

function setConnectionState(text, state) {
  const health = document.getElementById("health");
  health.textContent = text;
  health.className = `health ${state}`;
}

function setActionStatus(elementId, text, state = "") {
  const status = document.getElementById(elementId);
  status.textContent = text;
  status.className = `action-status ${state}`.trim();
}

async function requestJson(url, options = {}, label = "request") {
  const response = await fetch(url, options);
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    throw new Error(data.error || `${label} ${response.status}`);
  }
  return data;
}

async function requestRevisableJson(
  url,
  options = {},
  label = "request",
  etag = "",
) {
  const headers = { ...(options.headers || {}) };
  if (etag) headers["If-None-Match"] = etag;
  const response = await fetch(url, { ...options, headers });
  const responseEtag = response.headers?.get("ETag") || etag;
  if (response.status === 304) {
    return {
      notModified: true,
      data: null,
      etag: responseEtag,
    };
  }
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    throw new Error(data.error || `${label} ${response.status}`);
  }
  return {
    notModified: false,
    data,
    etag: responseEtag,
  };
}

function fillProjectSelect(select, projects, options) {
  const {
    allLabel = "",
    preferredValue = "",
    fallbackToSingleProject = false,
  } = options;
  select.innerHTML = "";
  if (allLabel) {
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = allLabel;
    select.append(allOption);
  }
  for (const project of projects) {
    const option = document.createElement("option");
    option.value = project.project_id;
    const archivedLabel = project.archived_at ? " · 已归档" : "";
    option.textContent = `${project.name} · ${project.project_id}${archivedLabel}`;
    select.append(option);
  }
  let value = projects.some((project) => project.project_id === preferredValue)
    ? preferredValue
    : "";
  if (!value && fallbackToSingleProject && projects.length === 1) {
    value = projects[0].project_id;
  }
  select.value = value;
}

async function loadProjects(signal) {
  const query = includeArchived ? "?include_archived=1" : "";
  const data = await requestJson(
    `/api/projects${query}`,
    { cache: "no-store", signal },
    "projects",
  );
  const projects = Array.isArray(data.projects) ? data.projects : [];
  projectsById = new Map(
    projects.map((project) => [project.project_id, project]),
  );
  if (selectedProject && !projectsById.has(selectedProject)) {
    selectedProject = "";
  }
  fillProjectSelect(
    document.getElementById("project-select"),
    projects,
    {
      allLabel: includeArchived ? "所有项目（含归档）" : "所有活动项目",
      preferredValue: selectedProject,
    },
  );
  fillProjectSelect(
    document.getElementById("cluster-project-select"),
    projects,
    {
      allLabel: "选择目标项目",
      preferredValue:
        selectedProject || document.getElementById("cluster-project-select").value,
      fallbackToSingleProject: true,
    },
  );
  projectsLoaded = true;
  updateArchiveButton();
}

async function syncNativeAgentThreads({
  force = false,
  silent = false,
  refresh = false,
  signal = undefined,
} = {}) {
  const now = Date.now();
  if (
    nativeAgentSyncInFlight
    || (!force && now - lastNativeAgentSyncAt < NATIVE_AGENT_SYNC_INTERVAL_MS)
  ) {
    return false;
  }
  nativeAgentSyncInFlight = true;
  lastNativeAgentSyncAt = now;
  if (!silent) {
    setActionStatus("agent-reset-status", "正在扫描 Codex Native Threads…", "pending");
  }
  try {
    const result = await requestJson(
      "/api/agents/sync-native",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 500 }),
        signal,
      },
      "sync native agents",
    );
    const registered = nonNegativeNumber(result.registered_count);
    const refreshed = nonNegativeNumber(result.refreshed_count);
    const changed = registered + refreshed;
    const errors = Array.isArray(result.errors) ? result.errors.length : 0;
    setActionStatus(
      "agent-reset-status",
      changed
        ? `Native Thread 已同步 · 新增 ${registered} · 更新 ${refreshed}${errors ? ` · ${errors} 个失败` : ""}`
        : `Native Thread 已同步${errors ? ` · ${errors} 个失败` : " · 无新增"}`,
      errors ? "error" : "success",
    );
    if (refresh && changed > 0) {
      await requestImmediateRefresh({ forceRender: true });
    }
    return changed > 0;
  } catch (error) {
    lastNativeAgentSyncAt = 0;
    if (error.name !== "AbortError") {
      setActionStatus(
        "agent-reset-status",
        `Native Thread 同步失败：${error.message}`,
        "error",
      );
    }
    return false;
  } finally {
    nativeAgentSyncInFlight = false;
  }
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

function resetDashboardRevisionCache() {
  lastBoardRevision = "";
  lastSummaryRevision = "";
  summaryEtag = "";
  boardEtag = "";
}

function syncProjectSelection(projectId, { refresh = true } = {}) {
  selectedProject = projectsById.has(projectId) ? projectId : "";
  const topSelect = document.getElementById("project-select");
  const clusterSelect = document.getElementById("cluster-project-select");
  if (topSelect && topSelect.value !== selectedProject) {
    topSelect.value = selectedProject;
  }
  if (clusterSelect && clusterSelect.value !== selectedProject) {
    clusterSelect.value = selectedProject;
  }
  closeDetail();
  resetDashboardRevisionCache();
  if (refresh) void requestImmediateRefresh();
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
  const claimed = task.status === "running"
    && task.claimed_by
    && !task.active_job
    ? `<span class="chip owner">认领 ${escapeHtml(task.claimed_by)}</span>`
    : "";
  const externalExecution = task.execution
    && task.execution.source !== "managed_job"
    ? `<span class="chip owner">${escapeHtml(task.execution.label || "执行中")} · ${escapeHtml(task.execution.display_name || task.execution.actor || "—")}</span>`
    : "";
  const claimWait = task.status === "ready" && task.claim_wait_seconds
    ? `<span class="chip ${escapeHtml(task.claim_wait_state || "normal")}">等待认领 ${escapeHtml(ageText(task.claim_wait_seconds))}</span>`
    : "";
  const workflowClass = task.workflow_epoch ? "" : " historical";
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
        <span class="chip workflow${workflowClass}">${escapeHtml(workflowEpochText(task.workflow_epoch))}</span>
        ${blocker}${lease}${assigned}${claimed}${externalExecution}${claimWait}${archived}
      </div>
      ${jobRuntimeHtml(task.active_job)}
    </article>`;
}

function summaryNumber(summary, keys, fallback) {
  for (const key of keys) {
    if (!summary || !Object.prototype.hasOwnProperty.call(summary, key)) {
      continue;
    }
    const value = summary[key];
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) return value.length;
    if (typeof value === "boolean") return value ? 1 : 0;
    const number = Number(value);
    if (Number.isFinite(number)) return number;
  }
  return fallback;
}

function legacyGoalError(goal) {
  const reason = String(goal?.last_error || "").trim();
  if (!reason || !["planning", "active", "failed"].includes(goal?.status)) {
    return "";
  }
  const userControlReason = /(?:pause|resume|cancel).*(?:requested|by user)|requested through dashboard/i;
  return userControlReason.test(reason) ? "" : reason;
}

function goalHealth(goal) {
  const rawHealth = goal?.health;
  const rawState = typeof rawHealth === "object" && rawHealth !== null
    ? rawHealth.state ?? rawHealth.status ?? rawHealth.level
    : rawHealth;
  const state = String(rawState ?? "").trim();
  const normalizedState = state.toLowerCase();
  const legacyError = legacyGoalError(goal);
  const explicitAttention = goal?.attention === true
    || Boolean(goal?.attention)
    || Boolean(legacyError)
    || (typeof rawHealth === "object"
      && rawHealth !== null
      && rawHealth.ok === false);
  const attention = explicitAttention
    || attentionHealthStates.has(normalizedState);
  const rawLabel = typeof rawHealth === "object" && rawHealth !== null
    ? rawHealth.label ?? rawHealth.message ?? rawHealth.summary
    : "";
  const labels = {
    healthy: "正常",
    ok: "正常",
    attention: "需要关注",
    warning: "警告",
    critical: "异常",
    error: "异常",
    failed: "失败",
    degraded: "降级",
    archived: "已归档",
    stalled: "已停滞",
    canceled: "已取消",
  };
  const statusLabels = {
    planning: "规划中",
    active: "运行中",
    paused: "已暂停",
    completed: "已完成",
    failed: "已终止",
    canceled: "已取消",
  };
  const label = String(
    rawLabel
      || labels[normalizedState]
      || (legacyError ? "执行异常" : "")
      || state.replaceAll("_", " ")
      || statusLabels[goal?.status]
      || (attention ? "需要关注" : "未知"),
  );
  return { attention, state, label };
}

function nextActionText(goal) {
  const value = goal?.next_action;
  if (value === undefined || value === null || value === "") {
    const fallbacks = {
      planning: "等待 Lead 拆分任务",
      active: "等待 Lead 继续调度",
      paused: "可恢复目标继续推进",
      completed: "目标已完成，无需操作",
      failed: "目标已终止",
      canceled: "目标已取消，无需操作",
    };
    return fallbacks[goal?.status] || "—";
  }
  if (value === "none") return "无";
  if (value === "inspect_stalled_goal") return "检查停滞原因";
  if (Array.isArray(value)) {
    return value
      .map((item) => nextActionText({ next_action: item }))
      .filter((item) => item !== "—")
      .join("；") || "—";
  }
  if (typeof value === "object") {
    return String(
      value.label
        ?? value.action
        ?? value.summary
        ?? value.description
        ?? value.message
        ?? "—",
    );
  }
  return String(value);
}

function buildClusterMetrics(data = {}) {
  const summary = data.cluster_summary || {};
  const runtime = data.cluster_runtime || {};
  const goals = Array.isArray(data.cluster_goals) ? data.cluster_goals : [];
  const jobs = Array.isArray(data.cluster_jobs) ? data.cluster_jobs : [];
  const activeGoals = goals.filter(
    (goal) => ["planning", "active"].includes(goal.status),
  ).length;
  const pausedGoals = goals.filter((goal) => goal.status === "paused").length;
  const attentionGoals = goals.filter((goal) => goalHealth(goal).attention).length;
  const activeWorkers = jobs.filter(
    (job) => job.kind === "worker"
      && ["starting", "running"].includes(job.status),
  ).length;
  const activeLeads = jobs.filter(
    (job) => job.kind === "lead"
      && ["starting", "running"].includes(job.status),
  ).length;
  const globalJobs = summaryNumber(
    summary,
    ["global_jobs", "active_workers"],
    activeWorkers,
  );
  const globalCapacity = summaryNumber(
    summary,
    ["global_capacity"],
    activeGoals * 3,
  );
  const capacityPerGoal = summaryNumber(
    summary,
    ["worker_capacity_per_goal"],
    3,
  );
  const queuedGoals = summaryNumber(summary, ["queued_goals"], 0);
  const heartbeatOverdue = summaryNumber(
    summary,
    ["heartbeat_overdue"],
    goals.filter(
      (goal) => ["warning", "critical"].includes(goalHeartbeatInfo(goal).state),
    ).length,
  );
  const oldestHeartbeatAge = summaryNumber(
    summary,
    ["oldest_heartbeat_age_seconds"],
    Math.max(
      0,
      ...goals.map((goal) => nonNegativeNumber(goal?.heartbeat_age_seconds)),
    ),
  );
  const fairnessWaitCount = summaryNumber(
    summary,
    ["fairness_wait_count"],
    goals.filter(
      (goal) => goalQueueReason(goal).state === "global_capacity",
    ).length,
  );
  const runtimeName = String(runtime.runtime || "unknown");
  const runtimeDetail = runtime.thread_reuse
    ? `复用 Thread${runtime.running ? " · daemon 在线" : " · daemon 未运行"}`
    : runtimeName === "exec"
      ? "独立进程 · 不保留 Thread"
      : "运行配置未知";
  const tick = runtime.tick && typeof runtime.tick === "object"
    ? runtime.tick
    : {};
  const failedPhases = Array.isArray(tick.failed_phases)
    ? tick.failed_phases.map(String).filter(Boolean)
    : [];
  const tickDegraded = Boolean(tick.degraded || failedPhases.length);
  const tickDetail = tickDegraded
    ? `降级 ${failedPhases.length ? failedPhases.join("/") : "tick"}${tick.error_code ? ` · ${tick.error_code}` : ""}`
    : runtimeDetail;
  return [
    {
      key: "jobs",
      label: "WORKER SLOTS",
      value: `${globalJobs}/${globalCapacity}`,
      detail: `${activeGoals} 主 Agent · 每会话 ${capacityPerGoal} Worker`,
      tone: globalCapacity > 0 && globalJobs >= globalCapacity
        ? "warning"
        : "",
    },
    {
      key: "queued",
      label: "QUEUED",
      value: queuedGoals,
      detail: `公平等待 ${fairnessWaitCount}`,
      tone: queuedGoals > 0 ? "warning" : "",
    },
    {
      key: "runtime",
      label: "RUNTIME",
      value: runtimeName,
      detail: tickDegraded
        ? tickDetail
        : runtime.config_mismatch
          ? "配置与 daemon 不一致"
          : runtimeDetail,
      tone: runtime.config_mismatch || tickDegraded ? "critical" : "",
    },
    {
      key: "heartbeat-overdue",
      label: "HB OVERDUE",
      value: heartbeatOverdue,
      detail: `最老 ${ageText(oldestHeartbeatAge)}`,
      tone: heartbeatOverdue > 0 ? "critical" : "",
    },
    {
      key: "goals",
      label: "GOALS",
      value: summaryNumber(
        summary,
        ["total", "total_goals", "goals_total", "goals", "goal_count"],
        goals.length,
      ),
    },
    {
      key: "active",
      label: "ACTIVE",
      value: summaryNumber(
        summary,
        ["active", "active_goals", "goals_active", "active_count", "running"],
        activeGoals,
      ),
    },
    {
      key: "paused",
      label: "PAUSED",
      value: summaryNumber(
        summary,
        ["paused", "paused_goals", "goals_paused", "paused_count"],
        pausedGoals,
      ),
    },
    {
      key: "attention",
      label: "ATTENTION",
      value: summaryNumber(
        summary,
        ["attention", "attention_goals", "attention_count"],
        attentionGoals,
      ),
    },
    {
      key: "leads",
      label: "LEADS",
      value: summaryNumber(
        summary,
        ["active_leads", "leads"],
        activeLeads,
      ),
    },
  ];
}

function renderBoard(data, { force = false } = {}) {
  const signature = buildBoardSignature(data);
  if (!force && signature === lastBoardSignature) return false;
  const boardTasks = Array.isArray(data.tasks) ? data.tasks : [];
  tasksById = new Map(boardTasks.map((task) => [task.task_id, task]));
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
    const tasks = boardTasks.filter((task) => task.status === lane);
    document.getElementById(`count-${lane}`).textContent = tasks.length;
    const container = document.getElementById(`cards-${lane}`);
    container.innerHTML = tasks.length
      ? tasks.map(renderCard).join("")
      : '<div class="empty">暂无任务</div>';
  }

  const counts = data.counts || {};
  const summary = [
    ["READY", counts.ready || 0],
    ["RUNNING", counts.running || 0],
    ["REVIEW", counts.review || 0],
    ["BLOCKED", counts.blocked || 0],
    ["DONE", counts.done || 0],
    ["PROJECTS", (data.projects || []).length],
    ["MESSAGES", (data.messages || []).length],
  ];
  document.getElementById("summary").innerHTML = summary
    .map(([label, value]) => `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`)
    .join("");

  renderCluster(data.cluster_goals || [], data.cluster_jobs || [], data);
  renderAgentThreads(
    data.cluster_agents || [],
    data.cluster_agent_events || [],
  );
  renderMessages(data.messages || []);
  updateArchiveButton();
  const detail = document.getElementById("detail");
  if (selectedTaskId && !detail.classList.contains("hidden")) {
    showDetail(selectedTaskId);
  }
  lastBoardSignature = signature;
  return true;
}

function renderCluster(goals, jobs, data) {
  const summaryContainer = document.getElementById("cluster-summary");
  summaryContainer.innerHTML = buildClusterMetrics(data)
    .map((metric) => `
      <div class="cluster-metric ${escapeHtml(metric.tone || (metric.key === "attention" && metric.value ? "attention" : ""))}"
           data-metric="${escapeHtml(metric.key)}">
        <strong>${escapeHtml(metric.value)}</strong>
        <span class="metric-copy">
          <span>${escapeHtml(metric.label)}</span>
          <small>${escapeHtml(metric.detail || "")}</small>
        </span>
      </div>`)
    .join("");

  const container = document.getElementById("cluster-overview");
  const selectedName = data.projects.find(
    (project) => project.project_id === data.selected_project_id,
  )?.name;
  if (!goals.length) {
    const scope = selectedName ? `“${escapeHtml(selectedName)}”项目` : "当前范围";
    container.innerHTML = `
      <div class="cluster-empty">
        ${scope}没有自动集群目标；可在上方创建，或继续使用下方普通任务泳道。
      </div>`;
    return;
  }
  const activeJobs = jobs.filter(
    (job) => ["starting", "running"].includes(job.status),
  );
  const cards = goals.map((goal) => {
    const health = goalHealth(goal);
    const goalJobs = activeJobs.filter((job) => job.goal_id === goal.goal_id);
    const heartbeat = goalHeartbeatInfo(goal);
    const queue = goalQueueReason(goal);
    const checkpoint = goal.checkpoint_updated_at
      ? `<span class="chip" title="${escapeHtml(goal.checkpoint_summary || "")}">checkpoint ${escapeHtml(timeText(goal.checkpoint_updated_at))}</span>`
      : "";
    const activeJobCount = nonNegativeNumber(
      goal.active_job_count,
      goalJobs.length,
    );
    const goalExternalExecutions = (data.tasks || [])
      .filter(
        (task) =>
          task.goal_id === goal.goal_id
          && task.execution
          && task.execution.source !== "managed_job",
      );
    const activeExecutionCount = nonNegativeNumber(
      goal.active_execution_count,
      activeJobCount + goalExternalExecutions.length,
    );
    const heartbeatAge = waitDurationText(goal.heartbeat_age_seconds);
    const estimatedWait = waitDurationText(goal.estimated_wait_seconds);
    const runText = [
      ...goalJobs.map(
        (job) => `${job.kind}:${job.role} PID ${job.pid || "—"}`,
      ),
      ...goalExternalExecutions.map(
        (task) =>
          `${task.execution.label}:${task.execution.display_name || task.execution.actor}`,
      ),
    ].join(" · ") || "无活动进程";
    const status = String(goal.status || "unknown");
    const controls = [];
    if (["planning", "active"].includes(status)) {
      controls.push(
        '<button class="goal-action" type="button" data-control-action="pause">暂停</button>',
      );
    }
    if (status === "paused") {
      controls.push(
        '<button class="goal-action resume" type="button" data-control-action="resume">恢复</button>',
      );
    }
    if (terminalGoalStatuses.has(status)) {
      controls.push(
        goal.archived_at
          ? '<button class="goal-action resume" type="button" data-control-action="restore" title="恢复目标，以及由该目标归档的 done 任务">恢复目标（含Done）</button>'
          : '<button class="goal-action" type="button" data-control-action="archive" title="归档目标及其 done 任务；不归档 Agent threads">归档目标（含Done）</button>',
      );
    }
    if (!terminalGoalStatuses.has(status)) {
      controls.push(
        '<button class="goal-action danger" type="button" data-control-action="cancel">取消</button>',
      );
    }
    const archivedClass = goal.archived_at ? " archived" : "";
    const workflowClass = goal.workflow_epoch ? "" : " historical";
    return `
      <article class="cluster-card ${health.attention ? "attention" : ""} heartbeat-${escapeHtml(heartbeat.tone)}${archivedClass}"
               data-heartbeat-state="${escapeHtml(heartbeat.state)}"
               data-queue-reason="${escapeHtml(queue.state)}"
               data-archived="${goal.archived_at ? "true" : "false"}"
               data-goal-id="${escapeHtml(goal.goal_id)}">
        <div class="card-top">
          <span class="task-id">${escapeHtml(goal.goal_id)}</span>
          <span class="kind">${escapeHtml(status)}</span>
        </div>
        <h3 title="${escapeHtml(goal.objective)}">${escapeHtml(goal.objective)}</h3>
        <div class="card-meta">
          <span class="chip">${escapeHtml(goal.project_name || "未知项目")}</span>
          <span class="chip owner">${escapeHtml(goal.autonomy)}</span>
          <span class="chip workflow${workflowClass}">${escapeHtml(workflowEpochText(goal.workflow_epoch))}</span>
          <span class="chip">lead ticks ${escapeHtml(goal.lead_ticks ?? 0)}</span>
          <span class="chip active-jobs-chip">执行中 ${escapeHtml(activeExecutionCount)} · ${escapeHtml(runText)}</span>
          ${checkpoint}
          ${goal.archived_at ? '<span class="chip archived">已归档</span>' : ""}
          ${health.attention ? '<span class="chip attention-chip">需关注</span>' : ""}
        </div>
        <div class="cluster-diagnostics">
          <div class="cluster-diagnostic heartbeat-${escapeHtml(heartbeat.tone)}">
            <span>心跳</span>
            <strong>${escapeHtml(heartbeat.label)}</strong>
            <small>${escapeHtml(heartbeat.state)} · ${escapeHtml(heartbeatAge)}</small>
          </div>
          <div class="cluster-diagnostic queue-${escapeHtml(queue.state)}">
            <span>队列</span>
            <strong>${escapeHtml(queue.label)}</strong>
            <small>${escapeHtml(queue.state)} · 当前等待 ${escapeHtml(estimatedWait)}</small>
          </div>
        </div>
        <div class="cluster-guidance">
          <span>目标健康</span>
          <strong class="${health.attention ? "attention" : ""}">${escapeHtml(health.label)}</strong>
          <span>下一步</span>
          <p>${escapeHtml(nextActionText(goal))}</p>
        </div>
        ${controls.length ? `<div class="cluster-actions">${controls.join("")}</div>` : ""}
      </article>`;
  }).join("");
  const activeExecutionCount = (
    (data.cluster_summary && data.cluster_summary.active_executions)
    ?? activeJobs.length
  );
  container.innerHTML = `<div class="cluster-title">${goals.length} 个目标 · ${escapeHtml(activeExecutionCount)} 个活动执行</div>${cards}`;
}

function renderAgentThreads(agents, events = []) {
  const safeAgents = Array.isArray(agents) ? agents : [];
  const safeEvents = Array.isArray(events) ? events : [];
  document.getElementById("agent-thread-count").textContent = safeAgents.length;
  const container = document.getElementById("agent-thread-list");
  if (!safeAgents.length) {
    container.innerHTML = '<div class="empty">本范围内暂无 Agent Thread</div>';
    return;
  }
  const latestEventByAgent = new Map();
  for (const event of safeEvents) {
    const current = latestEventByAgent.get(event.agent_id);
    const eventRank = Number(event.event_id);
    const currentRank = Number(current?.event_id);
    const eventTime = Date.parse(event.created_at);
    const currentTime = Date.parse(current?.created_at);
    const isNewer = Number.isFinite(eventRank) && Number.isFinite(currentRank)
      ? eventRank >= currentRank
      : Number.isFinite(eventTime) && Number.isFinite(currentTime)
        ? eventTime >= currentTime
        : !current;
    if (isNewer) {
      latestEventByAgent.set(event.agent_id, event);
    }
  }
  container.innerHTML = safeAgents.map((agent) => {
    const status = String(
      agent.effective_status || agent.status || "unknown",
    ).toLowerCase();
    const statusLabel = agentStatusLabels[status] || status;
    const workstream = agent.workstream_key ?? agent.workstream ?? "—";
    const thread = agent.thread_id ?? agent.thread ?? "—";
    const model = [agent.model, agent.model_provider]
      .filter((value) => value !== undefined && value !== null && value !== "")
      .join(" · ") || "模型未知";
    const useLatestJobUsage = agent.cache_usage_source === "latest_job";
    const cumulativeInput = agent.cumulative_input_tokens
      ?? agent.total_input_tokens;
    const cumulativeCached = agent.cumulative_cached_input_tokens
      ?? agent.total_cached_input_tokens;
    const cumulativeUncached = agent.cumulative_uncached_input_tokens
      ?? agent.total_uncached_input_tokens;
    const cumulativeOutput = agent.cumulative_output_tokens
      ?? agent.output_tokens;
    const cumulativeRate = agent.cumulative_cache_hit_rate
      ?? agent.cache_hit_rate;
    const cached = useLatestJobUsage
      ? agent.latest_job_cached_input_tokens
      : agent.last_cached_input_tokens
        ?? agent.total_cached_input_tokens;
    const uncached = useLatestJobUsage
      ? agent.latest_job_uncached_input_tokens
      : agent.last_uncached_input_tokens
        ?? agent.total_uncached_input_tokens;
    const contextTokens = useLatestJobUsage
      ? agent.latest_job_context_tokens
      : agent.context_tokens;
    const windowTokens = useLatestJobUsage
      ? agent.latest_job_model_context_window
      : agent.model_context_window;
    const usageScopeLabel = useLatestJobUsage ? "latest job" : "last request";
    const context = windowTokens !== undefined
      && windowTokens !== null
      && Number(windowTokens) > 0
      ? `${tokenText(contextTokens)} / ${tokenText(windowTokens)}`
      : tokenText(contextTokens);
    const taskLabel = agent.current_task_id
      ? `${agent.current_task_id}${agent.current_task_title ? ` · ${agent.current_task_title}` : ""}`
      : "未绑定任务";
    const goalLabel = agent.current_goal_id
      ? `${agent.current_goal_id}${agent.current_goal_objective ? ` · ${agent.current_goal_objective}` : ""}`
      : "未绑定目标";
    const latestEvent = latestEventByAgent.get(agent.agent_id);
    const latestJob = agent.latest_job_id
      ? `${agent.latest_job_id} · ${agent.latest_job_status || "unknown"}${agent.latest_job_thread_action ? ` · ${agent.latest_job_thread_action}` : ""}${agent.latest_job_workflow_epoch ? ` · ${workflowEpochText(agent.latest_job_workflow_epoch)}` : ""}`
      : "无 job";
    const nativePrompt = agent.runtime === "native"
      ? agent.prompt_status || "unknown"
      : "";
    const profileContract = agent.runtime === "native"
      ? ""
      : agent.profile_contract_status || "";
    const profileContractLabel = profileContract === "current"
      ? "contract current"
      : profileContract === "historical"
        ? "contract historical · not reusable"
        : "";
    const nativeIdentity = agent.runtime === "native"
      ? agent.agent_nickname || agent.agent_path || ""
      : "";
    const nativeLifecycle = agent.runtime === "native"
      ? nativeLifecycleLabels[String(agent.lifecycle || "unknown").toLowerCase()]
        || agent.lifecycle
        || "未知"
      : "";
    const identityDiagnostic = agent.runtime === "native"
      ? `${agent.identity_source || "unknown"} / ${agent.identity_confidence || "unknown"}`
      : "";
    const assignmentDiagnostic = agent.runtime === "native"
      ? `${agent.assignment_source || "none"} / ${agent.assignment_confidence || "unknown"}`
      : "";
    const nativeUsageStatus = agent.runtime === "native"
      ? agent.native_usage_status || "unavailable"
      : "";
    const nativeCandidates = Array.isArray(agent.task_candidates)
      ? agent.task_candidates
      : [];
    const turnText = agent.turn_count === null || agent.turn_count === undefined
      ? "—"
      : agent.turn_count;
    const resetState = agent.reset_requested_at
      ? `重置已请求 ${timeText(agent.reset_requested_at)}${agent.reset_reason ? ` · ${agent.reset_reason}` : ""}`
      : latestEvent
        ? `最近事件 ${latestEvent.event_type}`
        : "尚未请求重置";
    const resetBlockedReason = agent.archived_at
      ? "已归档不可重置"
      : agent.runtime === "native"
        ? "Native thread 仅支持 same-task，不能由 managed runtime 重置"
      : ["busy", "starting"].includes(status)
        ? "忙碌中不可重置"
        : status === "superseded"
          ? "已被替换不可重置"
          : "";
    const resetDisabled = Boolean(resetBlockedReason);
    return `
      <article class="agent-thread-card ${escapeHtml(status)}"
               data-agent-id="${escapeHtml(agent.agent_id)}"
               data-agent-status="${escapeHtml(status)}"
               data-agent-runtime="${escapeHtml(agent.runtime || "managed")}"
               data-agent-archived="${agent.archived_at ? "true" : "false"}">
        <div class="agent-thread-head">
          <strong>${escapeHtml(agent.display_name || agent.agent_id)}</strong>
          <span class="agent-status ${escapeHtml(status)}">${escapeHtml(statusLabel)}</span>
        </div>
        <div class="agent-thread-meta">
          <span>role ${escapeHtml(agent.role || "—")}</span>
          <span>runtime ${escapeHtml(agent.runtime || "managed")}</span>
          ${profileContractLabel
            ? `<span class="agent-profile ${escapeHtml(profileContract)}">${escapeHtml(profileContractLabel)}</span>`
            : ""}
          ${nativePrompt ? `<span>prompt ${escapeHtml(nativePrompt)}</span>` : ""}
          ${nativeIdentity ? `<span>agent ${escapeHtml(nativeIdentity)}</span>` : ""}
          ${nativeLifecycle
            ? `<span>lifecycle ${escapeHtml(nativeLifecycle)}${agent.session_stale ? " · stale" : ""}</span>`
            : ""}
          ${identityDiagnostic
            ? `<span>identity ${escapeHtml(identityDiagnostic)}</span>`
            : ""}
          ${assignmentDiagnostic
            ? `<span>assignment ${escapeHtml(assignmentDiagnostic)}${agent.task_assignment_ambiguous ? " · ambiguous" : ""}</span>`
            : ""}
          ${nativeUsageStatus
            ? `<span>usage ${escapeHtml(nativeUsageStatus)}</span>`
            : ""}
          <span>workstream ${escapeHtml(workstream)}</span>
          <span>thread ${escapeHtml(thread)}</span>
          <span>${escapeHtml(model)}</span>
          <span>turn ${escapeHtml(turnText)}</span>
          <span>last ${escapeHtml(timeText(agent.last_used_at))}</span>
          <span>latest job ${escapeHtml(latestJob)}</span>
          <span>${escapeHtml(usageScopeLabel)} cache/uncached ${escapeHtml(tokenText(cached))} / ${escapeHtml(tokenText(uncached))} · ${escapeHtml(cacheRateText(agent.last_cache_hit_rate, cached, uncached))}</span>
        </div>
        <div class="agent-thread-task">
          <div>任务：${escapeHtml(taskLabel)}</div>
          <div>当前目标：${escapeHtml(goalLabel)}</div>
          ${agent.runtime === "native" && agent.parent_thread_id
            ? `<div>父 Thread：${escapeHtml(agent.parent_thread_id)}</div>`
            : ""}
          ${agent.runtime === "native" && nativeCandidates.length
            ? `<div>任务候选：${escapeHtml(nativeCandidates.join(", "))}${agent.task_assignment_ambiguous ? " · 需人工确认" : ""}</div>`
            : ""}
        </div>
        <div class="agent-thread-usage">
          <div><span>累计 input</span><strong>${escapeHtml(tokenText(cumulativeInput))}</strong></div>
          <div><span>累计 cache</span><strong>${escapeHtml(cacheRateText(cumulativeRate, cumulativeCached, cumulativeUncached))} · ${escapeHtml(tokenText(cumulativeCached))}</strong></div>
          <div><span>累计 uncached / output</span><strong>${escapeHtml(tokenText(cumulativeUncached))} / ${escapeHtml(tokenText(cumulativeOutput))}</strong></div>
          <div><span>context</span><strong>${escapeHtml(context)}</strong></div>
        </div>
        <div class="agent-thread-actions">
          <small>${escapeHtml(resetState)}</small>
          <button class="agent-reset" type="button"
                  data-control-action="reset-agent"
                  data-agent-id="${escapeHtml(agent.agent_id)}"
                  data-agent-status="${escapeHtml(status)}"
                  data-agent-runtime="${escapeHtml(agent.runtime || "managed")}"
                  data-agent-archived="${agent.archived_at ? "true" : "false"}"
                  ${resetDisabled ? "disabled" : ""}>
            ${resetDisabled ? resetBlockedReason : "请求重置"}
          </button>
        </div>
      </article>`;
  }).join("");
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

function renderDetailTimeline(events) {
  return events.length
    ? events.map((event) => `
        <div class="timeline-row">
          <span>${escapeHtml(event.event_type)}</span>
          <strong>${escapeHtml(event.actor)}</strong>
          <time>${escapeHtml(timeText(event.created_at))}</time>
        </div>`).join("")
    : '<div class="empty">暂无事件</div>';
}

function renderTaskDetail(task, events) {
  const dependencies = task.blocked_by?.length
    ? task.blocked_by.map((item) => `${item.task_id} (${item.status})`).join(", ")
    : "无";
  const body = document.getElementById("detail-body");
  const orderedEvents = [...events].slice(-12).reverse();
  const runtime = task.active_job
    ? `${task.active_job.kind}:${task.active_job.role} · ${task.active_job.status} · PID ${task.active_job.pid || "—"}`
    : task.status === "ready"
      ? `等待认领 ${ageText(task.claim_wait_seconds)}`
      : "—";
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
      <dt>Review 目标快照</dt><dd>${escapeHtml(task.review_target_commit || "—")}</dd>
      <dt>Worker commit</dt><dd>${escapeHtml(task.commit_sha || "—")}</dd>
      <dt>创建时间</dt><dd>${escapeHtml(timeText(task.created_at))}</dd>
    </dl>
    <div class="detail-section"><h3>任务说明</h3><p>${escapeHtml(task.description || "—")}</p></div>
    <div class="detail-section"><h3>验收标准</h3><p>${escapeHtml(task.acceptance || "—")}</p></div>
    <div class="detail-section"><h3>结果</h3><p>${escapeHtml(task.result || "—")}</p></div>
    <div class="detail-section"><h3>测试</h3><p>${escapeHtml(task.tests || "—")}</p></div>
    <div class="detail-section"><h3>Review</h3><p>${escapeHtml(task.review_notes || "—")}</p></div>
    <div class="detail-section"><h3>交接事件</h3><div class="timeline">${renderDetailTimeline(orderedEvents)}</div></div>`;
  document.getElementById("detail").classList.remove("hidden");
  updateArchiveButton();
}

async function hydrateTaskDetailEvents(taskId, requestVersion) {
  const params = new URLSearchParams({ limit: "12" });
  try {
    const response = await requestRevisableJson(
      `/api/tasks/${encodeURIComponent(taskId)}/events?${params.toString()}`,
      { cache: "no-store" },
      "task events",
      detailEventsEtag,
    );
    if (
      requestVersion !== detailRequestVersion
      || selectedTaskId !== taskId
      || !tasksById.has(taskId)
    ) {
      return;
    }
    if (response.notModified) {
      return;
    }
    detailEventsEtag = response.etag;
    const events = Array.isArray(response.data?.events)
      ? response.data.events
      : [];
    const task = tasksById.get(taskId);
    renderTaskDetail(task, events);
    lastDetailSignature = JSON.stringify([task, events]);
    lastDetailTaskId = taskId;
  } catch (error) {
    if (requestVersion === detailRequestVersion && selectedTaskId === taskId) {
      const timeline = document.querySelector(".timeline");
      if (timeline) {
        timeline.innerHTML = `<div class="empty">事件读取失败：${escapeHtml(error.message)}</div>`;
      }
    }
  }
}

function showDetail(taskId) {
  const task = tasksById.get(taskId);
  if (!task) return;
  const switchingTask = selectedTaskId !== taskId;
  selectedTaskId = taskId;
  if (switchingTask) {
    detailEventsEtag = "";
    lastDetailSignature = "";
  }
  detailRequestVersion += 1;
  const requestVersion = detailRequestVersion;
  const events = eventsByTask.get(taskId) || [];
  // Rebuild the panel only when its content actually changed; periodic board
  // refreshes must not reset the reader's scroll position every 2 seconds.
  const signature = JSON.stringify([task, events]);
  if (switchingTask || signature !== lastDetailSignature) {
    lastDetailSignature = signature;
    lastDetailTaskId = taskId;
    renderTaskDetail(task, events);
  }
  void hydrateTaskDetailEvents(taskId, requestVersion);
}

function closeDetail() {
  detailRequestVersion += 1;
  selectedTaskId = "";
  lastDetailSignature = "";
  lastDetailTaskId = "";
  detailEventsEtag = "";
  document.getElementById("detail").classList.add("hidden");
  updateArchiveButton();
}

function buildBoardSignature(data) {
  if (data?.signature || data?.response_signature) {
    return String(data.signature || data.response_signature);
  }
  const normalized = JSON.parse(JSON.stringify(data ?? {}));
  delete normalized.generated_at;
  if (normalized.cluster_summary) {
    for (const field of [
      "oldest_claim_wait_seconds",
      "oldest_heartbeat_age_seconds",
    ]) {
      normalized.cluster_summary[field] = timeDerivedBucket(
        normalized.cluster_summary[field],
      );
    }
  }
  const runtimeTick = normalized.cluster_runtime?.tick;
  if (runtimeTick && typeof runtimeTick === "object") {
    normalized.cluster_runtime.tick = {
      degraded: Boolean(runtimeTick.degraded),
      failed_phases: Array.isArray(runtimeTick.failed_phases)
        ? runtimeTick.failed_phases.map(String)
        : [],
      error_code: String(runtimeTick.error_code || ""),
    };
  }
  for (const task of normalized.tasks || []) {
    task.claim_wait_seconds = timeDerivedBucket(task.claim_wait_seconds);
  }
  for (const goal of normalized.cluster_goals || []) {
    goal.heartbeat_age_seconds = timeDerivedBucket(
      goal.heartbeat_age_seconds,
    );
    goal.estimated_wait_seconds = timeDerivedBucket(
      goal.estimated_wait_seconds,
    );
  }
  return JSON.stringify(normalized);
}

function backoffDelay(failures) {
  return Math.min(
    POLL_INTERVAL_MS * (2 ** Math.max(0, failures - 1)),
    MAX_POLL_BACKOFF_MS,
  );
}

function clearPollTimer() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function schedulePoll(delay) {
  clearPollTimer();
  if (document.hidden) return;
  pollTimer = window.setTimeout(() => {
    pollTimer = null;
    void pollBoard();
  }, delay);
}

function pollStatusText({
  changed,
  recovered = false,
  now = new Date(),
} = {}) {
  const state = changed ? "已同步" : "已检查 · 无变化";
  return `${state} ${now.toLocaleTimeString("zh-CN")}${recovered ? " · 已恢复" : ""}`;
}

async function pollBoard({ forceRender = false } = {}) {
  clearPollTimer();
  if (document.hidden) return false;
  if (pollInFlight) {
    refreshQueued = true;
    queuedForceRender = queuedForceRender || forceRender;
    return false;
  }
  pollInFlight = true;
  activeRequest = new AbortController();
  let boardRendered = false;
  try {
    if (!projectsLoaded) {
      await loadProjects(activeRequest.signal);
    }
    await syncNativeAgentThreads({ silent: true, signal: activeRequest.signal });
    const params = new URLSearchParams();
    if (selectedProject) params.set("project_id", selectedProject);
    if (includeArchived) params.set("include_archived", "1");
    const suffix = params.size ? `?${params.toString()}` : "";
    const summaryResponse = await requestRevisableJson(
      `/api/summary${suffix}`,
      { cache: "no-store", signal: activeRequest.signal },
      "summary",
      summaryEtag,
    );
    let revision = lastSummaryRevision;
    if (!summaryResponse.notModified) {
      summaryEtag = summaryResponse.etag;
      revision = String(summaryResponse.data.revision || "");
      lastSummaryRevision = revision;
    } else if (!revision) {
      summaryEtag = "";
      throw new Error("summary returned 304 without a cached revision");
    }
    const boardRequired = forceRender
      || !lastBoardRevision
      || revision !== lastBoardRevision
      || tasksById.size === 0;
    if (boardRequired) {
      const boardResponse = await requestRevisableJson(
        `/api/board${suffix}`,
        { cache: "no-store", signal: activeRequest.signal },
        "board",
        forceRender || tasksById.size === 0 ? "" : boardEtag,
      );
      if (!boardResponse.notModified) {
        const data = boardResponse.data;
        boardEtag = boardResponse.etag;
        boardRendered = renderBoard(data, { force: forceRender });
        lastBoardRevision = String(data.revision || revision);
      } else {
        lastBoardRevision = revision;
      }
    }
    const downBefore = pollFailures > 0;
    pollFailures = 0;
    setConnectionState(
      pollStatusText({
        changed: boardRendered,
        recovered: downBefore,
      }),
      "ok",
    );
    schedulePoll(POLL_INTERVAL_MS);
    return true;
  } catch (error) {
    if (error.name === "AbortError") return false;
    pollFailures += 1;
    const delay = backoffDelay(pollFailures);
    setConnectionState(
      `连接失败：${error.message} · ${Math.round(delay / 1000)} 秒后重试`,
      "error",
    );
    schedulePoll(delay);
    return false;
  } finally {
    pollInFlight = false;
    activeRequest = null;
    if (refreshQueued) {
      const force = queuedForceRender;
      refreshQueued = false;
      queuedForceRender = false;
      if (!document.hidden) {
        clearPollTimer();
        pollTimer = window.setTimeout(() => {
          pollTimer = null;
          void pollBoard({ forceRender: force });
        }, 0);
      }
    }
  }
}

function requestImmediateRefresh({ forceRender = false } = {}) {
  clearPollTimer();
  return pollBoard({ forceRender });
}

function handleVisibilityChange() {
  if (document.hidden) {
    clearPollTimer();
    refreshQueued = false;
    queuedForceRender = false;
    activeRequest?.abort();
    setConnectionState("页面隐藏 · 轮询已暂停", "paused");
    return;
  }
  refreshQueued = false;
  resetDashboardRevisionCache();
  void pollBoard();
}

async function toggleArchiveTarget() {
  const task = selectedTaskId ? tasksById.get(selectedTaskId) : null;
  const project = projectsById.get(selectedProject);
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
    await requestJson(
      endpoint,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      "archive",
    );
    if (task && !archived && !includeArchived) closeDetail();
    if (!task && !archived && !includeArchived) selectedProject = "";
    projectsLoaded = false;
    await requestImmediateRefresh({ forceRender: true });
  } catch (error) {
    setConnectionState(`归档失败：${error.message}`, "error");
  } finally {
    updateArchiveButton();
  }
}

function createClusterPayload(projectId, objective, autonomy = "auto") {
  return {
    project_id: projectId,
    objective: objective.trim(),
    autonomy,
    max_worker_attempts: 3,
    max_lead_attempts: 5,
  };
}

async function handleCreateCluster(event) {
  event.preventDefault();
  const projectId = document.getElementById("cluster-project-select").value;
  const objective = document.getElementById("cluster-objective").value.trim();
  const autonomy = document.getElementById("cluster-autonomy").value;
  if (!projectId || !objective) {
    setActionStatus("cluster-create-status", "请选择项目并填写目标。", "error");
    return;
  }
  const button = document.getElementById("cluster-create");
  button.disabled = true;
  setActionStatus("cluster-create-status", "正在创建目标…", "pending");
  try {
    const result = await requestJson(
      "/api/clusters",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(createClusterPayload(projectId, objective, autonomy)),
      },
      "create cluster",
    );
    document.getElementById("cluster-objective").value = "";
    setActionStatus(
      "cluster-create-status",
      `目标 ${result.goal?.goal_id || ""} 已创建`,
      "success",
    );
    await requestImmediateRefresh();
  } catch (error) {
    setActionStatus(
      "cluster-create-status",
      `创建失败：${error.message}`,
      "error",
    );
  } finally {
    button.disabled = false;
  }
}

async function handleClusterControl(button) {
  const action = button.dataset.controlAction;
  const card = button.closest(".cluster-card");
  const goalId = card?.dataset.goalId;
  if (
    !goalId
    || !["pause", "resume", "cancel", "archive", "restore"].includes(action)
  ) return;
  if (
    action === "cancel"
    && !window.confirm(`取消目标 ${goalId}？未完成任务会保留。`)
  ) {
    return;
  }
  if (
    action === "archive"
    && !window.confirm(
      `归档目标 ${goalId}？该目标下的 done 任务会一起归档，但 Agent threads 会保留并继续按复用规则工作。`,
    )
  ) {
    return;
  }
  const labels = {
    pause: "正在暂停…",
    resume: "正在恢复…",
    cancel: "正在取消…",
    archive: "正在归档目标…",
    restore: "正在恢复目标…",
  };
  button.disabled = true;
  setActionStatus("cluster-action-status", labels[action], "pending");
  try {
    const endpoint = ["archive", "restore"].includes(action)
      ? "/api/clusters/archive"
      : "/api/clusters/control";
    const payload = ["archive", "restore"].includes(action)
      ? { goal_id: goalId, archived: action === "archive" }
      : { goal_id: goalId, action };
    await requestJson(
      endpoint,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
      "cluster control",
    );
    const completedLabels = {
      pause: "目标已暂停",
      resume: "目标已恢复",
      cancel: "目标已取消",
    archive: "目标及已完成任务已归档",
    restore: "目标及目标归档任务已恢复",
    };
    setActionStatus(
      "cluster-action-status",
      `${goalId} · ${completedLabels[action]}`,
      "success",
    );
    await requestImmediateRefresh();
  } catch (error) {
    setActionStatus(
      "cluster-action-status",
      `${goalId} · 操作失败：${error.message}`,
      "error",
    );
  } finally {
    button.disabled = false;
  }
}

async function handleAgentReset(button) {
  const agentId = button.dataset.agentId;
  const status = String(button.dataset.agentStatus || "").toLowerCase();
  const runtime = String(button.dataset.agentRuntime || "managed").toLowerCase();
  if (
    !agentId
    || runtime === "native"
    || ["busy", "starting", "superseded"].includes(status)
    || button.dataset.agentArchived === "true"
  ) return;
  if (!window.confirm(`请求重置 Agent Thread ${agentId}？此操作只写入重置请求。`)) {
    return;
  }
  button.disabled = true;
  setActionStatus("agent-reset-status", `${agentId} · 正在写入重置请求…`, "pending");
  try {
    const result = await requestJson(
      "/api/agents/reset",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: agentId,
          reason: "manual reset requested through dashboard",
        }),
      },
      "reset agent",
    );
    setActionStatus(
      "agent-reset-status",
      `${result.agent?.agent_id || agentId} · 重置请求已写入`,
      "success",
    );
    await requestImmediateRefresh();
  } catch (error) {
    setActionStatus(
      "agent-reset-status",
      `${agentId} · 重置失败：${error.message}`,
      "error",
    );
  } finally {
    button.disabled = false;
  }
}

function initializeDashboard() {
  const board = document.getElementById("board");
  if (!board) return;
  board.addEventListener("click", (event) => {
    const card = event.target.closest(".card");
    if (card) showDetail(card.dataset.taskId);
  });
  document.getElementById("cluster-overview").addEventListener("click", (event) => {
    const button = event.target.closest("[data-control-action]");
    if (button) void handleClusterControl(button);
  });
  document.getElementById("agent-threads").addEventListener("click", (event) => {
    const button = event.target.closest('[data-control-action="reset-agent"]');
    if (button) void handleAgentReset(button);
  });
  document.getElementById("agent-threads").addEventListener("toggle", (event) => {
    if (event.target.open) {
      void syncNativeAgentThreads({
        force: true,
        refresh: true,
      });
    }
  });
  document.getElementById("cluster-create-form").addEventListener(
    "submit",
    handleCreateCluster,
  );
  document.getElementById("project-select").addEventListener("change", (event) => {
    syncProjectSelection(event.target.value);
  });
  document.getElementById("cluster-project-select").addEventListener("change", (event) => {
    syncProjectSelection(event.target.value);
  });
  document.getElementById("show-archived").addEventListener("change", (event) => {
    closeDetail();
    includeArchived = event.target.checked;
    const project = projectsById.get(selectedProject);
    if (!includeArchived && project?.archived_at) selectedProject = "";
    projectsLoaded = false;
    resetDashboardRevisionCache();
    void requestImmediateRefresh();
  });
  document.getElementById("archive-target").addEventListener(
    "click",
    toggleArchiveTarget,
  );
  document.getElementById("detail-close").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDetail();
  });
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("resize", syncTopbarHeight);

  syncTopbarHeight();
  void pollBoard({ forceRender: true });
}

if (typeof window !== "undefined") {
  window.TeamBoardTestHooks = Object.freeze({
    ageText,
    backoffDelay,
    buildBoardSignature,
    buildClusterMetrics,
    createClusterPayload,
    goalHeartbeatInfo,
    goalHealth,
    goalQueueReason,
    jobRuntimeHtml,
    nextActionText,
    renderBoard,
    pollStatusText,
    renderAgentThreads,
    requestRevisableJson,
    summaryNumber,
    timeDerivedBucket,
    tokenText,
    waitDurationText,
    workflowEpochText,
  });
}

initializeDashboard();
