import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { isAbsolute, join, relative, sep } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const baseUrl = process.env.CODEX_TEAM_DASHBOARD_URL || "http://127.0.0.1:8765";
const edgeCandidates = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];
const configuredEdgePath = process.env.CODEX_TEAM_EDGE_PATH?.trim();
const edgePath = configuredEdgePath || edgeCandidates.find(existsSync);
if (!edgePath) throw new Error("Microsoft Edge not found");

const screenshotPath = join(process.cwd(), "artifacts", "dashboard-edge-smoke.png");
await mkdir(join(process.cwd(), "artifacts"), { recursive: true });
const profileRoot = fileURLToPath(
  new URL("../artifacts/edge-smoke-profiles", import.meta.url),
);
await mkdir(profileRoot, { recursive: true });
const profilePrefix = `edge-smoke-${process.pid}-`;
const profile = await mkdtemp(join(profileRoot, profilePrefix));
const failures = [];
const checks = [];
const network = new Map();
const pageErrors = [];
const consoleErrors = [];

function check(condition, name, details = null) {
  checks.push({ name, ok: Boolean(condition), details });
  if (!condition) failures.push({ name, details });
}

async function getJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result || {});
        return;
      }
      if (message.method === "Network.requestWillBeSent") {
        const request = message.params.request;
        if (request.url.startsWith(baseUrl)) {
          network.set(message.params.requestId, {
            requestId: message.params.requestId,
            method: request.method,
            url: request.url,
            status: null,
          });
        }
      } else if (message.method === "Network.responseReceived") {
        const entry = network.get(message.params.requestId);
        if (entry) entry.status = message.params.response.status;
      } else if (message.method === "Runtime.exceptionThrown") {
        pageErrors.push(
          message.params.exceptionDetails?.exception?.description
            || message.params.exceptionDetails?.text
            || "unknown page exception",
        );
      } else if (
        message.method === "Runtime.consoleAPICalled"
        && message.params.type === "error"
      ) {
        consoleErrors.push(
          message.params.args
            .map((argument) => argument.value ?? argument.description ?? "")
            .join(" "),
        );
      }
    });
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    return new Cdp(socket);
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    try {
      this.socket.close();
    } catch {
      // Best-effort cleanup.
    }
  }
}

async function waitFor(fn, timeoutMs = 15000, intervalMs = 120) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "";
  while (Date.now() < deadline) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (error) {
      if (error instanceof EdgeProcessError) throw error;
      lastError = error.message;
    }
    await delay(intervalMs);
  }
  throw new Error(`Timed out after ${timeoutMs}ms${lastError ? `: ${lastError}` : ""}`);
}

async function removeProfileWithRetry(profilePath) {
  const retryable = new Set(["EBUSY", "EPERM", "ENOTEMPTY"]);
  let lastError;
  for (let attempt = 0; attempt < 24; attempt += 1) {
    try {
      await rm(profilePath, { recursive: true, force: true });
      return;
    } catch (error) {
      lastError = error;
      if (!retryable.has(error?.code)) throw error;
      await delay(250);
    }
  }
  console.error(
    `warning: Edge profile cleanup incomplete after retries: ${profilePath}: `
    + `${lastError?.code || "unknown"}`,
  );
  process.exitCode = 1;
}

function killEdgeProfileProcesses(profilePath) {
  if (process.platform === "win32") {
    const command = [
      "$profilePath = $env:EDGE_SMOKE_PROFILE;",
      "Get-CimInstance Win32_Process",
      "| Where-Object {",
      "  $_.Name -eq 'msedge.exe'",
      "  -and $_.CommandLine",
      "  -and $_.CommandLine.Contains($profilePath)",
      "}",
      "| ForEach-Object {",
      "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue",
      "}",
    ].join(" ");
    spawnSync("powershell.exe", ["-NoProfile", "-Command", command], {
      windowsHide: true,
      stdio: "ignore",
      env: { ...process.env, EDGE_SMOKE_PROFILE: profilePath },
    });
  }
}

class EdgeProcessError extends Error {
  constructor(message) {
    super(message);
    this.name = "EdgeProcessError";
  }
}

