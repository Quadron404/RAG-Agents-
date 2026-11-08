// Comprehensive stateful fake backend for RAG Agents real-browser QA.
// Serves every HTTP + WebSocket endpoint the frontend calls, so the real
// deployed frontend can be driven through Chromium end-to-end.
const http = require("http");
const { WebSocketServer } = require("ws");
const urlModule = require("url");

const PORT = 8000;

/* ============================================================================
   In-memory state
   ========================================================================== */

const USER = {
  id: "qa-user-1",
  email: "qa@example.com",
  name: "QA Tester",
  picture: "",
  timezone: "UTC",
  developer_mode: 0,
};

let sessionCsrf = "qa-csrf-" + Math.random().toString(36).slice(2);
let gmailConnected = false;
let gmailStatus = "unknown";
let developerMode = 0;
let timezone = "UTC";

const threads = [
  {
    thread_id: "thread-qa-1",
    user_id: "qa-user-1",
    title: "Planning the week",
    created_at: Date.now() / 1000 - 10000,
    summarized_through: 0,
  },
  {
    thread_id: "thread-qa-2",
    user_id: "qa-user-1",
    title: "Research task",
    created_at: Date.now() / 1000 - 20000,
    summarized_through: 0,
  },
];

const messagesByThread = {
  "thread-qa-1": [
    {
      id: "m-1",
      thread_id: "thread-qa-1",
      user_id: "qa-user-1",
      role: "user",
      agent: "user",
      content: "Hi Chief",
      data: "{}",
      seq: 1,
      created_at: Date.now() / 1000 - 5000,
    },
    {
      id: "m-2",
      thread_id: "thread-qa-1",
      user_id: "qa-user-1",
      role: "assistant",
      agent: "commander",
      content: "Hi! How can I help you today?",
      data: "{}",
      seq: 2,
      created_at: Date.now() / 1000 - 4900,
    },
  ],
  "thread-qa-2": [],
};

let activeThreadId = "thread-qa-1";
let msgSeq = 3;

/* --- computer --- */
let computerState = {
  running: false,
  booting: false,
  msg: "",
  error: "",
};
let computerTaskId = 0;
const computerTasks = {};

/* --- screen --- */
let screenConfig = {
  mode: "bridge",
  wsUrl: "",
  bridgePath: "/ws/screen",
  wsProtocols: ["binary"],
  publicOrigin: null,
  publicUrlAgeSeconds: null,
};
let screenStatus = {
  ok: true,
  host: "127.0.0.1",
  port: 5900,
  listening: true,
  display: { size: "1280x800", vnc: true, websockify: true },
};

/* --- sysinfo --- */
let sysInfo = {
  hostname: "rag-qa-machine",
  uptime: 12345,
  os: "Linux",
  kernel: "6.1.0",
  cpu: { cores: 4, idle: 45, total: 100 },
  mem: { total_kb: 8388608, used_kb: 2097152 },
  disk: { total: 50, used: 12, free: 38 },
  browser: { running: true, cdp: true, pid: 4242, start_url: "about:blank", display: ":99" },
  display: { width: 1280, height: 800, size: "1280x800" },
  loadavg: [0.1, 0.2, 0.3],
};

/* --- routines --- */
let routines = [
  {
    id: "routine-1",
    name: "Daily summary",
    cron: "0 9 * * *",
    timezone: "UTC",
    status: "active",
    created_at: Date.now() / 1000 - 80000,
    last_run_at: null,
    next_run_at: Date.now() / 1000 + 1000,
  },
];
let routineRuns = {
  "routine-1": [
    { id: "run-1", routine_id: "routine-1", status: "completed", started_at: Date.now() / 1000 - 70000, finished_at: Date.now() / 1000 - 69000, output: "ok" },
  ],
};
let runSeq = 2;

/* --- approvals --- */
let approvals = [
  {
    approval_id: "approval-1",
    connector: "gmail",
    action: "send_email",
    recipient: "test@example.com",
    subject: "Test approval",
    status: "pending",
    created_at: Date.now() / 1000 - 100,
    expires_at: Date.now() / 1000 + 3600,
    one_time: true,
  },
];

/* --- virtual files --- */
const files = {
  "/qa/hello.txt": { content: "Hello from the fake backend\n", mime: "text/plain" },
  "/qa/notes.md": { content: "# Notes\n\nThis is a test note.\n", mime: "text/markdown" },
};

/* ============================================================================
   Helpers
   ========================================================================== */

function json(res, obj, status = 200, extraHeaders = {}) {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Credentials": "true",
    "Cache-Control": "no-store",
    ...extraHeaders,
  });
  res.end(JSON.stringify(obj));
}

function parseBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => resolve(body));
    req.on("error", reject);
  });
}

