const { test } = require("node:test");
const assert = require("node:assert");
const { channelForFlavor, buildFeedUrl } = require("../auto-update");

test("channelForFlavor maps beta -> insider", () => {
  assert.strictEqual(channelForFlavor("beta"), "insider");
});

test("channelForFlavor maps stable -> stable", () => {
  assert.strictEqual(channelForFlavor("stable"), "stable");
});

test("channelForFlavor defaults non-beta to stable", () => {
  assert.strictEqual(channelForFlavor(undefined), "stable");
  assert.strictEqual(channelForFlavor("anything"), "stable");
});

test("buildFeedUrl assembles platform/channel/version query", () => {
  const url = buildFeedUrl({
    base: "https://updates.example.dev/feed",
    platform: "darwin-arm64",
    channel: "insider",
    version: "1.2.3",
  });
  const u = new URL(url);
  assert.strictEqual(u.origin + u.pathname, "https://updates.example.dev/feed");
  assert.strictEqual(u.searchParams.get("platform"), "darwin-arm64");
  assert.strictEqual(u.searchParams.get("channel"), "insider");
  assert.strictEqual(u.searchParams.get("version"), "1.2.3");
});

test("buildFeedUrl strips trailing slashes from base", () => {
  const url = buildFeedUrl({
    base: "https://updates.example.dev/feed///",
    platform: "darwin-arm64",
    channel: "stable",
    version: "2.0.0",
  });
  assert.ok(url.startsWith("https://updates.example.dev/feed?"));
});

test("buildFeedUrl url-encodes version values", () => {
  const url = buildFeedUrl({
    base: "https://u.dev/feed",
    platform: "darwin-arm64",
    channel: "stable",
    version: "1.2.3-beta+build.5",
  });
  const u = new URL(url);
  assert.strictEqual(u.searchParams.get("version"), "1.2.3-beta+build.5");
});
