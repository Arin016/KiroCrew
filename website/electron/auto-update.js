/**
 * Desktop auto-update via Electron's native autoUpdater (Squirrel.Mac).
 *
 * Squirrel.framework + ShipIt are already in the signed app bundle, so this
 * only wires the updater to a feed and drives the install. The ONE
 * KiroCrew-specific concern vs. a plain Electron app: the bundled Python
 * gateway is a long-running child process, so it MUST be stopped gracefully
 * BEFORE Squirrel swaps the .app bundle — otherwise ShipIt can race the swap
 * and leave a half-replaced app. The graceful stopper is injected from main.js
 * (it calls POST /api/shutdown to flush state, then SIGTERM/SIGKILL).
 *
 * Pure helpers (channelForFlavor, buildFeedUrl) are dependency-free and tested
 * directly. initAutoUpdate takes electron modules + callbacks injected so it
 * stays testable without an Electron runtime.
 */

// Default update feed host: updates.crew.kiro.dev, the pointer hostname of
// the public distribution CDN (CloudFront + OAC over the kirocrew-updates
// bucket). The feed is a STATIC JSON file at
// <base>/<channel>/<latest-mac.json | latest-win.json> written by CI after
// the platform's publish lane; the artifact URLs inside it point at the byte
// hostname (download.crew.kiro.dev, CI's CLI_CDN_BASE). There is no 200/204
// server endpoint: safeCheck() fetches the pointer itself and compares
// versions CLIENT-SIDE, engaging Squirrel only when the feed version differs
// from the running app. (Squirrel treats any 200 feed response as "update
// available", so gating on the client compare is what prevents a re-download
// loop against a static file.)
//
// One asymmetry, contained to configureFeed()/startDownload(): Squirrel.Mac
// consumes this JSON directly, while Squirrel.Windows consumes a DIRECTORY
// (RELEASES + .nupkg resolved relative to it) that the feed body names in
// its `releases` field.
const DEFAULT_FEED_BASE = "https://updates.crew.kiro.dev/feed";
const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // every 4h while running
const LAUNCH_CHECK_DELAY_MS = 30 * 1000; // let startup settle first
const FORCE_EXIT_AFTER_MS = 5 * 1000; // failsafe: guarantee exit after quitAndInstall
const FEED_TIMEOUT_MS = 15 * 1000;
const FEED_MAX_BYTES = 64 * 1024;

/**
 * Map the build flavor ("beta" | "stable") to an update channel. Retained
 * for the internal beta flavor and as the fallback when the running version
 * carries no channel marker.
 * @param {"beta"|"stable"} flavor
 * @returns {"insider"|"stable"}
 */
function channelForFlavor(flavor) {
  return flavor === "beta" ? "insider" : "stable";
}

/**
 * Derive the update channel from the running version. CI stamps the app
 * version per channel (nightly.yml: <base>-nightly.<stamp>; release.yml:
 * tag-derived), so the version itself says which feed this build must
 * track. MUST mirror release.yml's tag-to-channel rule: "-nightly." is
 * nightly, any OTHER prerelease suffix (-insider.N, -rc.N, ...) is
 * insider, bare semver is stable. Without this, a nightly/insider build
 * would check the stable feed, see a differing version, and silently
 * migrate the user onto stable.
 * @param {string} version
 * @returns {"nightly"|"insider"|"stable"|null} null when unstamped (dev)
 */
function channelForVersion(version) {
  if (!version || typeof version !== "string") return null;
  if (version.includes("-nightly.")) return "nightly";
  if (version.includes("-")) return "insider";
  return "stable";
}

