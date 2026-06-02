// Helpers for Remote Tunnel mode: build the command executed on the remote
// dev desktop over SSH to fetch a KiroClaw dashboard token.
//
// Split out from main.js so the shell-construction logic can be unit-tested
// without spinning up Electron.

const DEFAULT_REMOTE_BIN = "$HOME/.local/bin/kiroclaw";

// Non-interactive SSH shells don't source ~/.zshrc, so PATH won't include
// `~/.toolbox/bin`. Each candidate is a full path so the remote shell can
// exec it directly without relying on PATH.
const REMOTE_BIN_CANDIDATES = [
  "$HOME/.toolbox/bin/kiroclaw",           // toolbox install (recommended per wiki)
  "$HOME/.local/bin/kiroclaw",             // install.sh / source install
  "$HOME/.kiroclaw-app/.venv/bin/kiroclaw", // one-liner installer venv
];

// Build a shell snippet that tries each candidate path in order and execs the
// first one that's executable. PATH is prefixed so the toolbox wrapper can
// find kiro-cli over non-interactive SSH.
//
// The `for b in "$HOME/a" "$HOME/b"` list expands $HOME at parse time (the
// candidates are inside double quotes), so `$b` already holds the fully
// resolved path — no `eval` needed for the -x test or the exec.
//
// Candidates are hard-coded literals (no user input) so double-quote embedding
// is safe. User-supplied binPath goes through validateRemoteSettings() and is
// handled separately in buildRemoteTokenCommand().
function buildCandidateTokenCommand(candidates) {
  const expanded = candidates.map((p) => `"${p}"`).join(" ");
  return [
    'export PATH="$HOME/.toolbox/bin:$PATH";',
    `for b in ${expanded}; do`,
    '  if [ -x "$b" ]; then',
    '    exec "$b" token;',
    '  fi;',
    'done;',
    `echo "kiroclaw binary not found in any of: ${candidates.join(", ")}" >&2;`,
    'exit 127',
  ].join(" ");
}

// Pick the right remote command given the user's stored binPath.
//   - If binPath is the default sentinel, try every candidate in order.
//   - Otherwise respect the user's customization and PATH-prefix the exec so
//     the toolbox wrapper resolves kiro-cli correctly over SSH.
function buildRemoteTokenCommand(binPath, candidates = REMOTE_BIN_CANDIDATES) {
  if (!binPath || binPath === DEFAULT_REMOTE_BIN) {
    return buildCandidateTokenCommand(candidates);
  }
  return `export PATH="$HOME/.toolbox/bin:$PATH"; "${binPath}" token`;
}

// Extract the JWT from a `kiroclaw token` URL. The command prints:
//   http://localhost:7777?token=eyJ...
// or in some configurations `https://.../?token=...&foo=bar` — match either.
function parseTokenFromStdout(stdout) {
  const match = stdout.trim().match(/[?&]token=([^\s&]+)/);
  return match ? match[1] : "";
}

module.exports = {
  DEFAULT_REMOTE_BIN,
  REMOTE_BIN_CANDIDATES,
  buildCandidateTokenCommand,
  buildRemoteTokenCommand,
  parseTokenFromStdout,
};
