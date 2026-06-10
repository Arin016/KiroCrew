const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  DEFAULT_REMOTE_BIN,
  REMOTE_BIN_CANDIDATES,
  buildCandidateTokenCommand,
  buildRemoteTokenCommand,
  parseTokenFromStdout,
} = require("../remote-token");

describe("REMOTE_BIN_CANDIDATES", () => {
  it("lists the toolbox path first (most common install)", () => {
    assert.equal(REMOTE_BIN_CANDIDATES[0], "$HOME/.toolbox/bin/kiroclaw");
  });

  it("includes the legacy default path", () => {
    assert.ok(REMOTE_BIN_CANDIDATES.includes(DEFAULT_REMOTE_BIN));
  });

  it("uses only $HOME-prefixed paths (no PATH reliance)", () => {
    for (const c of REMOTE_BIN_CANDIDATES) {
      assert.ok(
        c.startsWith("$HOME/") || c.startsWith("/"),
        `candidate ${c} must be absolute or $HOME-prefixed`,
      );
    }
  });
});

describe("buildCandidateTokenCommand", () => {
  it("produces a shell command that tries each candidate in order", () => {
    const cmd = buildCandidateTokenCommand(["$HOME/a", "$HOME/b"]);
    const aIdx = cmd.indexOf('"$HOME/a"');
    const bIdx = cmd.indexOf('"$HOME/b"');
    assert.ok(aIdx !== -1 && bIdx !== -1);
    assert.ok(aIdx < bIdx, "first candidate must appear before second");
  });

  it("prefixes PATH so toolbox wrapper can find kiro-cli", () => {
    const cmd = buildCandidateTokenCommand(REMOTE_BIN_CANDIDATES);
    assert.match(cmd, /export PATH="\$HOME\/\.toolbox\/bin:\$PATH"/);
  });

  it("tests -x directly on $b (for-list already expands $HOME at parse time)", () => {
    const cmd = buildCandidateTokenCommand(REMOTE_BIN_CANDIDATES);
    assert.match(cmd, /\[ -x "\$b" \]/);
    assert.match(cmd, /exec "\$b" token/);
    // Guard against regressing to the needlessly-complex eval form.
    assert.doesNotMatch(cmd, /eval echo/);
  });

  it("exits with 127 and prints all candidates when none are executable", () => {
    const cmd = buildCandidateTokenCommand(["$HOME/a", "$HOME/b"]);
    assert.match(cmd, /exit 127/);
    assert.match(cmd, /\$HOME\/a, \$HOME\/b/);
  });
});

describe("buildRemoteTokenCommand", () => {
  it("uses candidate list when binPath is the default sentinel", () => {
    const cmd = buildRemoteTokenCommand(DEFAULT_REMOTE_BIN);
    // candidate command has a for-loop; custom-path command does not
    assert.match(cmd, /for b in /);
  });

  it("uses candidate list when binPath is empty", () => {
    const cmd = buildRemoteTokenCommand("");
    assert.match(cmd, /for b in /);
  });

  it("respects a user-customized binPath", () => {
    const cmd = buildRemoteTokenCommand("/opt/custom/kiroclaw");
    assert.doesNotMatch(cmd, /for b in /);
    assert.match(cmd, /"\/opt\/custom\/kiroclaw" token/);
  });

  it("PATH-prefixes the user-customized path so toolbox wrapper works", () => {
    const cmd = buildRemoteTokenCommand("$HOME/.toolbox/bin/kiroclaw");
    assert.match(cmd, /export PATH="\$HOME\/\.toolbox\/bin:\$PATH"/);
  });

  it("rewrites a leading ~/ to $HOME/ so it expands inside double quotes", () => {
    const cmd = buildRemoteTokenCommand("~/.local/bin/kiroclaw");
    assert.match(cmd, /"\$HOME\/\.local\/bin\/kiroclaw" token/);
    assert.doesNotMatch(cmd, /"~\//);
  });

  it("leaves absolute and $HOME-prefixed paths untouched", () => {
    assert.match(buildRemoteTokenCommand("/opt/x/kiroclaw"), /"\/opt\/x\/kiroclaw" token/);
    assert.match(buildRemoteTokenCommand("$HOME/x/kiroclaw"), /"\$HOME\/x\/kiroclaw" token/);
  });

  it("accepts a custom candidate list for testing/extension", () => {
    const cmd = buildRemoteTokenCommand(DEFAULT_REMOTE_BIN, ["$HOME/x"]);
    assert.match(cmd, /"\$HOME\/x"/);
    assert.doesNotMatch(cmd, /\.toolbox\/bin\/kiroclaw"/);
  });
});

describe("parseTokenFromStdout", () => {
  it("extracts token from standard URL", () => {
    const url = "http://localhost:8765?token=eyJhbGciOiJIUzI1NiJ9";
    assert.equal(parseTokenFromStdout(url), "eyJhbGciOiJIUzI1NiJ9");
  });

  it("extracts token when it's not the only query param", () => {
    const url = "http://host/?foo=bar&token=abc123";
    assert.equal(parseTokenFromStdout(url), "abc123");
  });

  it("handles trailing whitespace/newlines", () => {
    assert.equal(parseTokenFromStdout("http://x?token=xyz\n"), "xyz");
  });

  it("returns empty string when no token is present", () => {
    assert.equal(parseTokenFromStdout("random output"), "");
    assert.equal(parseTokenFromStdout(""), "");
  });

  it("stops at ampersand (doesn't eat following params)", () => {
    const url = "http://x?token=abc&session_exp=99999";
    assert.equal(parseTokenFromStdout(url), "abc");
  });
});
