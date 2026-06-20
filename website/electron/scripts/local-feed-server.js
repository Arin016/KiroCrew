#!/usr/bin/env node
/**
 * Local Squirrel.Mac update feed for Stage 3 auto-update testing.
 *
 * Binds 127.0.0.1 ONLY. Implements the Squirrel.Mac contract the app's
 * autoUpdater expects:
 *   GET /<anything>?version=X  -> 200 {url,name,notes,pub_date}  if X < latest
 *                              -> 204 (no content)               if X >= latest
 *   GET /download              -> streams the update .zip
 *
 * The app's buildFeedUrl() appends ?platform=&channel=&version= ; this server
 * only reads `version` and compares against the served latest version.
 *
 * Usage:
 *   node local-feed-server.js --port 8799 --zip /path/Kiro-1.0.1-mac.zip --version 1.0.1
 */
const http = require("http");
const fs = require("fs");

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : def;
}
const PORT = parseInt(arg("port", "8799"), 10);
const ZIP = arg("zip");
const LATEST = arg("version", "1.0.1");

if (!ZIP || !fs.existsSync(ZIP)) {
  console.error("ERROR: --zip <path> is required and must exist");
  process.exit(1);
}

// Minimal 3-part semver compare (ignores prerelease/build for the test).
function cmpSemver(a, b) {
  const pa = String(a).split(".").map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < 3; i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d !== 0) return d;
  }
  return 0;
}

const server = http.createServer((req, res) => {
  const u = new URL(req.url, `http://127.0.0.1:${PORT}`);

  if (u.pathname === "/download") {
    const stat = fs.statSync(ZIP);
    res.writeHead(200, { "Content-Type": "application/zip", "Content-Length": stat.size });
    fs.createReadStream(ZIP).pipe(res);
    console.log(`[feed] served update zip (${stat.size} bytes)`);
    return;
  }

  const reqVersion = u.searchParams.get("version") || "0.0.0";
  if (cmpSemver(reqVersion, LATEST) < 0) {
    const body = JSON.stringify({
      url: `http://127.0.0.1:${PORT}/download`,
      name: LATEST,
      notes: `Stage 3 local test update to ${LATEST}`,
      pub_date: new Date().toISOString(),
    });
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(body);
    console.log(`[feed] version ${reqVersion} < ${LATEST} -> 200 update available`);
  } else {
    res.writeHead(204);
    res.end();
    console.log(`[feed] version ${reqVersion} >= ${LATEST} -> 204 up to date`);
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[feed] listening on http://127.0.0.1:${PORT}`);
  console.log(`[feed] serving latest=${LATEST} from ${ZIP}`);
});
