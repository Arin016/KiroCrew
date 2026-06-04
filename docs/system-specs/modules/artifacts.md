# Artifacts Module

Last Updated: 2026-05-21

## Overview

Artifacts give chat-rendered LLM-generated UI a persistent identity, version
history, and a stable handle the agent can iterate on across sessions.

A typical flow:

1. Agent emits an `<mcwidget>` in chat ("here's your CR queue")
2. Agent (or user) calls `artifact_save` — the widget is persisted under
   `~/.kiroclaw/artifacts/<slug>/current.html`
3. Days later, in a fresh session, the user says "iterate on the cr-queue
   artifact and add an age column"
4. Agent calls `artifact_get("cr-queue")` to read the current HTML, modifies
   it, then `artifact_update("cr-queue", content=…)` to publish a new version
5. The previous version is preserved under `versions/v1.html` for rollback

The dashboard provides a `/artifacts` library page for browse/search and a
`/artifacts/<slug>` standalone view with a version dropdown.

## Storage Layout

```
~/.kiroclaw/artifacts/
└── <slug>/
    ├── meta.json        canonical metadata (no content)
    ├── current.html     latest content
    └── versions/
        ├── v1.html
        ├── v2.html
        └── …
```

`meta.json` schema:

| Field | Type | Notes |
|---|---|---|
| `slug` | string | URL-safe handle, derived from `name` if not given |
| `name` | string | Human-readable display name |
| `kind` | enum | `widget` (default), `html`, `markdown`, `svg`, `json`, `text` |
| `source` | enum | `chat` (default), `cron`, `subagent`, `manual`, `import` |
| `description` | string | Optional, ≤ 2,000 chars |
| `tags` | string[] | ≤ 16 tags, alphanumeric / `_`, `:`, `.`, `-` |
| `version` | int | Latest version number; bumps on every content change |
| `created_at` / `updated_at` | string | ISO 8601 UTC microseconds |

## Public API

### Python (`kiro_claw.artifacts`)

```python
from kiro_claw.artifacts import ArtifactStore, get_default_store

store = get_default_store()
art = store.create(name="CR Queue", content="<table>…</table>", tags=["ops"])
art = store.get(art.slug)
art = store.update(art.slug, content="<table>… age column …</table>")
versions = store.list_versions(art.slug)
items = store.list(tag="ops")
store.delete(art.slug)
```

The store is thread-safe. A module-level singleton is available via
`get_default_store()`; pass an explicit `root` to `ArtifactStore(root=...)`
for isolated test instances.

### MCP tools (`@kiroclaw-core/*`)

| Tool | Purpose |
|---|---|
| `artifact_save` | Create a new artifact, returns slug |
| `artifact_get` | Read content + metadata (optionally a specific version) |
| `artifact_update` | Modify content/name/description/tags; bumps version on content change |
| `artifact_list` | List artifacts (filter by `tag`, `kind`, name `q`) |
| `artifact_versions` | List version numbers for a slug |
| `artifact_delete` | Permanent delete (artifact + all versions) |

Schemas live in `validation.py` (`ARTIFACT_*_SCHEMA`) and are registered in
`MCP_CORE_SCHEMAS`. The MCP tool layer always proxies through the HTTP API so
SEL audit, restricted-session enforcement, and any future authorization
middleware live in one place.

### CLI (`kiroclaw artifact`)

```
kiroclaw artifact list [--tag T] [--kind K] [--q SUBSTR]
kiroclaw artifact show <slug> [--version N] [--meta]
kiroclaw artifact save --name N [--kind K] [--content C | --content-file F] [--tags A,B] [--description D]
kiroclaw artifact update <slug> [--content C | --content-file F] [--name N] [--description D] [--tags A,B]
kiroclaw artifact versions <slug>
kiroclaw artifact delete <slug>
```

The CLI proxies through the gateway HTTP API (matches `kiroclaw learn`).

