// Input validation for remote tunnel settings.
// Extracted from main.js so it can be unit-tested without Electron.

// DNS label: starts/ends with alnum, hyphens allowed in the middle. Labels joined by single dots.
const HOSTNAME_RE = /^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$/;
// Filesystem path: alnum, ~, /, ., _, - only. $HOME allowed at start for remote expansion.
const BINPATH_RE = /^(\$HOME\/)?[a-zA-Z0-9~\/._][a-zA-Z0-9\/._~-]*$/;

function validateRemoteSettings(host, bin) {
  if (host && host.length > 253) return "Hostname too long (max 253 characters).";
  if (host && !HOSTNAME_RE.test(host)) return "Invalid hostname — must be a valid DNS name (e.g. myhost.corp.amazon.com).";
  if (bin && bin.startsWith("-")) return "Binary path must not start with a dash.";
  if (bin && bin.includes("..")) return "Binary path must not contain path traversal (../).";
  if (bin && !BINPATH_RE.test(bin)) return "Invalid binary path — only filesystem path characters allowed (no spaces).";
  return null;
}

module.exports = { HOSTNAME_RE, BINPATH_RE, validateRemoteSettings };
