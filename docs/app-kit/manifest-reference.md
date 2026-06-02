# App Manifest Reference

The app manifest (`app.json`) declares your app's identity, resources, and requirements.

## Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier, kebab-case (e.g. `"oncall-watchtower"`) |
| `version` | string | Semver version (e.g. `"1.0.0"`) |
| `displayName` | string | Human-readable name shown in App Store |
| `description` | string | Short description of what the app does |

## Recommended Fields

| Field | Type | Description |
|-------|------|-------------|
| `author` | string | Author name or team |
| `license` | string | License identifier |
| `minKiroClawVersion` | string | Minimum Gateway version required |
| `tags` | string[] | Discovery tags (e.g. `["oncall", "monitoring"]`) |
| `jobFamilies` | string[] | Job families this app is relevant to |

## Resources

| Field | Type | Description |
|-------|------|-------------|
| `agents` | string[] | Paths to agent JSON files (relative to app root) |
| `skills` | string[] | Paths to skill directories |
| `sops` | string[] | Paths to SOP (Standard Operating Procedure) files |
| `mcpServers` | object | MCP server definitions (same format as `mcp.json`) |

## Scheduling

### `crons` — Cron Job Definitions

```json
{
  "crons": [
    {
      "name": "ticket-refresh",
      "every": 300,
      "message": "Check for new high-severity tickets"
    },
    {
      "name": "daily-digest",
      "cron_expr": "0 9 * * 1-5",
      "message": "Generate daily digest",
      "agent": "digest-agent"
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Job identifier |
| `every` | number | Interval in seconds (mutually exclusive with `cron_expr`) |
| `cron_expr` | string | Cron expression (mutually exclusive with `every`) |
| `message` | string | Prompt sent to the agent on each run |
| `agent` | string | Agent to run (optional, uses default if omitted) |

## Frontend UI

### `ui` — Dashboard Integration

```json
{
  "ui": {
    "entry": "dist/index.mjs",
    "pages": [
      {
        "route": "/apps/my-app",
        "label": "My App",
        "icon": "Shield",
        "entryPoint": "dist/page.mjs",
        "mountFunction": "mount"
      }
    ],
    "sidebar": {
      "section": "Apps",
      "order": 10
    }
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ui.entry` | string | | Path to ESM bundle (relative to app root) |
| `ui.pages[].route` | string | | URL path for the page |
| `ui.pages[].label` | string | | Sidebar label |
| `ui.pages[].icon` | string | | Lucide icon name (e.g. `"Shield"`, `"Package"`) |
| `ui.pages[].iconUrl` | string | | Custom icon image path (relative to ui/) |
| `ui.pages[].entryPoint` | string | | Per-page ESM bundle path (overrides `ui.entry`) |
| `ui.pages[].mountFunction` | string | `"mount"` | Exported function name in the ESM bundle |
| `ui.sidebar.section` | string | `"Apps"` | Sidebar section name |
| `ui.sidebar.order` | number | `10` | Sort order within section |

## Backend

### `backend` — App Backend Process

```json
{
  "backend": {
    "entryPoint": "backend/server.py",
    "port": "auto",
    "healthCheck": "/health",
    "routes": "/api/apps/oncall-watchtower"
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `backend.entryPoint` | string | | Script to run (relative to app root) |
| `backend.port` | string | `"auto"` | Port number or `"auto"` for auto-assignment |
| `backend.healthCheck` | string | `"/health"` | Health check endpoint path |
| `backend.routes` | string | | Base route path for the backend |

App backends are accessible through the Gateway's reverse proxy at
`/apps/{name}/api/{path}`, which avoids CORS issues for dashboard UI pages.

## Permissions

### `permissions` — Declared Capabilities

```json
{
  "permissions": {
    "api": ["/api/crons", "/api/status", "/api/agents"],
    "events": ["notification", "slots"],
    "mcpTools": ["cron_add", "cron_list"],
    "storage": true,
    "cron": true,
    "memory": "app-scoped",
    "network": false
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `permissions.api` | string[] | Allowed API path prefixes |
| `permissions.events` | string[] | Allowed WebSocket event types |
| `permissions.mcpTools` | string[] | Allowed MCP tool names |
| `permissions.storage` | boolean | Can use app-scoped storage |
| `permissions.cron` | boolean | Can create cron jobs |
| `permissions.memory` | string | Memory access: `""` (none), `"app-scoped"`, or `"shared"` |
| `permissions.network` | boolean | Can make external network requests |

## Setup Hooks

### `setup` — Lifecycle Scripts

```json
{
  "setup": {
    "onInstall": "cd ui && npm install && npm run build",
    "onUninstall": "echo cleanup done",
    "onUpdate": "cd ui && npm install && npm run build",
    "onEnable": "echo enabled",
    "onDisable": "echo disabled",
    "configSchema": {}
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `setup.onInstall` | string | `""` | Shell command run after install |
| `setup.onUninstall` | string | `""` | Shell command run before uninstall |
| `setup.onUpdate` | string | `""` | Shell command run after update |
| `setup.onEnable` | string | `""` | Shell command run when app is enabled |
| `setup.onDisable` | string | `""` | Shell command run when app is disabled |
| `setup.onEnableTimeout` | number | `30` | Timeout in seconds for `onEnable` script |
| `setup.onDisableTimeout` | number | `30` | Timeout in seconds for `onDisable` script |
| `setup.configSchema` | object | `{}` | JSON Schema for app configuration |

If `onEnable` fails (non-zero exit), the enable is rolled back — the app
stays disabled and any registered resources are deregistered. `onDisable`
failures are logged as warnings but do not block the disable operation.

Install scripts run in a sandboxed environment with a minimal set of
environment variables (PATH, HOME, SSH_AUTH_SOCK, etc.) to prevent
leaking secrets from the gateway process.

## Dependencies

### `dependencies` — External Dependency Declarations

Declare external dependencies your app requires. The gateway tracks these
in a reference-counted ledger so shared dependencies are not removed when
only one app is uninstalled.

```json
{
  "dependencies": {
    "managedBy": "gateway",
    "aim": {
      "mcp": [
        { "id": "some-mcp-server", "source": "registry" }
      ],
      "skills": [
        { "id": "some-skill", "source": "registry" }
      ],
      "agents": [
        { "id": "some-agent", "source": "registry" }
      ]
    },
    "commands": ["jq", "node", "python3"]
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `dependencies.managedBy` | string | `"gateway"` | Who manages dependency lifecycle: `"gateway"` or `"app"` |
| `dependencies.aim` | object | `{}` | Capability-package dependencies (MCP servers, skills, agents) installed via the optional `aim` package manager. If the `aim` CLI is not on PATH, these are skipped gracefully. |
| `dependencies.aim.mcp` | object[] | `[]` | Required MCP server dependencies |
| `dependencies.aim.skills` | object[] | `[]` | Required skill dependencies |
| `dependencies.aim.agents` | object[] | `[]` | Required agent dependencies |
| `dependencies.commands` | string[] | `[]` | System commands that must be on PATH (checked via `which`) |

## Lifecycle & Resource Management

### `lifecycle` and `resources`

Control how KiroClaw manages the app:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `lifecycle` | string | `"gateway"` | `"gateway"` (managed), `"app"` (self-managed), or `"locked"` (cannot uninstall) |
| `resources` | string | `"gateway"` | `"gateway"` (KiroClaw registers agents/skills/MCP) or `"app"` (app handles its own) |

## Platform

### `platform` — Compatibility & Install Mode

```json
{
  "platform": {
    "os": ["macos", "linux"],
    "arch": [],
    "installMode": "server",
    "clientInstall": {
      "shell": "curl -fsSL https://example.com/install.sh | bash",
      "postInstall": "open ~/Applications/MyApp.app"
    }
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `platform.os` | string[] | `["macos", "linux"]` | Supported platforms |
| `platform.arch` | string[] | `[]` (any) | Supported architectures |
| `platform.installMode` | string | `"server"` | `"server"` or `"client"` |
| `platform.clientInstall.shell` | string | | One-liner for local install |
| `platform.clientInstall.postInstall` | string | | Command to run after install |

When `installMode` is `"client"`, the App Store shows copy-paste terminal
instructions instead of running the install on the server. This is used for
apps that must run on the user's local machine (e.g. Electron desktop apps
when KiroClaw runs on a remote host).

## Validation Rules

- `name` must match `/^[a-z0-9]+(?:-[a-z0-9]+)*$/` (kebab-case)
- `version` must match semver (`X.Y.Z`)
- Paths in `agents`, `skills`, `sops`, `ui.entry`, `ui.pages[].entryPoint` must not contain `..` (path traversal)
- All required fields must be non-empty strings
- Each cron entry must specify either `every` or `cron_expr`
- Each UI page must have `route` and `label`

## Full Example

```json
{
  "name": "oncall-watchtower",
  "version": "1.0.0",
  "displayName": "Oncall Watchtower",
  "description": "Monitor tickets, pipelines, and alarms for your on-call rotation",
  "author": "kiroclaw",
  "tags": ["oncall", "monitoring"],
  "agents": ["agents/ticket-analyst.json"],
  "skills": ["skills/oncall-runbook"],
  "crons": [
    {
      "name": "ticket-refresh",
      "every": 300,
      "message": "Check for new high-severity tickets"
    }
  ],
  "ui": {
    "entry": "dist/index.mjs",
    "pages": [
      {
        "route": "/apps/oncall-watchtower",
        "label": "Oncall",
        "icon": "Shield"
      }
    ]
  },
  "permissions": {
    "api": ["/api/crons", "/api/status"],
    "events": ["notification"]
  },
  "platform": {
    "os": ["macos", "linux"]
  }
}
```

## Forward Compatibility

Unknown fields in `app.json` are preserved during parsing and round-tripped
through `to_dict()` / `to_json()`. This allows newer manifest features to
coexist with older KiroClaw versions without breaking validation.