/**
 * Resolve the EFFECTIVE update channel from the build stamp + the user's
 * channel preference (the Settings > About switcher).
 *
 * Rules (stable ⇄ insider opt-in design):
 * - nightly-stamped builds are PINNED to nightly: the nightly app is a
 *   separate side-by-side install, and honoring a preference here would
 *   migrate the dev app onto a production channel.
 * - unstamped (dev, stamped === null) builds have no update lane; the
 *   preference cannot conjure one.
 * - production stamps (insider/stable) follow the preference when set,
 *   else their own stamp. Switching BACK can be a downgrade mid-cycle
 *   (insider 0.2.0-insider.1 -> stable 0.1.0); safeCheck's compare gate
 *   deliberately engages on any version DIFFERENCE, so that works.
 *
 * @param {"nightly"|"insider"|"stable"|null} stamped - channelForVersion(version)
 * @param {"insider"|"stable"|""|null|undefined} preference - user opt-in, falsy = follow stamp
 * @returns {"nightly"|"insider"|"stable"|null}
 */
function resolveChannel(stamped, preference) {
  if (stamped === "nightly") return "nightly";
  if (stamped === null) return null;
  if (preference === "insider" || preference === "stable") return preference;
  return stamped;
}

/**
 * Per-platform channel-pointer filename. The pointer is OUR json (version +
 * payload URLs + sha256), written by CI per channel; it is what drives the
 * client-side version compare and the consent card on every platform. The
 * platform-specific part is only which file and which payload field:
 *
 *   darwin -> latest-mac.json  { version, url (zip), dmg, ... }
 *   win32  -> latest-win.json  { version, setup, releases (Squirrel dir), nupkg, sha256, ... }
 *
 * Linux has no pointer yet (no updater consumes one), so it is absent here
 * and the platform guard in initAutoUpdate keeps auto-update disabled there.
 */
const FEED_FILENAME = Object.freeze({
  darwin: "latest-mac.json",
  win32: "latest-win.json",
});

/**
 * Build the static channel-pointer URL for a platform + channel. Pure + testable.
 *
 * `platform` is REQUIRED and is an os key (process.platform), not a
 * "darwin-arm64" display string. It is deliberately not defaulted: silently
 * falling back to the mac pointer would serve Windows clients a feed whose
 * payload field they cannot use, and the failure would surface as a confusing
 * "feed missing ..." error rather than a wiring mistake.
 *
 * @param {{base:string, channel:string, platform:string}} o
 * @returns {string}
 */
function buildFeedUrl({ base, channel, platform }) {
  const filename = FEED_FILENAME[platform];
  if (!filename) {
    throw new Error(`no update feed for platform ${String(platform)}`);
  }
  const b = (base || DEFAULT_FEED_BASE).replace(/\/+$/, "");
  return `${b}/${encodeURIComponent(channel)}/${filename}`;
}

/**
 * Default feed fetcher: GET the static feed JSON. Injectable via
 * deps.fetchFeed for tests. Bounded body size + timeout; rejects on any
 * non-200 so callers surface a single error path. HTTPS everywhere;
 * plain HTTP is permitted ONLY for loopback hosts so the local update
 * harness (KIROCREW_UPDATE_FEED=http://127.0.0.1:PORT/...) works --
 * cleartext update metadata over a real network stays rejected.
 * @param {string} url
 * @returns {Promise<{version:string, url:string}>}
 */
function fetchFeedHttps(url) {
  return new Promise((resolve, reject) => {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (err) {
      reject(err);
      return;
    }
    const isLoopback = ["127.0.0.1", "localhost", "[::1]", "::1"].includes(parsed.hostname);
    let mod;
    if (parsed.protocol === "https:") {
      mod = require("https");
    } else if (parsed.protocol === "http:" && isLoopback) {
      mod = require("http");
    } else {
      reject(new Error(`feed URL must be https (or http on loopback): ${parsed.protocol}//${parsed.hostname}`));
      return;
    }
    const req = mod.get(url, { headers: { "cache-control": "no-cache" } }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error(`feed HTTP ${res.statusCode}`));
        return;
      }
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        body += chunk;
        if (body.length > FEED_MAX_BYTES) req.destroy(new Error("feed response too large"));
      });
      res.on("end", () => {
        try { resolve(JSON.parse(body)); } catch (err) { reject(err); }
      });
    });
    req.on("error", reject);
    req.setTimeout(FEED_TIMEOUT_MS, () => req.destroy(new Error("feed request timed out")));
  });
}

