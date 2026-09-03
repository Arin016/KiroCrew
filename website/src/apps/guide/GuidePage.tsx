// Kiro Crew Guide — thin builtin page.
//
// First version: a search box, a ranked entry list, and a detail pane, all
// driven by the app's own read-only API (/api/apps/guide/entries[...]). The
// same ranking is computed server-side and shared with the MCP tools, so this
// page stays a thin shell over that one source of truth. Theme-driven colors
// (var(--*)) keep it correct under light/dark/custom palettes; icons are
// lucide-react, never emoji.
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { LifeBuoy, Search, ChevronRight, Wrench, AlertCircle } from 'lucide-react'

const API = '/api/apps/guide'

interface Step {
  t?: string
  do?: string
  expect?: string
}

interface EntrySummary {
  id: string
  title?: string
  symptom?: string
  trust?: string
  fix?: string
}

interface EntryDetail extends EntrySummary {
  title_zh?: string
  platform?: string[] | string
  topic?: string
  steps?: Step[]
  if_stuck?: { text?: string; next?: string | null }
  crew_prompt?: string
  keywords?: string[]
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: 'same-origin' })
  if (!res.ok) throw new Error(`${res.status}`)
  return (await res.json()) as T
}

export default function GuidePage() {
  const { t } = useTranslation()
  const [query, setQuery] = useState('')
  const [entries, setEntries] = useState<EntrySummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<EntryDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null)

  const runSearch = useCallback(async (q: string) => {
    setLoading(true)
    setError(false)
    try {
      const data = await getJson<{ entries: EntrySummary[] }>(
        `${API}/entries?q=${encodeURIComponent(q)}&limit=25`,
      )
      setEntries(data.entries || [])
    } catch {
      setError(true)
      setEntries([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current)
    debounce.current = setTimeout(() => void runSearch(query), 200)
    return () => {
      if (debounce.current) clearTimeout(debounce.current)
    }
  }, [query, runSearch])

  const selectEntry = useCallback(async (id: string) => {
    setSelectedId(id)
    setDetail(null)
    try {
      setDetail(await getJson<EntryDetail>(`${API}/entries/${encodeURIComponent(id)}`))
    } catch {
      setDetail(null)
    }
  }, [])

  return (
    <div className="flex h-full min-h-0" style={{ background: 'var(--bg)', color: 'var(--text)' }}>
      {/* List column */}
      <div
        className="flex flex-col min-h-0 w-80 shrink-0"
        style={{ borderRight: '1px solid var(--border)' }}
      >
        <div className="p-4" style={{ borderBottom: '1px solid var(--border)' }}>
          <div className="flex items-center gap-2 mb-3">
            <LifeBuoy size={18} style={{ color: 'var(--accent)' }} />
            <span className="font-semibold text-sm">{t('apps.guide.title')}</span>
          </div>
          <div
            className="flex items-center gap-2 px-3 py-2 rounded-lg"
            style={{ border: '1px solid var(--border)', background: 'var(--card)' }}
          >
            <Search size={15} style={{ color: 'var(--muted)' }} />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('apps.guide.searchPlaceholder')}
              className="flex-1 bg-transparent outline-none text-sm"
              style={{ color: 'var(--text)' }}
            />
          </div>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto">
          {error && (
            <div className="flex items-center gap-2 p-4 text-sm" style={{ color: 'var(--warn)' }}>
              <AlertCircle size={15} />
              {t('apps.guide.errorLoading')}
            </div>
          )}
          {!error && loading && entries.length === 0 && (
            <div className="p-4 text-sm" style={{ color: 'var(--muted)' }}>
              {t('apps.guide.loading')}
            </div>
          )}
          {!error && !loading && entries.length === 0 && (
            <div className="p-4 text-sm" style={{ color: 'var(--muted)' }}>
              {t('apps.guide.noResults')}
            </div>
          )}
          {entries.map((e) => (
            <button
              key={e.id}
              onClick={() => void selectEntry(e.id)}
              className="w-full text-left px-4 py-3 flex items-start gap-2"
              style={{
                borderBottom: '1px solid var(--border)',
                background: e.id === selectedId ? 'var(--card)' : 'transparent',
              }}
            >
              <ChevronRight
                size={14}
                className="mt-0.5 shrink-0"
                style={{ color: 'var(--muted)' }}
              />
              <span>
                <span className="block text-sm font-medium">{e.title}</span>
                {e.symptom && (
                  <span className="block text-xs mt-0.5" style={{ color: 'var(--muted)' }}>
                    {e.symptom}
                  </span>
                )}
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* Detail column */}
      <div className="flex-1 min-h-0 overflow-y-auto p-6">
        {!detail && (
          <div
            className="h-full flex items-center justify-center text-sm"
            style={{ color: 'var(--muted)' }}
          >
            {t('apps.guide.selectPrompt')}
          </div>
        )}
        {detail && (
          <div className="max-w-2xl">
            <h1 className="text-xl font-semibold">{detail.title}</h1>
            {detail.symptom && (
              <>
                <h2
                  className="text-xs font-semibold uppercase tracking-wide mt-5 mb-1"
                  style={{ color: 'var(--muted)' }}
                >
                  {t('apps.guide.symptomLabel')}
                </h2>
                <p className="text-sm">{detail.symptom}</p>
              </>
            )}
            {detail.steps && detail.steps.length > 0 && (
              <>
                <h2
                  className="text-xs font-semibold uppercase tracking-wide mt-5 mb-2"
                  style={{ color: 'var(--muted)' }}
                >
                  {t('apps.guide.stepsLabel')}
                </h2>
                <ol className="flex flex-col gap-3">
                  {detail.steps.map((s, i) => (
                    <li
                      key={i}
                      className="rounded-lg p-3 text-sm"
                      style={{ border: '1px solid var(--border)', background: 'var(--card)' }}
                    >
                      {s.do && <div>{s.do}</div>}
                      {s.expect && (
                        <div className="text-xs mt-1" style={{ color: 'var(--muted)' }}>
                          {s.expect}
                        </div>
                      )}
                    </li>
                  ))}
                </ol>
              </>
            )}
            {detail.if_stuck?.text && (
              <p
                className="text-sm mt-5 rounded-lg p-3"
                style={{
                  border: '1px solid color-mix(in srgb, var(--warn) 45%, transparent)',
                }}
              >
                {detail.if_stuck.text}
              </p>
            )}
            {detail.crew_prompt && (
              <>
                <h2
                  className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide mt-5 mb-1"
                  style={{ color: 'var(--muted)' }}
                >
                  <Wrench size={13} />
                  {t('apps.guide.crewPromptLabel')}
                </h2>
                <p
                  className="text-sm rounded-lg p-3 font-mono"
                  style={{ background: 'var(--card)', border: '1px solid var(--border)' }}
                >
                  {detail.crew_prompt}
                </p>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
