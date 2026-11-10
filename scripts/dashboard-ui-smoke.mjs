import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const root = new URL("../", import.meta.url);
const [html, app, css] = await Promise.all([
  readFile(new URL("src/codex_team/web/index.html", root), "utf8"),
  readFile(new URL("src/codex_team/web/app.js", root), "utf8"),
  readFile(new URL("src/codex_team/web/style.css", root), "utf8"),
]);

function expectText(source, text, label) {
  assert.ok(source.includes(text), `${label} missing: ${text}`);
}

function expectPattern(source, pattern, label) {
  assert.match(source, pattern, `${label} does not match ${pattern}`);
}

function between(source, start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert.ok(first >= 0 && last > first, `cannot isolate ${start}`);
  return source.slice(first, last);
}

const requiredDomIds = [
  "cluster-create-form",
  "cluster-project-select",
  "cluster-objective",
  "cluster-autonomy",
  "cluster-create",
  "cluster-create-status",
  "cluster-summary",
  "cluster-overview",
  "cluster-action-status",
  "agent-threads",
  "agent-thread-count",
  "agent-thread-list",
  "agent-reset-status",
  "project-select",
  "show-archived",
  "archive-target",
  "detail",
  "detail-body",
];
for (const id of requiredDomIds) {
  expectText(html, `id="${id}"`, "index.html");
}