let spawnError = null;
let earlyExit = null;
let client;
let edge = null;

try {
  try {
    edge = spawn(edgePath, [
      "--headless=new",
      "--disable-gpu",
      "--disable-breakpad",
      "--disable-crash-reporter",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-extensions",
      "--disable-sync",
      "--metrics-recording-only",
      "--noerrdialogs",
      "--no-first-run",
      "--no-default-browser-check",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "--window-size=1165,1000",
      "about:blank",
    ], {
      stdio: "ignore",
      windowsHide: true,
    });
  } catch (error) {
    throw new EdgeProcessError(`Edge spawn failed: ${error.message}`);
  }

  edge.once("error", (error) => {
    spawnError = error;
  });
  edge.once("exit", (code, signal) => {
    earlyExit = { code, signal };
  });

  const portFile = join(profile, "DevToolsActivePort");
  const port = await waitFor(async () => {
    if (spawnError) {
      throw new EdgeProcessError(
        `Edge spawn failed: ${spawnError.message}`,
      );
    }
    if (!existsSync(portFile)) return 0;
    const [value] = (await readFile(portFile, "utf8")).trim().split(/\r?\n/);
    return Number(value) || 0;
  });
  const targets = await getJson(`http://127.0.0.1:${port}/json/list`);
  const target = targets.find((item) => item.type === "page");
  if (!target) throw new Error("Edge page target not found");
  client = await Cdp.connect(target.webSocketDebuggerUrl);
  await client.send("Page.enable");
  await client.send("Runtime.enable");
  await client.send("Network.enable");
  await client.send("Emulation.setDeviceMetricsOverride", {
    width: 1120,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await client.send("Page.navigate", { url: `${baseUrl}/` });

  const evaluate = async (expression) => {
    const response = await client.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (response.exceptionDetails) {
      throw new Error(
        response.exceptionDetails.exception?.description
          || response.exceptionDetails.text
          || "Runtime.evaluate failed",
      );
    }
    return response.result?.value;
  };
  const snapshot = () => evaluate(`(() => ({
    innerWidth,
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: Math.max(
      document.documentElement.scrollWidth,
      document.body.scrollWidth
    ),
    metricText: document.getElementById("cluster-summary")?.innerText || "",
    runtimeMetric: (() => {
      const card = document.querySelector('[data-metric="runtime"]');
      const copy = card?.querySelector(".metric-copy small");
      if (!card || !copy) return null;
      const cardRect = card.getBoundingClientRect();
      const copyRect = copy.getBoundingClientRect();
      return {
        cardWidth: cardRect.width,
        copyWidth: copyRect.width,
        copyHeight: copyRect.height,
        lineRects: copy.getClientRects().length,
        text: copy.textContent?.trim() || "",
      };
    })(),
    topProject: document.getElementById("project-select")?.value || "",
    clusterProject:
      document.getElementById("cluster-project-select")?.value || "",
    goals: [...document.querySelectorAll(".cluster-card")].map((node) => ({
      id: node.dataset.goalId || "",
      archived: node.dataset.archived === "true",
      restore: Boolean(node.querySelector('[data-control-action="restore"]')),
      project:
        node.querySelector(".card-meta > .chip:first-child")?.textContent?.trim()
        || "",
    })),
  }))()`);

  const initial = await waitFor(async () => {
    const value = await snapshot();
    return value.goals.length ? value : false;
  });
  const board = await getJson(`${baseUrl}/api/board`);
  const summary = board.cluster_summary || {};
  check(
    initial.metricText.includes(`${summary.global_jobs}/${summary.global_capacity}`),
    "worker slot metric matches API",
    initial.metricText,
  );
  check(
    Number(summary.active_leads) >= 0
      && initial.metricText.includes("LEADS"),
    "lead metric is visible",
    initial.metricText,
  );
  check(
    initial.runtimeMetric
      && initial.runtimeMetric.cardWidth >= 120
      && initial.runtimeMetric.copyWidth >= 40
      && initial.runtimeMetric.lineRects <= 4,
    "runtime metric remains readable at 1120px",
    initial.runtimeMetric,
  );
  check(
    initial.scrollWidth <= initial.innerWidth,
    "1120px viewport has no horizontal overflow",
    {
      innerWidth: initial.innerWidth,
      clientWidth: initial.clientWidth,
      scrollWidth: initial.scrollWidth,
    },
  );

  const projectOptions = await evaluate(`[
    ...document.querySelectorAll("#project-select option")
  ].map((option) => ({ value: option.value, text: option.textContent.trim() }))`);
  const project = projectOptions.find(
    (option) => option.value
      && initial.goals.some(
        (goal) => goal.project === option.text.split(" · ")[0],
      ),
  ) || projectOptions.find((option) => option.value);
  if (project) {
    const projectName = project.text.split(" · ")[0];
    const expectsVisibleGoals = initial.goals.some(
      (goal) => goal.project === projectName,
    );
    await evaluate(`(() => {
      const select = document.getElementById("cluster-project-select");
      select.value = ${JSON.stringify(project.value)};
      select.dispatchEvent(new Event("change", { bubbles: true }));
    })()`);
    const selected = await waitFor(async () => {
      const value = await snapshot();
      return value.topProject === project.value
        && value.clusterProject === project.value
        && (
          !expectsVisibleGoals
          || (
            value.goals.length > 0
            && value.goals.every((goal) => goal.project === projectName)
          )
        )
        ? value
        : false;
    });
    await delay(2200);
    const selectedAfterRefresh = await snapshot();
    if (expectsVisibleGoals) {
      check(
        selected.goals.every((goal) => goal.project.length > 0),
        "cluster project selector loaded project-scoped goals",
        selectedAfterRefresh.goals,
      );
    }
  }

  await evaluate(`(() => {
    const select = document.getElementById("project-select");
    select.value = "";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  })()`);
  await waitFor(async () => {
    const value = await snapshot();
    return value.topProject === "" && value.clusterProject === "";
  });
  const archivedBoard = await getJson(
    `${baseUrl}/api/board?include_archived=1`,
  );
  const archivedGoal = (archivedBoard.cluster_goals || []).find(
    (goal) => goal.archived_at,
  );
  if (archivedGoal) {
    const archivedGoalId = archivedGoal.goal_id;
    await evaluate(`(() => {
      const input = document.getElementById("show-archived");
      input.checked = true;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    })()`);
    const archived = await waitFor(async () => {
      const value = await snapshot();
      return value.goals.some((goal) => goal.id === archivedGoalId)
        ? value
        : false;
    });
    const goal = archived.goals.find((item) => item.id === archivedGoal.goal_id);
    check(Boolean(goal?.archived), "archived goal is visible in archive view");
    check(Boolean(goal?.restore), "archived goal exposes restore control");
  }

  const capture = await client.send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
  });
  await writeFile(screenshotPath, Buffer.from(capture.data, "base64"));

  const badResponses = [...network.values()].filter(
    (entry) => entry.status !== null && (entry.status < 200 || entry.status >= 400),
  );
  check(badResponses.length === 0, "no 4xx/5xx responses", badResponses);
  check(pageErrors.length === 0, "no page errors", pageErrors);
  check(consoleErrors.length === 0, "no console errors", consoleErrors);
  if (spawnError) {
    throw new EdgeProcessError(`Edge spawn failed: ${spawnError.message}`);
  }
  console.log(JSON.stringify({
    ok: failures.length === 0,
    checks,
    failures,
    screenshotPath,
    networkRequests: [...network.values()],
    pageErrors,
    consoleErrors,
  }, null, 2));
  if (failures.length) process.exitCode = 1;
} finally {
  client?.close();
  if (edge?.pid) {
    spawnSync("taskkill", ["/PID", String(edge.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
    });
  }
  killEdgeProfileProcesses(profile);
  await delay(1000);
  const profileRelative = relative(profileRoot, profile);
  if (
    profileRelative.startsWith(profilePrefix)
    && !profileRelative.includes(sep)
    && !isAbsolute(profileRelative)
  ) {
    await removeProfileWithRetry(profile);
  }
}
