# App Platform Trust Model

KiroClaw's app platform loads app Python directly into the gateway process
(`apps/module_loader.py` → `importlib` → `exec_module`). This page documents the
resulting trust boundary and how KiroClaw makes it explicit.

## What an app can do

When you **enable** an app, its backend hooks, route handlers, and lifecycle
scripts run **in-process with full gateway privileges**:

- Arbitrary `import`, filesystem, and network access
- Access to anything in the gateway process's memory (including resolved credentials)
- Manifest `setup` lifecycle scripts run via `/bin/bash -c` (OS-sandbox-wrapped, but
  the script body comes from the app's `app.json`)

The app **permission system** (`permissions.py`, `context.py`, `app.json`
`permissions.mcpTools`) gates only the **SDK tool surface** handed to the app
context. It does **not** restrict imports, filesystem, network, or subprocess use
by the loaded module. There is currently **no process-level sandbox** around app
code itself.

> **Installing/enabling an app is therefore equivalent to running that code with
> the same privileges as KiroClaw itself.** Only enable apps you trust.

## How KiroClaw makes the boundary explicit

- **Builtin vs third-party split** — apps shipped inside the package
  (`apps/builtins/`) are trusted like core. Anything loaded from outside that
  directory is treated as third-party.
- **One-time SECURITY warning** — the first time a third-party app's Python is
  executed, `module_loader` logs a loud warning naming the app and the privilege it
  receives.
- **SEL audit** — every module load is recorded in the Security Event Log with its
  trust class (`builtin` / `third_party`), so app-code execution is auditable.

## Future work

True isolation (running app code in a separate sandboxed subprocess rather than
in-process) is intentionally **out of scope** for now — the open-source app
registry ships empty and all installs are operator-consented. Process isolation
is tracked as a separate design to be revisited if/when a public app store lands.
(Corresponds to CSE finding SEC-012.)
