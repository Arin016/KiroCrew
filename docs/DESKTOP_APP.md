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
   `website/electron/dist/`.

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
- Honors the **`KIROCLAW_PORT`** env var for the dashboard port (default `7777`,
  validated to `1–65535`). `BACKEND_URL` / health checks target that port.
- Sets `KIROCLAW_PROJECT_DIR` to the Electron app's parent directory so the
  bundled `agents/` and `skills/` are discovered.
- On window close the app hides to the tray; quitting sends `SIGTERM` to the
  gateway process.

## Remote tunnel mode

The desktop app can also connect to a gateway running on a **remote** host (e.g.
an always-on server) over an SSH tunnel, fetching a fresh token via
`ssh <host> kiroclaw token` on each launch instead of starting a local backend.
See [`website/electron/README.md`](../website/electron/README.md) and
[REMOTE_DESKTOP_SETUP.md](REMOTE_DESKTOP_SETUP.md) for setup.

## See also

- [INSTALL.md](INSTALL.md) — all three build/run methods and the Makefile targets
- [README](../README.md) — project overview and Quick Start