expectText(html, "创建目标", "index.html");
expectText(app, "暂停", "app.js");
expectText(app, "恢复", "app.js");
expectText(app, "取消", "app.js");
expectText(app, "JSON.stringify(payload)", "app.js");
expectText(app, "/api/clusters/control", "app.js");
expectText(app, "/api/clusters/archive", "app.js");
expectText(app, "/api/agents/reset", "app.js");
expectText(app, "/api/board", "app.js");
expectText(app, "/api/projects", "app.js");
expectText(app, "data.cluster_summary", "app.js");
expectText(app, "goal?.health", "app.js");
expectText(app, "goal?.next_action", "app.js");
expectText(
  between(app, "const labels = {", "const statusLabels = {"),
  'archived: "已归档"',
  "app.js goal health labels",
);
expectText(
  between(app, "const labels = {", "const statusLabels = {"),
  'stalled: "已停滞"',
  "app.js stalled health label",
);
expectText(app, 'if (value === "none") return "无";', "app.js");
expectText(
  app,
  'if (value === "inspect_stalled_goal") return "检查停滞原因";',
  "app.js stalled next action",
);
expectText(app, 'allLabel: "选择目标项目"', "app.js");
expectPattern(
  app,
  /function\s+syncProjectSelection\s*\(/,
  "cluster project selector sync",
);
expectText(
  app,
  'document.getElementById("cluster-project-select").addEventListener("change"',
  "cluster project selector change handler",
);
expectText(css, ".cluster-card.attention", "style.css");
expectText(css, ".cluster-actions", "style.css");
expectText(css, ".cluster-card.heartbeat-warning", "style.css");
expectText(css, ".cluster-card.heartbeat-critical", "style.css");
expectText(css, ".cluster-card.archived", "style.css");
expectText(css, ".cluster-diagnostic.heartbeat-warning", "style.css");
expectText(css, ".cluster-diagnostic.heartbeat-critical", "style.css");
expectText(css, ".agent-thread-card.busy", "style.css");
expectText(css, ".agent-profile.historical", "style.css");
expectText(css, ".chip.workflow.historical", "style.css");
expectText(css, ".task-runtime", "style.css");
const agentThreadsMarkup = between(
  html,
  '<details id="agent-threads"',
  "</details>",
);
assert.doesNotMatch(
  agentThreadsMarkup,
  /\sopen(?:\s|=|>)/,
  "Agent Threads must be collapsed by default",
);
assert.doesNotMatch(
  app,
  /agent\.(?:messages|transcript|conversation)/,
  "Agent diagnostics must not render conversation content",
);
for (const field of [
  "global_jobs",
  "global_capacity",
  "queued_goals",
  "heartbeat_overdue",
  "oldest_heartbeat_age_seconds",
  "fairness_wait_count",
  "active_job_count",
  "heartbeat_state",
  "heartbeat_age_seconds",
  "queue_reason",
  "estimated_wait_seconds",
  "cluster_agents",
  "cluster_agent_events",
  "cache_hit_rate",
  "uncached_input_tokens",
]) {
  expectText(app, field, "app.js");
}

const clusterSummaryCss = between(
  css,
  ".cluster-summary {",
  ".cluster-metric {",
);
expectText(clusterSummaryCss, "repeat(8, minmax(0, 1fr))", "cluster metrics");
const boardCss = between(css, ".board {", ".lane {");
expectText(boardCss, "repeat(5, minmax(0, 1fr))", "board layout");

expectPattern(app, /function\s+buildBoardSignature\s*\(/, "app.js");
expectPattern(app, /function\s+schedulePoll\s*\(/, "app.js");
expectPattern(app, /async\s+function\s+pollBoard\s*\(/, "app.js");
expectPattern(app, /function\s+handleVisibilityChange\s*\(/, "app.js");

const pollBoardSource = between(
  app,
  "async function pollBoard",
  "function requestImmediateRefresh",
);
assert.ok(
  !pollBoardSource.includes("/api/projects"),
  "pollBoard must not fetch projects after the one-time project load",
);
assert.ok(pollBoardSource.includes("/api/board"), "pollBoard must fetch the board");

const visibilitySource = between(
  app,
  "function handleVisibilityChange",
  "async function toggleArchiveTarget",
);
expectText(visibilitySource, "document.hidden", "visibility handler");
expectText(visibilitySource, "activeRequest?.abort()", "visibility handler");
expectText(visibilitySource, "void pollBoard()", "visibility handler");

const scheduleSource = between(
  app,
  "function schedulePoll",
  "async function pollBoard",
);
expectText(scheduleSource, "window.setTimeout", "poll scheduler");
expectText(scheduleSource, "document.hidden", "poll scheduler");

const documentObject = { getElementById: () => null };
const window = {};
const context = vm.createContext({
  window,
  document: documentObject,
  console,
  URLSearchParams,
  AbortController,
  setTimeout,
  clearTimeout,
});
vm.runInContext(app, context, { filename: "src/codex_team/web/app.js" });
const hooks = window.TeamBoardTestHooks;
assert.ok(hooks, "app.js did not expose test hooks");

function fakeElement() {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    disabled: false,
    classList: {
      contains: (name) => classes.has(name),
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      toggle: (name, force) => {
        const enabled = force ?? !classes.has(name);
        if (enabled) classes.add(name);
        else classes.delete(name);
        return enabled;
      },
    },
  };
}

const fakeElements = new Map();
for (const lane of ["ready", "running", "review", "blocked", "done"]) {
  fakeElements.set(`count-${lane}`, fakeElement());
  fakeElements.set(`cards-${lane}`, fakeElement());
}
for (const id of [
  "summary",
  "cluster-summary",
  "cluster-overview",
  "agent-thread-count",
  "agent-thread-list",
  "agent-reset-status",
  "message-count",
  "messages",
  "archive-target",
  "detail",
]) {
  fakeElements.set(id, fakeElement());
}
fakeElements.get("detail").classList.add("hidden");
documentObject.getElementById = (id) => fakeElements.get(id) || null;

const firstBoard = {
  generated_at: "2026-11-07T10:00:00Z",
  tasks: [{
    task_id: "T-1",
    status: "ready",
    title: "实现真实消费者",
    kind: "write",
    owner_role: "team_worker",
    priority: 1,
    workflow_epoch: "workflow-v2:taskcohort1234567890",
    project_name: "Demo",
    blocked_by: [],
    claim_wait_seconds: 62,
    claim_wait_state: "warning",
    active_job: {
      job_id: "J-TASK-1",
      kind: "worker",
      role: "team_worker",
      status: "running",
      runtime: "app_server",
      workflow_epoch: "workflow-v2:taskcohort1234567890",
      thread_id: "thread-task-1",
      turn_id: "turn-3",
      cached_input_tokens: 1500,
      uncached_input_tokens: 500,
      context_tokens: 12000,
      model_context_window: 128000,
      cache_hit_rate: 0.75,
    },
  }],
  counts: { ready: 1 },
  projects: [{
    project_id: "project-1",
    name: "Demo",
    archived_at: null,
  }],
  selected_project_id: "project-1",
  messages: [],
  events: [],
  cluster_summary: {
    goals_total: 5,
    goals_active: 2,
    goals_paused: 1,
    goals_failed: 0,
    active_jobs: 3,
    active_workers: 2,
    active_leads: 1,
    attention_count: 2,
    global_jobs: 2,
    global_capacity: 6,
    worker_capacity_per_goal: 3,
    system_jobs: 3,
    system_capacity: 16,
    queued_goals: 2,
    heartbeat_overdue: 2,
    oldest_heartbeat_age_seconds: 960,
    fairness_wait_count: 1,
  },
  cluster_runtime: {
    runtime: "app_server",
    thread_reuse: true,
    running: true,
    config_mismatch: false,
    version: "0.5.0",
  },
  cluster_goals: [
    {
      goal_id: "G-WARNING",
      status: "active",
      objective: "验证两个并发目标的警告心跳与全局容量队列",
      project_name: "Demo",
      autonomy: "auto",
      workflow_epoch: "workflow-v2:goalcohort1234567890",
      lead_ticks: 2,
      health: { state: "warning", label: "Worker 心跳延迟" },
      next_action: { action: "检查 Worker 进度" },
      active_job_count: 2,
      heartbeat_state: "warning",
      heartbeat_age_seconds: 320,
      checkpoint_summary: "Use fresh subagents and durable checkpoints.",
      checkpoint_updated_at: "2026-11-07T09:55:00Z",
      queue_reason: "global_capacity",
      estimated_wait_seconds: 120,
    },
    {
      goal_id: "G-CRITICAL",
      status: "active",
      objective: "验证关键心跳、目标容量排队与完整卡片展示",
      project_name: "Demo",
      autonomy: "supervised",
      workflow_epoch: "workflow-v2:goalcohort1234567890",
      lead_ticks: 5,
      health: { state: "critical", label: "心跳严重超时" },
      next_action: { action: "排查卡住的 Worker" },
      active_job_count: 1,
      heartbeat_state: "critical",
      heartbeat_age_seconds: 960,
      queue_reason: "goal_capacity",
      estimated_wait_seconds: 300,
    },
    {
      goal_id: "G-PAUSED",
      status: "paused",
      objective: "保留暂停目标控制",
      project_name: "Demo",
      autonomy: "supervised",
      workflow_epoch: "workflow-v2:goalcohort1234567890",
      lead_ticks: 1,
      active_job_count: 0,
      heartbeat_state: "idle",
      heartbeat_age_seconds: 0,
      queue_reason: "none",
      estimated_wait_seconds: 0,
    },
    {
      goal_id: "G-DONE",
      status: "completed",
      objective: "可归档的已完成目标",
      project_name: "Demo",
      autonomy: "auto",
      workflow_epoch: "workflow-v2:goalcohort1234567890",
      lead_ticks: 4,
      active_job_count: 0,
      heartbeat_state: "idle",
      heartbeat_age_seconds: 0,
      queue_reason: "none",
      estimated_wait_seconds: 0,
      archived_at: null,
    },
    {
      goal_id: "G-ARCHIVED",
      status: "failed",
      objective: "已归档且可恢复的目标",
      project_name: "Demo",
      autonomy: "auto",
      workflow_epoch: "workflow-v2:goalcohort1234567890",
      lead_ticks: 2,
      active_job_count: 0,
      heartbeat_state: "idle",
      heartbeat_age_seconds: 0,
      queue_reason: "none",
      estimated_wait_seconds: 0,
      archived_at: "2026-11-07T09:00:00Z",
    },
  ],
  cluster_jobs: [
    {
      goal_id: "G-WARNING",
      kind: "lead",
      role: "team_lead",
      status: "running",
      pid: 4320,
    },
    {
      goal_id: "G-WARNING",
      kind: "worker",
      role: "team_worker",
      status: "running",
      pid: 4321,
    },
    {
      goal_id: "G-CRITICAL",
      kind: "worker",
      role: "team_worker",
      status: "starting",
      pid: 4322,
    },
  ],
  cluster_agents: [
    {
      agent_id: "A-BUSY",
      display_name: "Diagnostic Worker",
      status: "busy",
      role: "team_worker",
      profile_contract_status: "current",
      workstream_key: "webui-agent-diagnostics",
      thread_id: "thread-agent-busy",
      current_goal_id: "G-WARNING",
      current_goal_objective: "验证两个并发目标的警告心跳与全局容量队列",
      current_task_id: "T-1",
      current_task_title: "实现真实消费者",
      model: "test-model",
      model_provider: "test-provider",
      model_context_window: 128000,
      context_tokens: 48000,
      last_cached_input_tokens: 1200,
      last_uncached_input_tokens: 400,
      total_cached_input_tokens: 2400,
      total_uncached_input_tokens: 800,
      cache_hit_rate: 0.75,
      latest_job_workflow_epoch: "workflow-v2:taskcohort1234567890",
      turn_count: 6,
      last_used_at: "2026-11-07T10:00:00Z",
      reset_requested_at: null,
    },
    {
      agent_id: "A-IDLE",
      display_name: "Idle Reviewer",
      status: "idle",
      role: "team_reviewer",
      profile_contract_status: "current",
      workstream_key: "review-queue",
      thread_id: "thread-agent-idle",
      current_goal_id: null,
      current_task_id: null,
      model: "test-model",
      model_provider: "test-provider",
      model_context_window: 64000,
      context_tokens: 0,
      last_cached_input_tokens: 0,
      last_uncached_input_tokens: 0,
      total_cached_input_tokens: 0,
      total_uncached_input_tokens: 0,
      cache_hit_rate: 0,
      turn_count: 2,
      last_used_at: "2026-11-07T09:55:00Z",
      reset_requested_at: null,
    },
    {
      agent_id: "A-SUPERSEDED",
      display_name: "Superseded Explorer",
      status: "superseded",
      role: "team_explorer",
      profile_contract_status: "historical",
      workstream_key: "retired",
      thread_id: "thread-agent-superseded",
      current_goal_id: null,
      current_task_id: null,
      model: "test-model",
      model_provider: "test-provider",
      context_tokens: 0,
      last_cached_input_tokens: 0,
      last_uncached_input_tokens: 0,
      total_cached_input_tokens: 0,
      total_uncached_input_tokens: 0,
      cache_hit_rate: 0,
      turn_count: 1,
      reset_requested_at: null,
      archived_at: null,
    },
    {
      agent_id: "A-ARCHIVED",
      display_name: "Archived Reviewer",
      status: "stored",
      role: "team_reviewer",
      profile_contract_status: "historical",
      workstream_key: "archived",
      thread_id: "thread-agent-archived",
      current_goal_id: null,
      current_task_id: null,
      model: "test-model",
      model_provider: "test-provider",
      context_tokens: 0,
      last_cached_input_tokens: 0,
      last_uncached_input_tokens: 0,
      total_cached_input_tokens: 0,
      total_uncached_input_tokens: 0,
      cache_hit_rate: 0,
      turn_count: 1,
      reset_requested_at: null,
      archived_at: "2026-11-07T09:00:00Z",
    },
  ],
  cluster_agent_events: [
    {
      event_id: 1,
      agent_id: "A-BUSY",
      event_type: "tokens_updated",
      actor: "app_server",
      data: { turn_id: "turn-3" },
      created_at: "2026-11-07T10:00:00Z",
    },
    {
      event_id: 2,
      agent_id: "A-IDLE",
      event_type: "reset_requested",
      actor: "human",
      data: { reason: "manual reset" },
      created_at: "2026-11-07T10:00:01Z",
    },
  ],
};
const secondBoard = JSON.parse(JSON.stringify(firstBoard));
secondBoard.generated_at = "2026-11-07T10:00:02Z";
secondBoard.tasks[0].claim_wait_seconds = 64;
secondBoard.tasks[0].claim_wait_state = "warning";
secondBoard.cluster_summary.oldest_heartbeat_age_seconds = 962;
secondBoard.cluster_goals[0].heartbeat_age_seconds = 322;
secondBoard.cluster_goals[0].estimated_wait_seconds = 122;
secondBoard.cluster_goals[1].heartbeat_age_seconds = 962;
secondBoard.cluster_goals[1].estimated_wait_seconds = 302;

assert.equal(
  hooks.buildBoardSignature(firstBoard),
  hooks.buildBoardSignature(secondBoard),
  "generated time and age-only changes must not force a redraw",
);
const thresholdBoard = JSON.parse(JSON.stringify(secondBoard));
thresholdBoard.tasks[0].claim_wait_state = "critical";
assert.notEqual(
  hooks.buildBoardSignature(firstBoard),
  hooks.buildBoardSignature(thresholdBoard),
  "claim-wait severity changes must trigger a redraw",
);
const heartbeatThresholdBoard = JSON.parse(JSON.stringify(secondBoard));
heartbeatThresholdBoard.cluster_goals[0].heartbeat_state = "critical";
assert.notEqual(
  hooks.buildBoardSignature(firstBoard),
  hooks.buildBoardSignature(heartbeatThresholdBoard),
  "heartbeat severity changes must trigger a redraw",
);
const agentTokenBoard = JSON.parse(JSON.stringify(secondBoard));
agentTokenBoard.cluster_agents[0].context_tokens = 52000;
agentTokenBoard.cluster_agents[0].cache_hit_rate = 0.8;
assert.notEqual(
  hooks.buildBoardSignature(firstBoard),
  hooks.buildBoardSignature(agentTokenBoard),
  "Agent token diagnostics must trigger a redraw",
);

assert.equal(hooks.renderBoard(firstBoard), true, "first board should render");
const clusterHtmlBefore = fakeElements.get("cluster-overview").innerHTML;
const summaryHtml = fakeElements.get("cluster-summary").innerHTML;
const agentThreadHtml = fakeElements.get("agent-thread-list").innerHTML;
const taskCardHtml = fakeElements.get("cards-ready").innerHTML;
assert.match(summaryHtml, /data-metric="jobs"/);
assert.match(summaryHtml, /data-metric="queued"/);
assert.match(summaryHtml, /data-metric="runtime"/);
assert.match(summaryHtml, /data-metric="heartbeat-overdue"/);
assert.match(summaryHtml, /2\/6/);
assert.match(summaryHtml, /QUEUED/);
assert.match(summaryHtml, /HB OVERDUE/);
assert.match(summaryHtml, /RUNTIME/);
assert.match(summaryHtml, /复用 Thread/);
assert.match(summaryHtml, /公平等待 1/);
assert.match(summaryHtml, /最老 16 分钟/);
assert.match(summaryHtml, /data-metric="attention"/);
assert.match(clusterHtmlBefore, /G-WARNING/);
assert.match(clusterHtmlBefore, /G-CRITICAL/);
assert.match(clusterHtmlBefore, /活动任务 2/);
assert.match(clusterHtmlBefore, /checkpoint/);
assert.match(clusterHtmlBefore, /心跳延迟/);
assert.match(clusterHtmlBefore, /心跳超时/);
assert.match(clusterHtmlBefore, /warning · 5 分钟/);
assert.match(clusterHtmlBefore, /critical · 16 分钟/);
assert.match(clusterHtmlBefore, /全局容量已满/);
assert.match(clusterHtmlBefore, /目标容量已满/);
assert.match(clusterHtmlBefore, /当前等待 2 分钟/);
assert.match(clusterHtmlBefore, /当前等待 5 分钟/);
assert.match(clusterHtmlBefore, /data-heartbeat-state="warning"/);
assert.match(clusterHtmlBefore, /data-heartbeat-state="critical"/);
assert.match(clusterHtmlBefore, /data-queue-reason="global_capacity"/);
assert.match(clusterHtmlBefore, /data-queue-reason="goal_capacity"/);
assert.match(clusterHtmlBefore, /class="cluster-card .*heartbeat-critical"/);
assert.equal(
  (clusterHtmlBefore.match(/data-control-action="pause"/g) || []).length,
  2,
  "both active goals must retain pause controls",
);
assert.match(clusterHtmlBefore, /需关注/);
assert.match(clusterHtmlBefore, /Worker 心跳延迟/);
assert.match(clusterHtmlBefore, /检查 Worker 进度/);
assert.match(clusterHtmlBefore, /data-control-action="pause"/);
assert.match(clusterHtmlBefore, /data-control-action="cancel"/);
assert.match(clusterHtmlBefore, /data-control-action="archive"/);
assert.match(clusterHtmlBefore, /data-control-action="restore"/);
assert.match(clusterHtmlBefore, /归档目标（含Done）/);
assert.match(clusterHtmlBefore, /恢复目标（含Done）/);
assert.match(clusterHtmlBefore, /不归档 Agent threads/);
assert.match(clusterHtmlBefore, /data-archived="true"/);
assert.match(clusterHtmlBefore, /已归档/);
assert.match(clusterHtmlBefore, /目标已结束/);
assert.match(clusterHtmlBefore, /无需调度/);
assert.match(clusterHtmlBefore, /workflow workflow-v2:goalcohort/);
assert.equal(fakeElements.get("agent-thread-count").textContent, 4);
assert.match(agentThreadHtml, /Diagnostic Worker/);
assert.match(agentThreadHtml, /thread thread-agent-busy/);
assert.match(agentThreadHtml, /contract current/);
assert.match(agentThreadHtml, /contract historical · not reusable/);
assert.match(agentThreadHtml, /context/);
assert.match(agentThreadHtml, /cache/);
assert.match(agentThreadHtml, /uncached/);
assert.match(agentThreadHtml, /last cache\/uncached 1,200 \/ 400/);
assert.match(agentThreadHtml, /T-1 · 实现真实消费者/);
assert.match(agentThreadHtml, /75%/);
assert.match(agentThreadHtml, /忙碌中不可重置/);
assert.match(agentThreadHtml, /请求重置/);
assert.match(agentThreadHtml, /data-control-action="reset-agent"/);
assert.match(agentThreadHtml, /最近事件 reset_requested/);
assert.match(agentThreadHtml, /已被替换不可重置/);
assert.match(agentThreadHtml, /已归档不可重置/);
assert.match(agentThreadHtml, /data-agent-archived="true"/);
assert.doesNotMatch(agentThreadHtml, /conversation|transcript/i);

const nativeBoard = JSON.parse(JSON.stringify(secondBoard));
nativeBoard.cluster_agents = [{
  agent_id: "A-NATIVE",
  display_name: "Native Explorer",
  status: "loaded",
  runtime: "native",
  role: "team_explorer",
  workstream_key: "native-stream",
  thread_id: "thread-native-1",
  parent_thread_id: "thread-parent-1",
  agent_path: "/root/native_explorer",
  agent_nickname: "Erdos",
  prompt_status: "missing",
  current_task_id: "T-NATIVE",
  current_task_title: "验证任务送达",
  context_tokens: 0,
  turn_count: 0,
}];
assert.equal(
  hooks.renderBoard(nativeBoard),
  true,
  "native prompt status changes should redraw diagnostics",
);
const nativeAgentHtml = fakeElements.get("agent-thread-list").innerHTML;
assert.match(nativeAgentHtml, /runtime native/);
assert.match(nativeAgentHtml, /prompt missing/);
assert.match(nativeAgentHtml, /agent Erdos/);
assert.match(nativeAgentHtml, /父 Thread：thread-parent-1/);
assert.match(nativeAgentHtml, /Native thread 仅支持 same-task/);
assert.equal(
  hooks.renderBoard(secondBoard),
  true,
  "restoring managed diagnostics should redraw once",
);

assert.match(taskCardHtml, /thread thread-task-1/);
assert.match(taskCardHtml, /thread reuse/);
assert.match(taskCardHtml, /workflow workflow-v2:taskcohort/);
assert.match(taskCardHtml, /turn turn-3/);
assert.match(taskCardHtml, /cache 75%/);
assert.match(taskCardHtml, /1,500 cached/);
assert.match(taskCardHtml, /500 uncached/);
assert.doesNotMatch(taskCardHtml, /undefined/);
assert.equal(
  hooks.workflowEpochText("workflow-v2:abcdefghijklmnopqrstuvwxyz"),
  "workflow workflow-v2:abcdefghij…",
);
assert.equal(hooks.workflowEpochText(""), "workflow unbound");
assert.equal(
  hooks.renderBoard(secondBoard),
  false,
  "unchanged meaningful state should skip DOM rendering",
);
assert.equal(
  fakeElements.get("cluster-overview").innerHTML,
  clusterHtmlBefore,
  "skipped render must leave the cluster DOM unchanged",
);

const meaningfulBoard = JSON.parse(JSON.stringify(secondBoard));
meaningfulBoard.cluster_summary.attention_count = 0;
meaningfulBoard.cluster_summary.heartbeat_overdue = 0;
meaningfulBoard.cluster_goals[0].health = {
  state: "healthy",
  label: "运行正常",
};
meaningfulBoard.cluster_goals[0].heartbeat_state = "normal";
meaningfulBoard.cluster_goals[0].queue_reason = "running";
meaningfulBoard.cluster_goals[1].health = {
  state: "healthy",
  label: "运行正常",
};
meaningfulBoard.cluster_goals[1].heartbeat_state = "normal";
meaningfulBoard.cluster_goals[1].queue_reason = "running";
assert.notEqual(
  hooks.buildBoardSignature(firstBoard),
  hooks.buildBoardSignature(meaningfulBoard),
  "meaningful cluster health changes must change the signature",
);
assert.equal(hooks.renderBoard(meaningfulBoard), true, "health change should redraw");
const clusterHtmlAfter = fakeElements.get("cluster-overview").innerHTML;
assert.match(clusterHtmlAfter, /运行正常/);
assert.doesNotMatch(clusterHtmlAfter, /需关注/);

const pausedBoard = JSON.parse(JSON.stringify(meaningfulBoard));
pausedBoard.cluster_summary.goals_active = 0;
pausedBoard.cluster_summary.goals_paused = 2;
pausedBoard.cluster_summary.active_jobs = 0;
pausedBoard.cluster_summary.active_workers = 0;
pausedBoard.cluster_summary.active_leads = 0;
pausedBoard.cluster_summary.global_jobs = 0;
pausedBoard.cluster_summary.global_capacity = 0;
pausedBoard.cluster_summary.queued_goals = 0;
pausedBoard.cluster_summary.fairness_wait_count = 0;
pausedBoard.cluster_summary.attention_count = 0;
pausedBoard.cluster_summary.heartbeat_overdue = 0;
pausedBoard.cluster_jobs = [];
for (const goal of pausedBoard.cluster_goals.slice(0, 2)) {
  delete goal.health;
  delete goal.next_action;
  goal.status = "paused";
  goal.active_job_count = 0;
  goal.heartbeat_state = "idle";
  goal.heartbeat_age_seconds = 0;
  goal.queue_reason = "none";
  goal.estimated_wait_seconds = 0;
}
assert.equal(hooks.renderBoard(pausedBoard), true, "paused state should redraw");
const pausedClusterHtml = fakeElements.get("cluster-overview").innerHTML;
assert.match(pausedClusterHtml, /data-control-action="resume"/);
assert.match(pausedClusterHtml, /data-control-action="cancel"/);
assert.match(pausedClusterHtml, /已暂停/);
assert.match(pausedClusterHtml, /可恢复目标继续推进/);

const metrics = Object.fromEntries(
  hooks.buildClusterMetrics(firstBoard).map(({ key, value }) => [key, value]),
);
assert.deepEqual(metrics, {
  jobs: "2/6",
  queued: 2,
  runtime: "app_server",
  "heartbeat-overdue": 2,
  goals: 5,
  active: 2,
  paused: 1,
  attention: 2,
  leads: 1,
});
const compatibilityBoard = JSON.parse(JSON.stringify(firstBoard));
delete compatibilityBoard.cluster_summary.active_workers;
delete compatibilityBoard.cluster_summary.global_jobs;
const compatibilityMetrics = Object.fromEntries(
  hooks.buildClusterMetrics(compatibilityBoard)
    .map(({ key, value }) => [key, value]),
);
assert.equal(
  compatibilityMetrics.jobs,
  "2/6",
  "worker slots must fall back to active worker jobs",
);
assert.equal(compatibilityMetrics.leads, 1);
assert.equal(compatibilityMetrics.runtime, "app_server");
assert.equal(hooks.goalHealth(firstBoard.cluster_goals[0]).attention, true);
assert.deepEqual(
  JSON.parse(JSON.stringify(
    hooks.goalHeartbeatInfo(firstBoard.cluster_goals[1]),
  )),
  { state: "critical", label: "心跳超时", tone: "critical" },
);
assert.deepEqual(
  JSON.parse(JSON.stringify(
    hooks.goalQueueReason(firstBoard.cluster_goals[0]),
  )),
  { state: "global_capacity", label: "全局容量已满" },
);
assert.equal(hooks.waitDurationText(0), "0 秒");
assert.equal(hooks.waitDurationText(125), "2 分钟");
assert.equal(hooks.waitDurationText(undefined), "—");
assert.deepEqual(
  {
    idle: hooks.goalHeartbeatInfo({ heartbeat_state: "idle" }).label,
    normal: hooks.goalHeartbeatInfo({ heartbeat_state: "normal" }).label,
    warning: hooks.goalHeartbeatInfo({ heartbeat_state: "warning" }).label,
    critical: hooks.goalHeartbeatInfo({ heartbeat_state: "critical" }).label,
  },
  {
    idle: "无活动心跳",
    normal: "心跳正常",
    warning: "心跳延迟",
    critical: "心跳超时",
  },
);
assert.deepEqual(
  Object.fromEntries(
    [
      "running",
      "review_pending",
      "blocked",
      "ready",
      "global_capacity",
      "goal_capacity",
      "dependency_wait",
      "waiting_for_lead",
      "none",
    ].map((state) => [
      state,
      hooks.goalQueueReason({ queue_reason: state }).label,
    ]),
  ),
  {
    running: "运行中",
    review_pending: "等待 Review",
    blocked: "任务受阻",
    ready: "等待调度",
    global_capacity: "全局容量已满",
    goal_capacity: "目标容量已满",
    dependency_wait: "等待依赖完成",
    waiting_for_lead: "等待 Lead 规划",
    none: "无排队",
  },
);
assert.equal(
  hooks.nextActionText(firstBoard.cluster_goals[0]),
  "检查 Worker 进度",
);
assert.equal(
  hooks.goalHealth({ status: "active", last_error: "lead dispatch failed" }).attention,
  true,
  "legacy last_error state should still surface attention",
);
assert.equal(
  hooks.goalHeartbeatInfo({
    status: "failed",
    active_job_count: 0,
    heartbeat_state: "idle",
  }).label,
  "目标已结束",
  "terminal goals must not be presented as heartbeat failures",
);
assert.equal(
  hooks.goalHealth({
    status: "paused",
    last_error: "pause requested through dashboard",
  }).attention,
  false,
  "normal control reasons must not be treated as attention",
);
assert.equal(
  hooks.nextActionText({ status: "paused" }),
  "可恢复目标继续推进",
  "paused goals should expose a fallback next action",
);
assert.equal(
  hooks.goalQueueReason({ status: "completed", queue_reason: "none" }).label,
  "无需调度",
  "terminal goals should not be presented as queued",
);
const sparseDiagnosticsBoard = JSON.parse(JSON.stringify(meaningfulBoard));
sparseDiagnosticsBoard.tasks[0].active_job = {
  role: "team_worker",
  status: "running",
};
sparseDiagnosticsBoard.cluster_agents = [{
  agent_id: "A-SPARSE",
  status: "stored",
  role: "team_worker",
}];
sparseDiagnosticsBoard.cluster_agent_events = [];
assert.equal(
  hooks.renderBoard(sparseDiagnosticsBoard),
  true,
  "partial diagnostic fields should still render",
);
assert.doesNotMatch(
  fakeElements.get("cards-ready").innerHTML,
  /undefined/,
  "missing job token fields must not leak undefined into task cards",
);
assert.doesNotMatch(
  fakeElements.get("agent-thread-list").innerHTML,
  /undefined/,
  "missing Agent fields must not leak undefined into diagnostics",
);
assert.deepEqual(
  [1, 2, 3, 4, 5, 6, 7].map((failures) => hooks.backoffDelay(failures)),
  [2000, 4000, 8000, 16000, 30000, 30000, 30000],
  "poll failures should back off exponentially and cap at 30 seconds",
);
assert.deepEqual(
  JSON.parse(JSON.stringify(
    hooks.createClusterPayload("project-1", "  交付可验收结果  ", "supervised"),
  )),
  {
    project_id: "project-1",
    objective: "交付可验收结果",
    autonomy: "supervised",
    max_worker_attempts: 3,
    max_lead_attempts: 5,
  },
);

console.log("dashboard-ui-smoke: ok");
