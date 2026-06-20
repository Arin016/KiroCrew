/**
 * Graceful gateway stop, extracted from main.js for testability.
 *
 * The embedded Python gateway is a long-running child process. Before quit
 * (and before any Squirrel auto-update bundle swap) it must be stopped
 * cleanly: POST /api/shutdown so it flushes session/memory/cron state and
 * exits itself, falling back to SIGTERM then SIGKILL. main.js injects the live
 * child process + module-level config; tests inject a real spawned process and
 * a local HTTP server. Deps (http/fs/path/timers) are injectable so the logic
 * is unit-testable without Electron.
 */

const http = require("http");
const fs = require("fs");
const path = require("path");

/**
 * POST /api/shutdown with the local secret (mirrors the dashboard's
 * X-Local-Secret auth). Resolves true on HTTP 200, false on any failure
 * (missing secret, connection error, timeout, non-200) so the caller can fall
 * back to signals.
 *
 * @returns {Promise<boolean>}
 */
function postShutdown({
  backendUrl,
  kiroclawHome,
  httpMod = http,
  fsMod = fs,
  pathMod = path,
  timeoutMs = 5000,
}) {
  return new Promise((resolve) => {
    let secret = "";
    try {
      secret = fsMod.readFileSync(pathMod.join(kiroclawHome, ".local_secret"), "utf8").trim();
    } catch {
      return resolve(false);
    }
    if (!secret) return resolve(false);
    let u;
    try { u = new URL(`${backendUrl}/api/shutdown`); } catch { return resolve(false); }
    const req = httpMod.request(
      {
        hostname: u.hostname,
        port: u.port,
        path: u.pathname,
        method: "POST",
        headers: { "X-Local-Secret": secret },
        timeout: timeoutMs,
      },
      (res) => { res.resume(); resolve(res.statusCode === 200); }
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.end();
  });
}

/**
 * Stop the gateway child gracefully and await its exit.
 *   1. POST /api/shutdown (clean flush + self-exit)
 *   2. SIGTERM if the endpoint didn't take (older gateway / unreachable)
 *   3. SIGKILL if it still hasn't exited within timeoutMs
 * Resolves once the process is fully gone — callers (quit / auto-update) rely
 * on the exit having completed before proceeding.
 *
 * @param {import("child_process").ChildProcess} proc
 * @param {object} opts
 * @returns {Promise<void>}
 */
async function stopGatewayGracefully(
  proc,
  {
    backendUrl,
    kiroclawHome,
    timeoutMs = 15000,
    postShutdownFn = postShutdown,
    httpMod,
    fsMod,
    pathMod,
  } = {}
) {
  if (!proc || proc.exitCode !== null) return;
  await new Promise((resolve) => {
    let settled = false;
    const done = () => { if (!settled) { settled = true; resolve(); } };
    proc.once("exit", done);
    if (proc.exitCode !== null) return done();
    // Send SIGKILL at timeoutMs but DON'T resolve here — wait for the real
    // 'exit' so callers are guaranteed the process is gone (and signalCode is
    // accurate). A hard safety net resolves even if 'exit' never fires.
    const killTimer = setTimeout(() => {
      if (proc.exitCode === null) { try { proc.kill("SIGKILL"); } catch {} }
    }, timeoutMs);
    const hardTimer = setTimeout(done, timeoutMs + 3000);
    proc.once("exit", () => { clearTimeout(killTimer); clearTimeout(hardTimer); });
    // Prefer the clean endpoint; signal-nudge only if it didn't take.
    postShutdownFn({ backendUrl, kiroclawHome, httpMod, fsMod, pathMod }).then((ok) => {
      if (!ok && proc.exitCode === null) { try { proc.kill("SIGTERM"); } catch {} }
    });
  });
}

module.exports = { postShutdown, stopGatewayGracefully };