const userIdFromPath = (pathname) => {
  const m = pathname.match(/^\/ws\/([^\/]+)$/);
  return m ? m[1] : null;
};

/* ============================================================================
   HTTP router
   ========================================================================== */

const server = http.createServer(async (req, res) => {
  const parsed = urlModule.parse(req.url, true);
  const pathname = parsed.pathname;
  const method = (req.method || "GET").toUpperCase();

  // CORS preflight
  if (method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Credentials": "true",
      "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
      "Access-Control-Allow-Headers": "content-type,x-rag-csrf,x-request-id",
    });
    res.end();
    return;
  }

  const cs = req.headers["x-rag-csrf"];

  /* ---------- auth ---------- */
  if (pathname === "/auth/session" && method === "GET") {
    json(res, { authenticated: true, csrf: sessionCsrf, user: USER, gmail: { connected: gmailConnected, status: gmailStatus } });
    return;
  }
  if (pathname === "/auth/gmail/status" && method === "GET") {
    json(res, { connected: gmailConnected, status: gmailStatus });
    return;
  }
  if (pathname === "/auth/gmail/connect" && method === "POST") {
    gmailConnected = true;
    gmailStatus = "connected";
    json(res, { ok: true, connected: true });
    return;
  }
  if (pathname === "/auth/gmail/disconnect" && method === "POST") {
    gmailConnected = false;
    gmailStatus = "disconnected";
    json(res, { ok: true, connected: false });
    return;
  }
  if (pathname === "/auth/logout" && method === "POST") {
    json(res, { ok: true });
    return;
  }
  if (pathname === "/auth/settings/developer-mode" && method === "GET") {
    json(res, { developer_mode: developerMode });
    return;
  }
  if (pathname === "/auth/settings/developer-mode" && method === "POST") {
    const b = await parseBody(req);
    let payload = {};
    try { payload = JSON.parse(b); } catch {}
    if (typeof payload.developer_mode === "number") developerMode = payload.developer_mode;
    json(res, { ok: true, developer_mode: developerMode });
    return;
  }
  if (pathname === "/auth/settings/timezone" && method === "GET") {
    json(res, { timezone });
    return;
  }
  if (pathname === "/auth/settings/timezone" && method === "POST") {
    const b = await parseBody(req);
    try { timezone = JSON.parse(b).timezone || timezone; } catch {}
    json(res, { ok: true, timezone });
    return;
  }
  if (pathname === "/auth/settings/reset" && method === "POST") {
    developerMode = 0;
    json(res, { ok: true });
    return;
  }

  /* ---------- threads ---------- */
  if (pathname === "/threads" && method === "GET") {
    json(res, { threads });
    return;
  }
  if (pathname === "/threads" && method === "POST") {
    const b = await parseBody(req);
    let payload = {};
    try { payload = JSON.parse(b); } catch {}
    const id = "thread-qa-" + Date.now();
    const t = { thread_id: id, user_id: "qa-user-1", title: payload.title || "New conversation", created_at: Date.now() / 1000, summarized_through: 0 };
    threads.push(t);
    messagesByThread[id] = [];
    activeThreadId = id;
    json(res, { ok: true, thread: t }, 201);
    return;
  }
  if (method === "GET" && /^\/threads\/([^\/]+)\/messages$/.test(pathname)) {
    const id = pathname.match(/^\/threads\/([^\/]+)\/messages$/)[1];
    json(res, { messages: messagesByThread[id] || [] });
    return;
  }
  if (method === "POST" && /^\/threads\/([^\/]+)\/messages$/.test(pathname)) {
    const id = pathname.match(/^\/threads\/([^\/]+)\/messages$/)[1];
    const b = await parseBody(req);
    let payload = {};
    try { payload = JSON.parse(b); } catch {}
    const userText = payload.content || "";
    const userId = payload.request_id || "req-" + Date.now();
    // Store the user turn.
    const userMsg = {
      id: "um-" + Date.now(),
      thread_id: id,
      user_id: "qa-user-1",
      role: "user",
      agent: "user",
      content: userText,
      data: "{}",
      seq: ++msgSeq,
      created_at: Date.now() / 1000,
    };
    (messagesByThread[id] = messagesByThread[id] || []).push(userMsg);
    // SSE stream: delta then done.
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
      "Access-Control-Allow-Origin": "*",
    });
    const sendEvent = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    sendEvent("delta", { agent: "commander", delta: "I received your message: \"" + userText + "\"\n\nThis is a demo response from the QA fake backend." });
    setTimeout(() => {
      try { sendEvent("done", {}); res.end(); } catch {}
    }, 400);
    return;
  }
  if (pathname === "/session/active-thread" && method === "GET") {
    json(res, { active_thread_id: activeThreadId });
    return;
  }
  if (method === "POST" && /^\/threads\/([^\/]+)\/activate$/.test(pathname)) {
    const id = pathname.match(/^\/threads\/([^\/]+)\/activate$/)[1];
    activeThreadId = id;
    json(res, { ok: true, active_thread_id: id });
    return;
  }
  if (method === "POST" && /^\/threads\/([^\/]+)\/generate-history$/.test(pathname)) {
    json(res, { ok: true, summary: "Generated summary for " + activeThreadId });
    return;
  }

  /* ---------- computer ---------- */
  if (pathname === "/computer/start" && method === "POST") {
    computerState = { running: true, booting: false, msg: "Starting remote computer…", error: "" };
    json(res, { ok: true, running: true });
    return;
  }
  if (pathname === "/computer/stop" && method === "POST") {
    computerState = { running: false, booting: false, msg: "", error: "" };
    json(res, { ok: true, running: false });
    return;
  }

  /* ---------- sysinfo ---------- */
  if (pathname === "/sysinfo" && method === "GET") {
    json(res, sysInfo);
    return;
  }

  /* ---------- screen ---------- */
  if (pathname === "/screen/config" && method === "GET") {
    json(res, screenConfig);
    return;
  }
  if (pathname === "/screen/status" && method === "GET") {
    json(res, screenStatus);
    return;
  }
  if (pathname === "/screen/ensure" && method === "POST") {
    screenStatus.listening = true;
    json(res, { ok: true });
    return;
  }
  if (pathname === "/screen/browser/restart" && method === "POST") {
    screenStatus.display = { size: "1280x800", vnc: true, websockify: true };
    json(res, { ok: true });
    return;
  }

  /* ---------- ai/computer ---------- */
  if (pathname === "/ai/computer/start" && method === "POST") {
    const b = await parseBody(req);
    let payload = {};
    try { payload = JSON.parse(b); } catch {}
    const taskId = "ctask-" + ++computerTaskId;
    computerTasks[taskId] = {
      task_id: taskId,
      status: "done",
      step: 3,
      message: "Task completed.",
      url: "about:blank",
      steps: 3,
      running: false,
      done: true,
      task: payload.task || "demo task",
      thread_id: payload.thread_id || "",
      provider: payload.provider || "groq",
    };
    json(res, { ok: true, task_id: taskId, running: false, done: true, status: "done" });
    return;
  }
  if (method === "GET" && /^\/ai\/computer\/([^\/]+)$/.test(pathname)) {
    const id = pathname.match(/^\/ai\/computer\/([^\/]+)$/)[1];
    const t = computerTasks[id];
    if (!t) { res.writeHead(404); res.end("not found"); return; }
    json(res, t);
    return;
  }
  if (method === "POST" && /^\/ai\/computer\/([^\/]+)\/stop$/.test(pathname)) {
    const id = pathname.match(/^\/ai\/computer\/([^\/]+)\/stop$/)[1];
    if (computerTasks[id]) computerTasks[id].running = false;
    json(res, { ok: true });
    return;
  }
  if (pathname === "/ai/computer/providers" && method === "GET") {
    json(res, { providers: [{ name: "groq", label: "Groq", model: "openai/gpt-oss-20b", configured: true }], default: "groq" });
    return;
  }
  if (method === "POST" && /^\/ai\/computer\/([^\/]+)\/provider$/.test(pathname)) {
    json(res, { provider: "groq", model: "openai/gpt-oss-20b" });
    return;
  }
  if (method === "GET" && /^\/ai\/computer\/([^\/]+)\/trace$/.test(pathname)) {
    const id = pathname.match(/^\/ai\/computer\/([^\/]+)\/trace$/)[1];
    const t = computerTasks[id];
    if (!t) { res.writeHead(404); res.end("not found"); return; }
    json(res, {
      task_id: id, task: t.task, thread_id: t.thread_id, status: t.status, message: t.message, url: t.url,
      step: t.step, started_at: Date.now() / 1000 - 100, finished_at: Date.now() / 1000,
      provider: t.provider, selected_providers: [], last_provider: t.provider, last_model: t.provider,
      protocol: { first_turn_allowed: [], after_screenshot_allowed: [], after_screenshot_visible_target: [], json_only: false },
      turns: [],
    });
    return;
  }

  /* ---------- routines ---------- */
  if (pathname === "/routines" && method === "GET") {
    json(res, { routines });
    return;
  }
  if (method === "GET" && /^\/routines\/([^\/]+)\/runs$/.test(pathname)) {
    const id = pathname.match(/^\/routines\/([^\/]+)\/runs$/)[1];
    json(res, { runs: routineRuns[id] || [] });
    return;
  }
  if (method === "POST" && /^\/routines\/([^\/]+)\/pause$/.test(pathname)) {
    const id = pathname.match(/^\/routines\/([^\/]+)\/pause$/)[1];
    const r = routines.find((x) => x.id === id);
    if (r) r.status = "paused";
    json(res, { ok: true });
    return;
  }
  if (method === "POST" && /^\/routines\/([^\/]+)\/resume$/.test(pathname)) {
    const id = pathname.match(/^\/routines\/([^\/]+)\/resume$/)[1];
    const r = routines.find((x) => x.id === id);
    if (r) r.status = "active";
    json(res, { ok: true });
    return;
  }
  if (method === "DELETE" && /^\/routines\/([^\/]+)$/.test(pathname)) {
    const id = pathname.match(/^\/routines\/([^\/]+)$/)[1];
    routines = routines.filter((x) => x.id !== id);
    json(res, { ok: true });
    return;
  }
  if (pathname === "/routines/connectors/approvals" && method === "GET") {
    json(res, { ok: true, approvals });
    return;
  }
  if (pathname === "/routines/connectors/approvals/execute" && method === "POST") {
    const b = await parseBody(req);
    let payload = {};
    try { payload = JSON.parse(b); } catch {}
    const id = payload.approval_id || "approval-1";
    const a = approvals.find((x) => x.approval_id === id);
    if (a) a.status = "executed";
    json(res, { ok: true, executed: true });
    return;
  }
  if (pathname === "/routines/connectors/approvals/deny" && method === "POST") {
    const b = await parseBody(req);
    let payload = {};
    try { payload = JSON.parse(b); } catch {}
    const id = payload.approval_id || "approval-1";
    const a = approvals.find((x) => x.approval_id === id);
    if (a) a.status = "denied";
    json(res, { ok: true, denied: true });
    return;
  }

  /* ---------- files ---------- */
  if (pathname === "/file" && method === "GET") {
    const path = parsed.query.path || "";
    const f = files[path];
    if (!f) { res.writeHead(404, { "Content-Type": "text/plain" }); res.end("not found"); return; }
    res.writeHead(200, { "Content-Type": f.mime, "Content-Disposition": "inline" });
    res.end(f.content);
    return;
  }

  /* ---------- health ---------- */
  if (pathname === "/health" && method === "GET") {
    json(res, { ok: true, provider: "groq", computer: "offline" });
    return;
  }

  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("not found: " + pathname);
});

