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
expectText(app, "JSON.stringify({ goal_id: goalId, action })", "app.js");
expectText(app, "/api/clusters/control", "app.js");
expectText(app, "/api/board", "app.js");
expectText(app, "/api/projects", "app.js");
expectText(app, "data.cluster_summary", "app.js");
expectText(app, "goal?.health", "app.js");
expectText(app, "goal?.next_action", "app.js");
expectText(app, 'allLabel: "选择目标项目"', "app.js");
expectText(css, ".cluster-card.attention", "style.css");
expectText(css, ".cluster-actions", "style.css");

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
    project_name: "Demo",
    blocked_by: [],
    claim_wait_seconds: 62,
    claim_wait_state: "warning",
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
    total_goals: 3,
    active_goals: 1,
    paused_goals: 1,
    attention_goals: 1,
    active_workers: 2,
  },
  cluster_goals: [{
    goal_id: "G-1",
    status: "active",
    objective: "验证集群状态渲染",
    project_name: "Demo",
    autonomy: "auto",
    lead_ticks: 2,
    health: { state: "attention", label: "Worker 连续失败" },
    next_action: { action: "重试 Worker 或检查日志" },
  }],
  cluster_jobs: [{
    goal_id: "G-1",
    kind: "worker",
    role: "team_worker",
    status: "running",
    pid: 4321,
  }],
};
const secondBoard = JSON.parse(JSON.stringify(firstBoard));
secondBoard.generated_at = "2026-11-07T10:00:02Z";
secondBoard.tasks[0].claim_wait_seconds = 64;
secondBoard.tasks[0].claim_wait_state = "warning";

assert.equal(
  hooks.buildBoardSignature(firstBoard),
  hooks.buildBoardSignature(secondBoard),
  "generated time and claim-age-only changes must not force a redraw",
);

assert.equal(hooks.renderBoard(firstBoard), true, "first board should render");
const clusterHtmlBefore = fakeElements.get("cluster-overview").innerHTML;
assert.match(
  fakeElements.get("cluster-summary").innerHTML,
  /data-metric="attention"/,
);
assert.match(clusterHtmlBefore, /需关注/);
assert.match(clusterHtmlBefore, /Worker 连续失败/);
assert.match(clusterHtmlBefore, /重试 Worker 或检查日志/);
assert.match(clusterHtmlBefore, /data-control-action="pause"/);
assert.match(clusterHtmlBefore, /data-control-action="cancel"/);
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
meaningfulBoard.cluster_summary.attention_goals = 0;
meaningfulBoard.cluster_goals[0].health = {
  state: "healthy",
  label: "运行正常",
};
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
pausedBoard.cluster_summary.active_goals = 0;
pausedBoard.cluster_summary.paused_goals = 1;
pausedBoard.cluster_summary.attention_goals = 0;
delete pausedBoard.cluster_goals[0].health;
delete pausedBoard.cluster_goals[0].next_action;
pausedBoard.cluster_goals[0].status = "paused";
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
  goals: 3,
  active: 1,
  paused: 1,
  attention: 1,
  workers: 2,
});
assert.equal(hooks.goalHealth(firstBoard.cluster_goals[0]).attention, true);
assert.equal(
  hooks.nextActionText(firstBoard.cluster_goals[0]),
  "重试 Worker 或检查日志",
);
assert.equal(
  hooks.goalHealth({ status: "active", last_error: "lead dispatch failed" }).attention,
  true,
  "legacy last_error state should still surface attention",
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
