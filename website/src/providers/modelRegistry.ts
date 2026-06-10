// Frontend reader for the canonical model registry. Imports the SAME data file
// the Python backend uses (model_registry.json) so canonical keys, display
// names, and context windows agree without an API round-trip. The wire value
// sent to the backend is the canonical key; the backend translates it to a
// provider id at the config.loader factory boundary.
import registryRaw from '../model_registry.json'

type Entry = {
  display: string
  description?: string
  window: number
  default?: boolean
  supports_effort?: boolean
  aliases?: string[]
  providers: Record<string, string>
}

const REGISTRY: Record<string, Entry> = Object.fromEntries(
  Object.entries(registryRaw as Record<string, unknown>).filter(([k]) => !k.startsWith('_')),
) as Record<string, Entry>

const PROVIDER = 'claude_code'

// Precomputed: canonical key + provider id + every alias -> window. Built once
// (the registry is immutable) so contextWindow() is O(1), not a linear scan per
// dropdown row.
const WINDOW_INDEX: Map<string, number> = (() => {
  const m = new Map<string, number>()
  for (const [key, e] of Object.entries(REGISTRY)) {
    if (!(PROVIDER in e.providers)) continue
    m.set(key, e.window)
    const pid = e.providers[PROVIDER]
    if (pid) m.set(pid, e.window)
    for (const alias of e.aliases ?? []) {
      if (!m.has(alias)) m.set(alias, e.window)
    }
  }
  return m
})()

const DEFAULT_KEY: string =
  Object.entries(REGISTRY).find(([, e]) => e.default && PROVIDER in e.providers)?.[0] ?? 'opus-4.8-1m'

/** Canonical key of the provider's default model (falls back to opus-4.8-1m). */
export function defaultModel(): string {
  return DEFAULT_KEY
}

/** Context window for a canonical key, alias, or provider id (200k default). */
export function contextWindow(canonicalOrId: string): number {
  const hit = WINDOW_INDEX.get(canonicalOrId)
  if (hit !== undefined) return hit
  // Forward-compat: a [1m]/-1m id the registry doesn't list still gets 1M.
  // Boundary excludes digits ([^a-z0-9]) so "10m"/"21m" do NOT match — keeps
  // parity with the Python _has_1m_token (model_registry.py).
  if (canonicalOrId.includes('[1m]') || /(^|[^a-z0-9])1m([^a-z0-9]|$)/i.test(canonicalOrId)) {
    return 1_000_000
  }
  return 200_000
}

/** Dropdown rows (canonical key + display + window), default first. */
export function displayModels(): { name: string; description: string; contextWindow: number }[] {
  return Object.entries(REGISTRY)
    .filter(([, e]) => PROVIDER in e.providers)
    .sort(([, a], [, b]) => (b.default ? 1 : 0) - (a.default ? 1 : 0))
    .map(([key, e]) => ({ name: key, description: e.display, contextWindow: e.window }))
}