### HTTP

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/artifacts` | `?tag&kind&q` filters; returns `{artifacts: […]}` |
| `POST` | `/api/artifacts` | JSON body — creates, returns full artifact + content |
| `GET` | `/api/artifacts/{slug}` | Returns full artifact + content |
| `PATCH` | `/api/artifacts/{slug}` | Partial update; `content` bumps version |
| `DELETE` | `/api/artifacts/{slug}` | Permanent delete |
| `GET` | `/api/artifacts/{slug}/versions` | `{slug, versions: [int]}` |
| `GET` | `/api/artifacts/{slug}/versions/{n}` | Specific version content |

POST/PATCH/DELETE require an unrestricted session. The body is capped at
2 MiB; the store enforces a per-content cap of 1 MiB.

### Dashboard pages

- `/artifacts` — list page (name / kind / tags / updated_at), tag filter,
  name substring search, click-through to detail
- `/artifacts/<slug>` — full-screen render of the current artifact in a
  sandboxed iframe (same security model as inline `<mcwidget>`), with a
  version dropdown

A small "Save as artifact" button is overlaid on every rendered `<mcwidget>`
in chat. Clicking prompts for a name and POSTs to `/api/artifacts`.

## Validation & Limits

| Field | Limit |
|---|---|
| `slug` | regex `^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$`, ≤ 80 chars |
| `name` | ≤ 200 chars, non-empty |
| `description` | ≤ 2,000 chars |
| `tags` | ≤ 16 tags; each ≤ 64 chars |
| `content` | ≤ 1 MiB |
| `kind` | one of `widget` / `html` / `markdown` / `svg` / `json` / `text` |
| `source` | one of `chat` / `cron` / `subagent` / `manual` / `import` |
| `MAX_VERSIONS` | 50 (oldest pruned beyond cap) |

## Security

- **Path traversal** — slugs are regex-validated; the store resolves every
  path and refuses any that escape the artifact root.
- **Sensitive paths** — every read and write goes through
  `security.is_sensitive_path()`; the store refuses to instantiate at any
  sensitive root.
- **Restricted sessions** — POST/PATCH/DELETE are denied when the dashboard
  classifies the session as restricted (`_is_restricted_session`).
- **SEL audit** — every mutation emits a `log_tool_invocation` event from the
  HTTP layer (`api/dashboard/handlers/artifacts.py`). Reads are not audited.
- **Atomic writes** — `_write_text()` writes to a `.tmp` sibling and renames,
  so a crash mid-write cannot corrupt `current.html` or `meta.json`.
- **Tolerant load** — `_read_meta_file()` ignores unknown keys and supplies
  defaults for missing keys, so future schema additions don't break existing
  files.
- **Frontend rendering** — artifact bodies are rendered in the same sandboxed
  iframe that powers `<mcwidget>`. No `dangerouslySetInnerHTML` without
  DOMPurify; no inline event handlers.

## Versioning

Each `create()` writes the initial content to `current.html` and snapshots
it as `versions/v1.html`. Each subsequent `update(slug, content=…)` that
changes the content bumps the version number, writes the new content as
both `current.html` and `versions/v{N}.html`. Older versions remain in
`versions/` untouched until the prune cap is reached, so any prior version
can be re-read via `get(slug, version=N)` or rolled back into `current.html`
via a follow-up `update()`.

`list_versions(slug)` returns the sorted set of stored version numbers.
`get(slug, version=N)` reads a specific version. After pruning, lower-numbered
versions may be unavailable; callers must handle `ArtifactNotFoundError` for
out-of-range versions.

## Roadmap

In scope for the foundation:

- ✅ data layer + CLI + MCP tools + HTTP + library page + standalone page
- ✅ "Save as artifact" affordance on rendered widgets
- ✅ system prompt context note documenting the iterate flow

Out of scope (separate tasks):

- **Whiteboard layout** — saved arrangements of (artifact_id, x, y, w, h) —
  parent task [Mesh-1437](https://taskei.amazon.dev/tasks/Mesh-1437).
- **Live refresh bindings** — cron / Python script / MCP-tool source types
  that auto-rewrite `current.html` on a schedule — task
  [Mesh-1565](https://taskei.amazon.dev/tasks/Mesh-1565). The hook will be a
  new `meta.json.refresh_binding` field consumed by a refresh service.
- **Right-panel inline render** — clicking an `<a>` to an artifact in chat
  opens the artifact in a side panel rather than the standalone page —
  related to [Mesh-1534](https://taskei.amazon.dev/tasks/Mesh-1534).
- **Cross-user sharing**, **embeddings/full-text search**, **install from
  URL/community widget store** — future expansions.
