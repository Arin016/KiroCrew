const { test } = require("node:test");
const assert = require("node:assert");
const http = require("http");
const os = require("os");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const { postShutdown, stopGatewayGracefully } = require("../gateway-stop");

// Helper: temp KIROCLAW_HOME containing a .local_secret file.
function tmpHomeWithSecret(secret) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gw-stop-"));
  if (secret !== null) fs.writeFileSync(path.join(dir, ".local_secret"), secret);
  return dir;
}

// Helper: a long-lived child. ignoreSigterm=true => only SIGKILL can stop it.
// Prints "ready" once its signal handler is registered so tests don't send
// signals during the child's startup window (before the handler exists).
function spawnDummy({ ignoreSigterm = false } = {}) {
  const code = ignoreSigterm
    ? "process.on('SIGTERM',()=>{}); console.log('ready'); setInterval(()=>{}, 1e9);"
    : "console.log('ready'); setInterval(()=>{}, 1e9);"; // default: SIGTERM terminates
  return spawn(process.execPath, ["-e", code]);
}

// Resolve once the child has printed "ready" (handler registered, loop running).
function waitReady(proc) {
  return new Promise((resolve) => {
    let buf = "";
    const onData = (d) => {
      buf += d.toString();
      if (buf.includes("ready")) { proc.stdout.off("data", onData); resolve(); }
    };
    proc.stdout.on("data", onData);
  });
}

// Helper: local server implementing the /api/shutdown contract.
// onShutdown(req) lets a test simulate the gateway exiting itself on 200.
function startServer({ secret, status = 200, onShutdown }) {
  const server = http.createServer((req, res) => {
    if (req.method === "POST" && req.url === "/api/shutdown") {
      const ok = req.headers["x-local-secret"] === secret;
      if (!ok) { res.writeHead(403); return res.end('{"error":"invalid secret"}'); }
      res.writeHead(status); res.end(status === 200 ? '{"ok":true}' : "{}");
      if (status === 200 && onShutdown) onShutdown(req);
      return;
    }
    res.writeHead(404); res.end();
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, port: server.address().port });
    });
  });
}

test("postShutdown returns true on 200 with correct secret", async () => {
  const home = tmpHomeWithSecret("s3cr3t");
  const { server, port } = await startServer({ secret: "s3cr3t", status: 200 });
  try {
    const ok = await postShutdown({ backendUrl: `http://127.0.0.1:${port}`, kiroclawHome: home });
    assert.strictEqual(ok, true);
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("postShutdown returns false on 403 (wrong secret)", async () => {
  const home = tmpHomeWithSecret("wrong");
  const { server, port } = await startServer({ secret: "right", status: 200 });
  try {
    const ok = await postShutdown({ backendUrl: `http://127.0.0.1:${port}`, kiroclawHome: home });
    assert.strictEqual(ok, false);
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("postShutdown returns false when no secret file exists", async () => {
  const home = tmpHomeWithSecret(null);
  const ok = await postShutdown({ backendUrl: "http://127.0.0.1:1", kiroclawHome: home });
  assert.strictEqual(ok, false);
  fs.rmSync(home, { recursive: true, force: true });
});

test("stopGatewayGracefully: happy path — endpoint exits process, no signal needed", async () => {
  const home = tmpHomeWithSecret("s3cr3t");
  const proc = spawnDummy({ ignoreSigterm: true }); // proves SIGTERM was NOT used
  await waitReady(proc);
  // Server kills the child on 200, simulating the gateway exiting itself.
  const { server, port } = await startServer({
    secret: "s3cr3t", status: 200, onShutdown: () => proc.kill("SIGKILL"),
  });
  try {
    await stopGatewayGracefully(proc, {
      backendUrl: `http://127.0.0.1:${port}`, kiroclawHome: home, timeoutMs: 10000,
    });
    assert.notStrictEqual(proc.exitCode === null && proc.signalCode === null, true, "process should be gone");
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("stopGatewayGracefully: SIGTERM fallback when endpoint fails", async () => {
  const home = tmpHomeWithSecret("s3cr3t");
  const proc = spawnDummy({ ignoreSigterm: false }); // exits on SIGTERM
  await waitReady(proc);
  const { server, port } = await startServer({ secret: "s3cr3t", status: 500 }); // endpoint fails
  try {
    await stopGatewayGracefully(proc, {
      backendUrl: `http://127.0.0.1:${port}`, kiroclawHome: home, timeoutMs: 10000,
    });
    assert.strictEqual(proc.signalCode, "SIGTERM");
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("stopGatewayGracefully: SIGKILL fallback when SIGTERM ignored", async () => {
  const home = tmpHomeWithSecret("s3cr3t");
  const proc = spawnDummy({ ignoreSigterm: true }); // ignores SIGTERM
  await waitReady(proc);
  const { server, port } = await startServer({ secret: "s3cr3t", status: 500 });
  try {
    await stopGatewayGracefully(proc, {
      backendUrl: `http://127.0.0.1:${port}`, kiroclawHome: home, timeoutMs: 800,
    });
    assert.strictEqual(proc.signalCode, "SIGKILL");
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("stopGatewayGracefully: no-op on already-dead process", async () => {
  const proc = spawnDummy({ ignoreSigterm: false });
  await new Promise((r) => { proc.once("exit", r); proc.kill("SIGKILL"); });
  // Should resolve immediately without throwing.
  await stopGatewayGracefully(proc, { backendUrl: "http://127.0.0.1:1", kiroclawHome: "/nope", timeoutMs: 500 });
  assert.ok(true);
});