/**
 * Wire Electron's autoUpdater. All Electron surfaces injected for testability.
 *
 * @param {object} deps
 * @param {import("electron").App} deps.app
 * @param {import("electron").AutoUpdater} deps.autoUpdater
 * @param {typeof import("electron").dialog} deps.dialog
 * @param {typeof import("electron").Notification} deps.Notification
 * @param {() => string} deps.getFlavor      - returns "beta" | "stable"
 * @param {() => Promise<void>} deps.stopGateway - graceful, awaitable gateway stop
 * @param {string} [deps.platform]           - e.g. "darwin-arm64"
 * @param {string} [deps.feedBase]           - override feed host
 * @param {(state:object) => void} [deps.onUpdateState] - if provided, the
 *   in-app UI drives the install prompt: state transitions are pushed here
 *   ({state, version, notes, channel}) and the native dialog is suppressed.
 *   Without it, the native dialog is the fallback prompt.
 * @param {{info:Function,warn:Function,error:Function}} [deps.log]
 * @returns {{check:Function, install:Function, getInfo:Function}} renderer-callable triggers
 */
function initAutoUpdate(deps) {
  const {
    app,
    autoUpdater,
    dialog,
    Notification,
    getFlavor,
    getChannelPreference = () => "",
    notifyUpdateFound = null,
    stopGateway,
    platform = "darwin-arm64",
    feedBase = process.env.KIROCREW_UPDATE_FEED || DEFAULT_FEED_BASE,
    fetchFeed = fetchFeedHttps,
    onUpdateState = null,
    log = console,
  } = deps;

  // When the in-app UI is wired (onUpdateState provided), it owns the prompt;
  // the native dialog stays as the fallback for headless / no-renderer cases.
  const uiDriven = typeof onUpdateState === "function";
  // Single channel resolver used for the feed AND everything reported to
  // the UI. Read the preference FRESH on every call: configureFeed() runs
  // per check, so a Settings channel switch takes effect on the next check
  // with no re-init. Flavor stays the unstamped-dev display fallback.
  function currentChannel() {
    const stamped = channelForVersion(app.getVersion());
    return resolveChannel(stamped, getChannelPreference()) || channelForFlavor(getFlavor());
  }
  function emit(state, extra = {}) {
    if (!uiDriven) return;
    try {
      onUpdateState({ state, channel: currentChannel(), version: app.getVersion(), ...extra });
    } catch (err) {
      log.error("[update] onUpdateState threw", err);
    }
  }
  function getInfo() {
    const stamped = channelForVersion(app.getVersion());
    return {
      version: app.getVersion(),
      channel: currentChannel(),
      // Switcher inputs: the build's own lane, whether this build may switch
      // (nightly is pinned; dev has no lane), and the stored preference.
      stampedChannel: stamped,
      channelSwitchable: stamped === "insider" || stamped === "stable",
      channelPreference: getChannelPreference() || "",
      platform,
      packaged: !!app.isPackaged,
    };
  }

  // Squirrel is unavailable for unsigned / not-installed dev builds.
  if (!app.isPackaged) {
    log.info("[update] dev build — auto-update disabled");
    return { check: () => {}, install: async () => {}, getInfo, disabled: "dev" };
  }
  // Platforms with a published channel pointer AND a Squirrel implementation
  // Electron's built-in autoUpdater can drive: macOS (Squirrel.Mac) and
  // Windows (Squirrel.Windows). Linux has neither a pointer nor an updater
  // (the AppImage is replaced by hand), so it stays disabled and the About
  // panel reports `disabled: "platform"` rather than showing dead controls.
  const osPlatform = process.platform;
  if (!FEED_FILENAME[osPlatform]) {
    log.info(`[update] ${osPlatform} — auto-update disabled (no channel feed / updater)`);
    return { check: () => {}, install: async () => {}, getInfo, disabled: "platform" };
  }
  const isWindows = osPlatform === "win32";

  let updateReady = false;
  let downloading = false; // Squirrel download/extract in flight
  let stagedVersion = null; // version name Squirrel has downloaded + staged
  let stagedNotes = "";
  let installing = false;
  let quitHandled = false;

  /**
   * Resolve this check's channel-pointer URL, and on macOS also hand it to
   * Squirrel.
   *
   * The platforms diverge here, and only here. Squirrel.MAC consumes a JSON
   * feed, so the pointer URL IS its feed URL. Squirrel.WINDOWS consumes a
   * DIRECTORY: it fetches `RELEASES` from it and resolves each `.nupkg`
   * relative to it. That directory is not derivable from the pointer host --
   * pointers live on the updates hostname, the Squirrel directory on the byte
   * hostname (its protocol couples RELEASES and the payloads into one prefix,
   * so publish-windows.yml puts the whole directory on the byte host). Rather
   * than hardcode a second base in the client, the feed body TELLS us the
   * directory (`releases`), and it is applied at consent time in
   * startDownload(). That keeps the server authoritative: the directory can
   * move without shipping a client.
   */
  function configureFeed() {
    const channel = currentChannel();
    const url = buildFeedUrl({ base: feedBase, channel, platform: osPlatform });
    if (!isWindows) {
      autoUpdater.setFeedURL({ url });
    }
    log.info(`[update] feed: ${url}`);
    return url;
  }

  /**
   * Validate + normalize the Squirrel.Windows directory URL taken from the
   * feed body. Same transport discipline as fetchFeedHttps: HTTPS everywhere,
   * plain HTTP only for loopback so the local update harness works. A
   * trailing slash is required by Squirrel's relative resolution, so add one
   * if the feed omitted it.
   * @param {string} raw
   * @returns {string}
   */
  function squirrelDirFromFeed(raw) {
    if (typeof raw !== "string" || !raw) {
      throw new Error("feed missing releases (Squirrel directory) for win32");
    }
    const parsed = new URL(raw);
    const isLoopback = ["127.0.0.1", "localhost", "[::1]", "::1"].includes(parsed.hostname);
    if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isLoopback)) {
      throw new Error(`refusing non-HTTPS Squirrel directory: ${parsed.protocol}//${parsed.hostname}`);
    }
    return raw.endsWith("/") ? raw : `${raw}/`;
  }

  let checking = false;
  let foundFeed = null; // last feed entry surfaced to the user, awaiting consent
  async function safeCheck() {
    // NOTE: no updateReady short-circuit here. A check ALWAYS consults the
    // feed and reports state (macOS Software Update semantics) — the silent
    // `return` this replaces made the Check-for-updates button a dead no-op
    // once a download had been staged.
    if (checking) return;
    if (downloading) {
      // Squirrel is mid-download/extract. Re-engaging checkForUpdates() now
      // restarts its update flow and tears down the temp staging dir under
      // the in-flight extraction (observed in the field as
      // "ditto: Could not lstat .../update.XXXX/...: No such file or
      // directory"). Report progress instead; update-downloaded/error will
      // clear the flag and the next check proceeds normally.
      log.info("[update] check requested while download in flight — reporting progress");
      emit("downloading");
      return;
    }
    checking = true;
    try {
      const url = configureFeed(); // re-read flavor/channel each check
      emit("checking");
      const feed = await fetchFeed(url);
      // Per-platform payload field. Rejecting the wrong shape here (rather
      // than discovering it when Squirrel is engaged) is what turns a
      // mis-published feed into a visible error instead of a silent
      // no-op download. mac: `url` is the zip Squirrel.Mac fetches.
      // win32: `releases` is the DIRECTORY Squirrel.Windows resolves
      // RELEASES and the .nupkg against.
      const payloadField = isWindows ? "releases" : "url";
      if (!feed || typeof feed.version !== "string" || typeof feed[payloadField] !== "string") {
        throw new Error(`feed missing version/${payloadField}`);
      }
      if (feed.version === app.getVersion()) {
        log.info(`[update] up to date (${feed.version})`);
        foundFeed = null;
        emit("not-available");
        return;
      }
      if (updateReady && stagedVersion === feed.version) {
        // Latest is already downloaded + staged: re-surface the install
        // prompt instead of doing nothing (and instead of re-downloading).
        log.info(`[update] ${stagedVersion} already downloaded — awaiting install`);
        emit("downloaded", { version: stagedVersion, notes: stagedNotes });
        return;
      }
      // CONSENT GATE (macOS Software Update semantics): discovery never
      // downloads. Surface what was found — version, notes, publish date —
      // and wait for an explicit download() before engaging Squirrel.
      foundFeed = feed;
      log.info(`[update] found ${feed.version} (running ${app.getVersion()}) — awaiting user consent`);
      // Nudge hook: main.js shows a native notification pointing at
      // Settings > About (deduped there, once per version). Discovery-only —
      // download/install still require the explicit consent actions.
      if (typeof notifyUpdateFound === "function") {
        try { notifyUpdateFound(feed.version); } catch (err) { log.error("[update] notifyUpdateFound threw", err); }
      }
      emit("found", {
        version: feed.version,
        notes: typeof feed.notes === "string" ? feed.notes : "",
        pubDate: typeof feed.pub_date === "string" ? feed.pub_date : "",
      });
    } catch (err) {
      log.error("[update] check failed", err);
      emit("error", { message: String(err && err.message || err) });
    } finally {
      checking = false;
    }
  }

  /**
   * Explicit user consent: engage Squirrel to download (and stage) the
   * version last surfaced by safeCheck. Never called automatically.
   */
  async function startDownload() {
    if (downloading) { emit("downloading"); return; }
    if (updateReady && foundFeed && stagedVersion === foundFeed.version) {
      emit("downloaded", { version: stagedVersion, notes: stagedNotes });
      return;
    }
    if (updateReady) {
      // A previously staged bundle was superseded by a newer find: drop the
      // stale stage so Squirrel re-downloads the newest instead of installing
      // an already-old build.
      log.info(`[update] staged ${stagedVersion} superseded — re-downloading`);
      updateReady = false;
      stagedVersion = null;
      stagedNotes = "";
    }
    configureFeed();
    if (isWindows) {
      // Squirrel.Windows is handed the DIRECTORY the feed named, not the
      // pointer URL (see configureFeed). Applied here, at consent time,
      // because it is only knowable after the pointer has been fetched --
      // and a validation failure must abort BEFORE any download starts.
      let dir;
      try {
        dir = squirrelDirFromFeed(foundFeed && foundFeed.releases);
      } catch (err) {
        log.error("[update] refusing to download", err);
        emit("error", { message: String(err && err.message || err) });
        return;
      }
      autoUpdater.setFeedURL({ url: dir });
      log.info(`[update] squirrel directory: ${dir}`);
    }
    log.info("[update] user consented — engaging Squirrel download");
    downloading = true;
    emit("downloading");
    autoUpdater.checkForUpdates();
  }

  // The installer needs this process GONE before it can replace the app.
  //   macOS: ShipIt aborts the bundle swap with "App Still Running Error"
  //     (Code=-9) if ANY instance is alive during its ~25s window, and the
  //     user silently relaunches into the OLD version.
  //   Windows: Update.exe cannot overwrite files that are still open, so a
  //     lingering process yields a half-applied update instead.
  // Either way, if anything blocks the Electron quit (a renderer
  // beforeunload, a lingering child holding the process open), force-exit so
  // the installer can proceed.
  function forceExitFailsafe(reason) {
    const t = setTimeout(() => {
      log.error(`[update] process still alive ${FORCE_EXIT_AFTER_MS}ms after quitAndInstall (${reason}) — forcing exit so the installer can replace the app`);
      try { app.exit(0); } catch { process.exit(0); }
    }, FORCE_EXIT_AFTER_MS);
    if (typeof t.unref === "function") t.unref();
  }

  async function applyUpdateAndRestart() {
    if (installing) return;
    installing = true;
    // STRICT ORDER: stop the gateway and await its exit, THEN quitAndInstall.
    // A live gateway child during the swap can leave a half-replaced app --
    // and on Windows this is not merely likely but mandatory: the bundled
    // backend's files are OPEN while it runs, and Update.exe cannot overwrite
    // open files, so skipping the stop produces a broken install rather than
    // a retry.
    log.info("[update] stopping gateway before install");
    try {
      await stopGateway();
    } catch (err) {
      log.error("[update] gateway stop errored (continuing to install)", err);
    }
    app.removeListener("before-quit", deferredInstallOnQuit);
    log.info("[update] gateway down — quitAndInstall");
    autoUpdater.quitAndInstall();
    forceExitFailsafe("manual install");
  }

  // If the user chose "Later", install on the natural quit. before-quit can't
  // await async work, so preventDefault, stop the gateway, then quitAndInstall.
  function deferredInstallOnQuit(event) {
    if (quitHandled || !updateReady) return;
    quitHandled = true;
    event.preventDefault();
    (async () => {
      log.info("[update] deferred install on quit");
      try { await stopGateway(); } catch (err) { log.error("[update] stop on quit errored", err); }
      autoUpdater.quitAndInstall();
      forceExitFailsafe("deferred install on quit");
    })();
  }

  async function promptInstall(versionName, notes) {
    const { response } = await dialog.showMessageBox({
      type: "info",
      buttons: ["Restart & Update", "Later"],
      defaultId: 0,
      cancelId: 1,
      title: "Kiro Crew update ready",
      message: `Kiro Crew ${versionName || ""} is ready to install.`.trim(),
      detail:
        (notes || "").slice(0, 500) +
        "\n\nKiro Crew will stop the local gateway, install the update, and relaunch.",
    });
    if (response === 0) {
      await applyUpdateAndRestart();
    } else {
      app.once("before-quit", deferredInstallOnQuit);
      try {
        new Notification({
          title: "Update deferred",
          body: "Kiro Crew will finish updating the next time you quit.",
        }).show();
      } catch { /* notifications optional */ }
    }
  }

  autoUpdater.on("error", (err) => { downloading = false; log.error("[update] error", err); emit("error", { message: String(err && err.message || err) }); });
  autoUpdater.on("checking-for-update", () => { log.info("[update] checking…"); emit("checking"); });
  autoUpdater.on("update-not-available", () => { downloading = false; log.info("[update] up to date"); emit("not-available"); });
  autoUpdater.on("update-available", () => { downloading = true; log.info("[update] downloading…"); emit("downloading"); });
  autoUpdater.on("update-downloaded", (_e, notes, name) => {
    updateReady = true;
    downloading = false;
    stagedVersion = name || null;
    stagedNotes = notes || "";
    log.info(`[update] downloaded ${name} — ${uiDriven ? "notifying UI" : "prompting"}`);
    emit("downloaded", { version: name || app.getVersion(), notes: notes || "" });
    if (uiDriven) {
      // In-app UI owns the prompt. Still install on a natural quit if the user
      // dismisses the modal with "Later" (mirrors the native dialog's deferral).
      app.once("before-quit", deferredInstallOnQuit);
    } else {
      promptInstall(name, notes);
    }
  });

  configureFeed();
  const launchTimer = setTimeout(safeCheck, LAUNCH_CHECK_DELAY_MS);
  const pollTimer = setInterval(() => { if (!updateReady) safeCheck(); }, CHECK_INTERVAL_MS);
  // Timers must never hold the process open (Electron quit, tests).
  if (typeof launchTimer.unref === "function") launchTimer.unref();
  if (typeof pollTimer.unref === "function") pollTimer.unref();

  // Renderer-callable triggers (wired to ipcMain in main.js). Background
  // timers only ever DISCOVER (safeCheck emits "found") — downloading
  // requires the explicit download() consent call.
  return {
    check: () => safeCheck(),
    download: () => startDownload(),
    install: () => applyUpdateAndRestart(),
    getInfo,
    isReady: () => updateReady,
  };
}

module.exports = { initAutoUpdate, channelForFlavor, channelForVersion, resolveChannel, buildFeedUrl, fetchFeedHttps };