/* ============================================================================
   WebSocket: /ws/{userId} — chat transport
   ========================================================================== */

const wss = new WebSocketServer({ noServer: true });

wss.on("connection", (ws, req) => {
  const parsed = urlModule.parse(req.url, true);
  const m = parsed.pathname.match(/^\/ws\/([^\/]+)$/);
  const uid = m ? m[1] : "unknown";
  console.log(`[ws] chat connected: ${uid}`);

  let seq = 0;
  const sendEvent = (event, payload = {}) => {
    if (ws.readyState === 1) ws.send(JSON.stringify({ event, seq: ++seq, ...payload }));
  };

  ws.on("message", (raw) => {
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.event === "chat" || msg.event === "message") {
        // Echo a user message back as an assistant response after a short delay.
        const userText = msg.text || msg.content || "";
        sendEvent("tool_start", { tool: "echo", args: {}, title: "echo" });
        setTimeout(() => {
          sendEvent("tool_output", { tool: "echo", output: "Received: " + userText, status: "ok" });
          sendEvent("delta", { agent: "commander", delta: "I received your message: \"" + userText + "\"\n\nThis is a demo response from the QA fake backend." });
          sendEvent("done", {});
        }, 600);
      } else if (msg.event === "ping") {
        sendEvent("pong", {});
      }
    } catch (e) {
      console.error("[ws] parse error", e);
    }
  });

  ws.on("close", () => console.log(`[ws] chat disconnected: ${uid}`));
  ws.on("error", (e) => console.error("[ws] error", e.message));
});

/* ============================================================================
   WebSocket: /ws/screen — noVNC relay (accept + echo handshake)
   ========================================================================== */

const screenWss = new WebSocketServer({ noServer: true });
screenWss.on("connection", (ws) => {
  console.log("[ws] screen connected");
  ws.on("message", () => {});
  ws.on("close", () => console.log("[ws] screen disconnected"));
});

server.on("upgrade", (req, socket, head) => {
  const parsed = urlModule.parse(req.url, true);
  if (parsed.pathname === "/ws/screen") {
    screenWss.handleUpgrade(req, socket, head, (ws) => screenWss.emit("connection", ws, req));
    return;
  }
  const m = parsed.pathname.match(/^\/ws\/([^\/]+)$/);
  if (m) {
    wss.handleUpgrade(req, socket, head, (ws) => wss.emit("connection", ws, req));
    return;
  }
  socket.destroy();
});

server.listen(PORT, () => {
  console.log(`fake backend listening on :${PORT}`);
});
