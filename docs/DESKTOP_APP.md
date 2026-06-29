# KiroClaw Desktop App

The desktop app is an [Electron](https://www.electronjs.org/) shell that wraps
the KiroClaw web dashboard and embeds a **frozen Python backend**. Because the
backend is frozen with PyInstaller and shipped inside the app, end users need
**no** Python, pip, npm, or node — they just double-click the app and the
dashboard opens.

The Electron sources live in [`website/electron/`](../website/electron/); the
build is driven by [`packaging/build-desktop.sh`](../packaging/build-desktop.sh)
and the PyInstaller spec
[`packaging/kiroclaw-backend.spec`](../packaging/kiroclaw-backend.spec).

## What `make desktop` produces

```bash
make desktop
```

Output lands in **`website/electron/dist/`**:

| Platform | Artifact |
|----------|----------|
| macOS | `KiroClaw-*.dmg` |
| Linux | `KiroClaw-*.AppImage` |

The artifact for the host OS is built (DMG on macOS, AppImage on Linux). The
electron-builder configuration lives in
[`website/electron/package.json`](../website/electron/package.json):

- **appId:** `dev.kiroclaw.desktop`
- **productName:** `KiroClaw`
- mac target: `dmg` (category `public.app-category.developer-tools`)
- linux target: `AppImage` (category `Development`)

### Builds are host-architecture-only — one build per target arch

> **Important:** `make desktop` produces an installer for the **host OS *and*
> host CPU architecture only.** It is not a universal/fat binary.

The PyInstaller spec uses `target_arch=None` (honors the host arch) and the
electron-builder config sets no `arch` key (defaults to the host arch). The
frozen backend's architecture is therefore **coupled** to the installer's — you
cannot mix (e.g. an arm64 DMG carrying an x86_64 backend). To cover all four
supported targets you must run the build on a machine of each architecture:

| Target | Build host | Produces |
|--------|-----------|----------|
| macOS arm64 (Apple Silicon) | Apple Silicon Mac | arm64 `.dmg` |
| macOS x86_64 (Intel) | Intel Mac, or an Apple-Silicon Mac via Rosetta (see [Building BOTH macOS DMGs](#building-both-macos-dmgs-from-one-apple-silicon-machine-rosetta)) | x86_64 `.dmg` |
| Linux x86_64 | x86_64 Linux | x86_64 `.AppImage` |
| Linux aarch64 (Graviton/ARM) | aarch64 Linux | aarch64 `.AppImage` |

A maintainer on an Apple-Silicon Mac who runs `make desktop` ships an
**arm64-only** DMG; Intel-Mac users cannot run it. For a public release, build
each artifact on its own runner (e.g. a CI matrix of `macos-14` (arm64),
`macos-13` (x86_64), `ubuntu-latest` (x86_64), and an arm64 Linux runner).
There is intentionally no `universal2` macOS target — it would require
universal2 wheels for every native dependency (numpy, aiohttp, lxml, PyYAML),
which not all publish.

### Building BOTH macOS DMGs from one Apple-Silicon machine (Rosetta)

You can produce both the arm64 and the x86_64 DMG on a single Apple-Silicon Mac
without an Intel machine, by running the x86_64 toolchain under **Rosetta 2**.
PyInstaller does not cross-compile, so the trick is to freeze the backend with
an *x86_64* Python (run via `arch -x86_64`) and then ask electron-builder for
the x64 target (it downloads the x86_64 Electron itself).

Prerequisites: Rosetta 2 (`softwareupdate --install-rosetta --agree-to-license`)
and an x86_64-capable `python3` (the system `/usr/bin/python3` is universal2 and
works; verify with `arch -x86_64 /usr/bin/python3 -c 'import platform;
print(platform.machine())'` → `x86_64`).

```bash
# 0. Build + stage the frontend ONCE (arch-independent); both DMGs reuse it.
cd website && npm ci && npm run build && cd ..
rm -rf src/kiro_claw/static/dist && cp -R website/dist src/kiro_claw/static/dist

# 1. arm64 (native): freeze + stage + package. SKIP_FRONTEND reuses step 0.
SKIP_FRONTEND=1 PYTHON="$PWD/.venv/bin/python" bash packaging/build-desktop.sh
#    → website/electron/dist/KiroClaw-<version>-arm64.dmg

# 2. x86_64 (under Rosetta): build an x86_64 venv, freeze with it, then have
#    electron-builder package the x64 target with that backend staged.
arch -x86_64 /usr/bin/python3 -m venv .venv-x86
arch -x86_64 .venv-x86/bin/python -m pip install -U pip setuptools wheel
arch -x86_64 .venv-x86/bin/python -m pip install -e . "pyinstaller>=6,<7"
PYTHONPATH="$PWD/src" arch -x86_64 .venv-x86/bin/python -m PyInstaller \
  packaging/kiroclaw-backend.spec --noconfirm \
  --distpath "$PWD/build/pyinstaller-x86/dist" \
  --workpath "$PWD/build/pyinstaller-x86/build"
rm -rf website/electron/backend-dist/kiroclaw-backend
cp -R build/pyinstaller-x86/dist/kiroclaw-backend website/electron/backend-dist/kiroclaw-backend
( cd website/electron && npx electron-builder --mac --x64 )
#    → website/electron/dist/KiroClaw-<version>.dmg   (no -arch suffix == x64)

# 3. Restore the arm64 backend into backend-dist so the working tree is back to
#    its native state for normal `make desktop` runs.
rm -rf website/electron/backend-dist/kiroclaw-backend
cp -R build/pyinstaller/dist/kiroclaw-backend website/electron/backend-dist/kiroclaw-backend
```

electron-builder names the host-arch (arm64) DMG `KiroClaw-<v>-arm64.dmg` and the
x64 DMG `KiroClaw-<v>.dmg` (no suffix), so the two coexist in
`website/electron/dist/`. Verify each actually carries the matching backend:

```bash
# The embedded backend's arch MUST match the DMG's arch (an arm64 DMG carrying
# an x86_64 backend would crash on launch). Mount and check:
hdiutil attach -nobrowse -readonly website/electron/dist/KiroClaw-<v>-arm64.dmg
file "/Volumes/KiroClaw <v>-arm64/KiroClaw.app/Contents/Resources/backend-dist/kiroclaw-backend/kiroclaw-backend"
#   → …executable arm64
hdiutil detach "/Volumes/KiroClaw <v>-arm64"
```

> CI is still the cleaner path for releases (`macos-14` for arm64, `macos-13`
> for x86_64) — the Rosetta route is for producing both locally when you don't
> have an Intel runner.

### Refreshing / cleaning the DMGs

The `dist/` directory is **not** cleaned between builds, so old artifacts pile up
(e.g. a `KiroClaw-1.0.0.dmg` from before a version bump, or a stale `mac/`
app-staging dir). After a version change or a re-build, remove the stale ones so
only the current set remains:

```bash
cd website/electron/dist
rm -f KiroClaw-<old-version>*.dmg            # stale DMGs from a prior version
rm -rf mac mac-arm64                          # app-staging dirs (regenerated each build)
rm -f builder-debug.yml
```

The desktop app's version comes from `website/electron/package.json` (`version`)
— **keep it in sync with the backend `version` in `pyproject.toml`**. When you
bump one, bump the other and the root `version` fields in
`website/electron/package-lock.json` (the top-level `version` and
`packages[""].version`, NOT the dependency entries that coincidentally share a
version), or `npm ci` will complain about a lock mismatch.

> **npm registry pin (required):** both `website/.npmrc` *and*
> `website/electron/.npmrc` pin `registry=https://registry.npmjs.org/`. The
> electron pin is load-bearing — without it `npm ci` in `website/electron/`
> inherits whatever registry the machine's global `~/.npmrc` sets and can fail
> with an auth error on a non-public registry. Any new npm subproject needs its
> own public-registry `.npmrc`.

## Build pipeline

`make desktop` runs `bash packaging/build-desktop.sh`, which executes the
pipeline end-to-end:

```
1. Build the React dashboard (npm)      → website/dist
2. Stage it into the Python package      → src/kiro_claw/static/dist
3. Freeze the backend with PyInstaller    → build/pyinstaller/dist/kiroclaw-backend/
4. Drop the frozen backend into Electron  → website/electron/backend-dist/kiroclaw-backend/
5. Package with electron-builder          → website/electron/dist/ (DMG / AppImage)
```

Step by step:

1. **Frontend** — in `website/`, runs `npm ci` (or `npm install`) + `npm run
   build`, then copies `website/dist` into `src/kiro_claw/static/dist`. The
   script aborts if `src/kiro_claw/static/dist/index.html` is missing, so the
   SPA is always bundled.
2. **PyInstaller backend** — installs `pyinstaller` if absent, then freezes the
   backend using `packaging/kiroclaw-backend.spec`. The build is made hermetic by
   pointing `PYTHONPATH` at this repo's `src/` only (the spec's `pathex=[SRC]`
   reinforces this) so a polluted ambient `PYTHONPATH` can't leak modules into
   the bundle. The frozen backend is smoke-tested with
   `kiroclaw-backend --version`.
3. **Stage into Electron** — copies the frozen
   `build/pyinstaller/dist/kiroclaw-backend/` directory into
   `website/electron/backend-dist/kiroclaw-backend/`.
4. **Package** — in `website/electron/`, runs `npm ci` (or `npm install`) +
   `npm run dist` (electron-builder), producing the installer(s) in
   `website/electron/dist/`. This `npm ci` is pinned to the public registry by
   `website/electron/.npmrc`; see the registry-pin note under
   [Refreshing / cleaning the DMGs](#refreshing--cleaning-the-dmgs).

### Build-only-the-backend escape hatches

The script honors two environment flags (also used by `make backend-bin`, which
sets both):

| Flag | Effect |
|------|--------|
| `SKIP_FRONTEND=1` | Reuse an already-staged `src/kiro_claw/static/dist` |
| `SKIP_ELECTRON=1` | Stop after the frozen backend binary (no electron-builder) |

So `make backend-bin` (`SKIP_FRONTEND=1 SKIP_ELECTRON=1`) yields just the frozen
backend at `website/electron/backend-dist/kiroclaw-backend/` without packaging
the desktop app.

## The frozen backend (PyInstaller)

[`packaging/kiroclaw-backend.spec`](../packaging/kiroclaw-backend.spec) produces
a **one-folder** bundle (not one-file) named `kiroclaw-backend` — chosen for
faster startup and a stable on-disk layout for bundled data files. Key details:

- **Entry point** is `src/kiro_claw/__main__.py` (not `cli.py`): `__main__`
  runs SSL-cert setup before importing `kiro_claw.cli`, which caches its SSL
  context at import time.
- **Data files** mirror the `setup.cfg` `package_data` (configs, prompts,
  personas, app manifests, eval scenarios, in-app docs, scripts, skills) **plus**
  the pre-built dashboard at `static/dist`, which `package_data` intentionally
  omits because Vite emits content-hashed filenames.
- **Repo-root `agents/` and `skills/`** are bundled too (loaded via
  `KIROCLAW_PROJECT_DIR`) so the standalone app has its agent config without a
  source checkout.
- **Lean by default** — `boto3`, `botocore`, `amazon_transcribe`, `matplotlib`,
  and `tkinter` are excluded since the `[aws]`/`[voice]` extras are optional and
  lazy-imported. Remove them from `excludes` if shipping an AWS/voice build.
- **`console=True`** — the headless gateway logs to stdout/stderr so the Electron
  parent can capture output.

## How the app finds and launches the backend

When the app starts, [`main.js`](../website/electron/main.js) first checks
whether a gateway is already running; if not, it locates the backend binary via
[`find-bin.js`](../website/electron/find-bin.js) and spawns it as
`kiroclaw-backend gateway --no-open`, then polls `/api/status` (up to 2 minutes)
and loads the dashboard once it is healthy.

### `find-bin.js` — locating the binary

`findKiroclawBin()` checks well-known paths in order and returns the first
executable it finds, falling back to bare `kiroclaw` on `PATH`:

1. `<resourcesPath>/backend-dist/kiroclaw-backend/kiroclaw-backend` — the bundled
   PyInstaller binary inside the packaged `.app` (electron-builder ships
   `backend-dist/kiroclaw-backend` as `extraResources`).
2. `<__dirname>/backend-dist/kiroclaw-backend/kiroclaw-backend` — the same binary
   when running unpackaged from `website/electron/` in development.
3. `<__dirname>/../bin/kiroclaw`
4. Well-known install paths under `$HOME` (e.g. `~/.local/bin/kiroclaw`,
   `~/.kiroclaw-app/.venv/bin/kiroclaw`).
5. Bare `"kiroclaw"` (resolved via `PATH`).

The function is pure — `fs`, `os`, `path`, `process.resourcesPath`, and
`__dirname` are injected — so it is unit-testable without mocking globals.

### `main.js` — spawning the gateway

- Ensures `KIROCLAW_HOME` (default `~/.kiroclaw`, overridable via the
  `KIROCLAW_HOME` env var) exists, then spawns the backend with
  `["gateway", "--no-open"]`.
- Honors the **`KIROCLAW_PORT`** env var for the dashboard port (default `5476`,
  validated to `1–65535`). `BACKEND_URL` / health checks target that port.
- Sets `KIROCLAW_PROJECT_DIR` to the Electron app's parent directory so the
  bundled `agents/` and `skills/` are discovered.
- On window close the app hides to the tray; quitting sends `SIGTERM` to the
  gateway process.

## Code signing & notarization (macOS)

An unsigned `.app`/DMG is quarantined by Gatekeeper and shows **"KiroClaw is
damaged and can't be opened"** when downloaded on another Mac. To distribute a
DMG that opens cleanly you must sign it with a **Developer ID Application**
certificate and **notarize** it with Apple. (Local builds without credentials
still work — they produce an ad-hoc–signed DMG you can open on the build machine
after right-click → Open or `xattr -dr com.apple.quarantine KiroClaw.app`.)

The build is already wired for this — `website/electron/package.json` enables
`hardenedRuntime` with `build/entitlements.mac.plist`, and the
`scripts/notarize.js` afterSign hook notarizes when credentials are present and
silently skips when they aren't. You only supply the secrets at build time via
env vars (nothing is committed):

```bash
# 1. Signing identity — a Developer ID Application cert exported as .p12
#    (Xcode → Settings → Accounts, or developer.apple.com → Certificates).
export CSC_LINK=/abs/path/DeveloperIDApplication.p12   # or its base64
export CSC_KEY_PASSWORD='<p12 export password>'

# 2. Notarization credentials — EITHER an App Store Connect API key …
export APPLE_API_KEY=/abs/path/AuthKey_XXXXXXXXXX.p8
export APPLE_API_KEY_ID=XXXXXXXXXX
export APPLE_API_ISSUER=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#    … OR an Apple ID + app-specific password (appleid.apple.com → Sign-In
#    & Security → App-Specific Passwords):
export APPLE_ID='you@example.com'
export APPLE_APP_SPECIFIC_PASSWORD='abcd-efgh-ijkl-mnop'
export APPLE_TEAM_ID=XXXXXXXXXX

# 3. Build — electron-builder signs, the hook notarizes + staples.
make desktop
```

Verify the result: `spctl -a -vv "KiroClaw.app"` should report
`source=Notarized Developer ID` and `codesign -dv` should show your Team ID
(not `Signature=adhoc`).

Requires a paid Apple Developer account ($99/yr) for the Developer ID cert and
notary access. Without one, distribute via Homebrew cask or instruct users to
clear the quarantine flag.

## Remote tunnel mode

The desktop app can also connect to a gateway running on a **remote** host (e.g.
an always-on server) over an SSH tunnel, fetching a fresh token via
`ssh <host> kiroclaw token` on each launch instead of starting a local backend.
See [`website/electron/README.md`](../website/electron/README.md) and
[REMOTE_DESKTOP_SETUP.md](REMOTE_DESKTOP_SETUP.md) for setup.

## See also

- [INSTALL.md](INSTALL.md) — all three build/run methods and the Makefile targets
- [README](../README.md) — project overview and Quick Start
