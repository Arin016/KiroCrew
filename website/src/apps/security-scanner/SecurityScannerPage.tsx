/**
 * Security Scanner — builtin dashboard page.
 *
 * Self-contained port of the app's UI: React hooks + fetch against the builtin
 * backend (``/api/apps/security-scanner/*``), inline styles keyed off the
 * dashboard theme CSS custom properties. Kept dependency-light (no ui-kit /
 * MarkdownRenderer imports) so it renders identically on every theme and stays
 * simple to review. "Scan Now" launches the security-scan skill in a background
 * agent slot via ``/api/chat?ws=1`` — the page never drives the scan itself.
 *
 * All user-facing strings resolve through i18nT under the
 * ``apps.securityScanner.page.*`` catalog namespace.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { RefreshCw, ShieldCheck } from 'lucide-react'
import Clickable from '../../components/Clickable'
import { i18nT } from '../../i18n/t'

const t = (k: string, vars?: Record<string, unknown>) => i18nT(`apps.securityScanner.page.${k}`, vars)

const ACCENT = '#7c3aed'
const ACCENT_SUBTLE = '#e8d5f5'
const tabList = ['overview', 'findings', 'knowledge', 'exploit-lab', 'settings'] as const
type Tab = (typeof tabList)[number]

const SEV_COLOR: Record<string, string> = {
  critical: '#b91c1c', high: '#b45309', medium: '#7c3aed', low: '#6b7280', info: '#6b7280',
}
const STATUS_COLOR: Record<string, string> = {
  exploited: '#b91c1c', confirmed: '#b45309', 'pattern-learned': '#7c3aed',
  blocked: '#6b7280', suppressed: '#6b7280',
}

const tabLabel = (tv: Tab): string =>
  tv === 'overview' ? t('tab_overview')
    : tv === 'findings' ? t('tab_findings')
      : tv === 'knowledge' ? t('tab_knowledge')
        : tv === 'exploit-lab' ? t('tab_exploit_lab')
          : t('tab_settings')

interface Finding {
  id: string
  topic: string
  title: string
  location: string
  severity: string
  description?: string
  exploit_suggestion?: string
  status: string
  evidence?: string
}

interface Pattern {
  id: string
  topic: string
  pattern: string
  source?: string
  confidence: number
}

interface Status {
  running?: boolean
  last_scan_at?: string
  findings_total?: number
  findings_by_status?: Record<string, number>
  findings_by_severity?: Record<string, number>
  patterns_total?: number
  coverage?: Record<string, number>
  avg_false_positive_rate?: number
}

const BASE = '/api/apps/security-scanner'

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const resp = await fetch(BASE + path, opts)
  if (!resp.ok) throw new Error('HTTP ' + resp.status)
  return (await resp.json()) as T
}

function Pill({ text, bg, fg }: { text: string; bg: string; fg: string }) {
  return (
    <span style={{ background: bg, color: fg, padding: '2px 7px', borderRadius: '9999px', fontSize: 10, fontWeight: 600, letterSpacing: '0.02em', whiteSpace: 'nowrap' }}>
      {text}
    </span>
  )
}

function Card({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{ background: 'var(--card, #1a1b26)', border: '1px solid var(--border, #2d2f3d)', borderRadius: 6, padding: 14, ...(style || {}) }}>
      {children}
    </div>
  )
}

function StatCard({ label, value, sub, color }: { label: string; value: React.ReactNode; sub?: string; color?: string }) {
  return (
    <Card style={{ flex: 1 }}>
      <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: color || 'var(--text, #e2e8f0)', marginTop: 2 }}>{value}</div>
      {sub ? <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', marginTop: 2 }}>{sub}</div> : null}
    </Card>
  )
}

export default function SecurityScannerPage() {
  const [tab, setTab] = useState<Tab>('overview')
  const [status, setStatus] = useState<Status | null>(null)
  const [findings, setFindings] = useState<Finding[]>([])
  const [patterns, setPatterns] = useState<Pattern[]>([])
  const [selected, setSelected] = useState<Finding | null>(null)
  const [err, setErr] = useState('')
  const [scanning, setScanning] = useState(false)
  const [scanErr, setScanErr] = useState('')
  const [ingestText, setIngestText] = useState('')
  const [ingesting, setIngesting] = useState(false)
  const [filter, setFilter] = useState('')
  const mounted = useRef(true)
  useEffect(() => () => { mounted.current = false }, [])

  const load = useCallback(async () => {
    try {
      const [st, fd, kn] = await Promise.all([
        api<Status>('/status'),
        api<{ findings: Finding[] }>('/findings'),
        api<{ patterns: Pattern[] }>('/knowledge'),
      ])
      setStatus(st)
      setFindings(fd.findings || [])
      setPatterns(kn.patterns || [])
      setErr('')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    load()
    const id = window.setInterval(load, 30000)
    return () => window.clearInterval(id)
  }, [load])

  const scanNow = useCallback(async () => {
    setScanning(true)
    setScanErr('')
    try {
      const resp = await fetch('/api/chat?ws=1', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message:
            'Run the security-scan skill against the Kiro Crew codebase for all active topics now. Persist findings and notify only on new actionable findings.',
          slot: 'security-scanner-scan',
        }),
      })
      if (!resp.ok) throw new Error('HTTP ' + resp.status)
    } catch {
      setScanErr(t('scan_error'))
      setScanning(false)
      return
    }
    // Launched. Reflect the REAL run state: poll status until a scan is
    // observed running (or a bounded wait elapses), rather than a blind timer
    // that flips the button back regardless of what actually happened.
    for (let i = 0; i < 10; i++) {
      await new Promise((r) => window.setTimeout(r, 1500))
      if (!mounted.current) return
      const st = await api<Status>('/status').catch(() => null)
      if (!mounted.current) return
      if (st) {
        setStatus(st)
        if (st.running) break
      }
    }
    if (!mounted.current) return
    setScanning(false)
    load()
  }, [load])

  const ingest = useCallback(async () => {
    if (!ingestText.trim()) return
    setIngesting(true)
    try {
      await api('/knowledge/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: ingestText }),
      })
    } catch {
      /* surfaced via reload */
    }
    setIngestText('')
    setIngesting(false)
    load()
  }, [ingestText, load])

  const s = status || {}
  const cov = s.coverage || {}
  const covMax = Math.max(1, ...Object.values(cov))
  const exploited = (s.findings_by_status || {}).exploited || 0

  const tabBar = (
    <div style={{ display: 'flex', gap: 2, marginBottom: 16, background: 'var(--bg, #12131a)', borderRadius: 8, padding: 3 }}>
      {tabList.map((tv) => (
        <button
          key={tv}
          onClick={() => { setTab(tv); setSelected(null) }}
          style={{
            fontSize: 11, padding: '6px 12px', borderRadius: 6, border: 'none', cursor: 'pointer', fontWeight: 500,
            background: tab === tv ? 'var(--card, #1a1b26)' : 'transparent',
            color: tab === tv ? 'var(--text, #e2e8f0)' : 'var(--muted, #6b7280)',
          }}
        >
          {tabLabel(tv)}
        </button>
      ))}
    </div>
  )

  let body: React.ReactNode
  if (err) {
    body = <Card><div style={{ fontSize: 12, color: 'var(--muted, #6b7280)' }} title={err}>{t('backend_unreachable')}</div></Card>
  } else if (selected) {
    body = (
      <Card>
        <button onClick={() => setSelected(null)} style={{ fontSize: 11, color: ACCENT, background: 'none', border: 'none', cursor: 'pointer', padding: 0, marginBottom: 8 }}>{t('back')}</button>
        <div style={{ fontSize: 14, fontWeight: 600 }}>{selected.title}</div>
        <div style={{ fontSize: 11, color: 'var(--muted, #6b7280)', margin: '4px 0 10px', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <Pill text={selected.severity.toUpperCase()} bg="transparent" fg={SEV_COLOR[selected.severity] || '#6b7280'} />
          <Pill text={selected.status} bg="transparent" fg={STATUS_COLOR[selected.status] || '#6b7280'} />
          <span>{selected.location}</span>
          <span>{selected.topic}</span>
        </div>
        {selected.description ? <div style={{ fontSize: 12, marginBottom: 10, lineHeight: 1.5 }}>{selected.description}</div> : null}
        {selected.exploit_suggestion ? (
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', textTransform: 'uppercase' }}>{t('exploit_suggestion')}</div>
            <div style={{ fontSize: 11, fontFamily: 'monospace', background: 'var(--bg, #12131a)', padding: '6px 8px', borderRadius: 4, marginTop: 2 }}>{selected.exploit_suggestion}</div>
          </div>
        ) : null}
        {selected.evidence ? (
          <div>
            <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', textTransform: 'uppercase' }}>{t('exploit_evidence')}</div>
            <pre style={{ fontSize: 11, fontFamily: 'monospace', background: 'var(--bg, #12131a)', padding: 8, borderRadius: 4, marginTop: 2, whiteSpace: 'pre-wrap', overflowX: 'auto' }}>{selected.evidence}</pre>
          </div>
        ) : null}
      </Card>
    )
  } else if (tab === 'overview') {
    body = (
      <>
        <div style={{ display: 'flex', gap: 12, marginBottom: 16 }}>
          <StatCard label={t('stat_vulns')} value={exploited} sub={t('stat_vulns_sub')} color={exploited > 0 ? SEV_COLOR.critical : undefined} />
          <StatCard label={t('stat_patterns')} value={s.patterns_total || 0} sub={t('stat_patterns_sub')} />
          <StatCard label={t('stat_findings')} value={s.findings_total || 0} sub={t('stat_findings_sub')} />
          <StatCard label={t('stat_fp')} value={`${Math.round((s.avg_false_positive_rate || 0) * 100)}%`} sub={t('stat_fp_sub')} color={(s.avg_false_positive_rate || 0) <= 0.15 ? '#047857' : (s.avg_false_positive_rate || 0) <= 0.4 ? SEV_COLOR.high : SEV_COLOR.critical} />
        </div>
        <div style={{ fontSize: 13, fontWeight: 600, margin: '4px 0 8px' }}>{t('coverage_title')}</div>
        <Card>
          {Object.keys(cov).length === 0 ? (
            <div style={{ fontSize: 11, color: 'var(--muted, #6b7280)' }}>{t('coverage_empty')}</div>
          ) : (
            Object.entries(cov).map(([topic, n]) => (
              <div key={topic} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0' }}>
                <div style={{ width: 130, fontSize: 11, color: 'var(--muted, #6b7280)' }}>{topic}</div>
                <div style={{ flex: 1, height: 6, background: 'var(--bg, #12131a)', borderRadius: 3, overflow: 'hidden' }}>
                  <div style={{ width: `${(n / covMax) * 100}%`, height: '100%', background: ACCENT, borderRadius: 3 }} />
                </div>
                <div style={{ width: 28, textAlign: 'right', fontSize: 10, color: 'var(--muted, #6b7280)' }}>{n}</div>
              </div>
            ))
          )}
        </Card>
      </>
    )
  } else if (tab === 'findings') {
    const filters = ['', 'critical', 'high', 'exploited', 'blocked']
    const list = findings.filter((f) => !filter || f.status === filter || f.severity === filter)
    body = (
      <>
        <div style={{ display: 'flex', gap: 6, marginBottom: 10, flexWrap: 'wrap' }}>
          {filters.map((f) => (
            <button
              key={f || 'all'}
              aria-label={f || t('filter_all')}
              onClick={() => setFilter(f)}
              style={{
                fontSize: 11, padding: '4px 10px', borderRadius: 9999, cursor: 'pointer', fontWeight: 500,
                border: `1px solid ${filter === f ? ACCENT : 'var(--border, #2d2f3d)'}`,
                background: filter === f ? ACCENT_SUBTLE : 'transparent',
                color: filter === f ? ACCENT : 'var(--muted, #6b7280)',
              }}
            >
              {f || t('filter_all')}
            </button>
          ))}
        </div>
        {list.length === 0 ? (
          <Card><div style={{ fontSize: 11, color: 'var(--muted, #6b7280)' }}>{filter ? t('findings_empty_filtered') : t('findings_empty')}</div></Card>
        ) : (
          <Card style={{ padding: 0 }}>
            {list.map((f, i) => (
              <Clickable
                key={f.id}
                onClick={() => setSelected(f)}
                style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '10px 14px', cursor: 'pointer', borderBottom: i < list.length - 1 ? '1px solid var(--border, #2d2f3d)' : 'none' }}
              >
                <div style={{ width: 6, height: 6, borderRadius: '50%', marginTop: 5, flexShrink: 0, background: SEV_COLOR[f.severity] || '#6b7280' }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 500 }}>{f.title}</div>
                  <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', display: 'flex', gap: 8, marginTop: 2, flexWrap: 'wrap' }}>
                    <span>{f.location}</span>
                    <span>{f.topic}</span>
                  </div>
                </div>
                <Pill text={f.status === 'exploited' ? t('status_exploited') : f.status} bg="transparent" fg={STATUS_COLOR[f.status] || '#6b7280'} />
              </Clickable>
            ))}
          </Card>
        )}
      </>
    )
  } else if (tab === 'knowledge') {
    body = (
      <>
        <Card style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>{t('ingest_title')}</div>
          <textarea
            value={ingestText}
            onChange={(e) => setIngestText(e.target.value)}
            placeholder={t('ingest_placeholder')}
            style={{ width: '100%', minHeight: 70, fontSize: 11, padding: 8, borderRadius: 4, background: 'var(--bg, #12131a)', color: 'var(--text, #e2e8f0)', border: '1px solid var(--border, #2d2f3d)', boxSizing: 'border-box', resize: 'vertical' }}
          />
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 8 }}>
            <button
              disabled={ingesting || !ingestText.trim()}
              onClick={ingest}
              style={{ fontSize: 11, padding: '5px 14px', borderRadius: 9999, border: 'none', fontWeight: 500, background: ingesting || !ingestText.trim() ? 'var(--border, #2d2f3d)' : ACCENT, color: '#fff', cursor: ingesting || !ingestText.trim() ? 'default' : 'pointer' }}
            >
              {ingesting ? t('ingesting') : t('ingest_button')}
            </button>
          </div>
        </Card>
        <Card style={{ padding: 0 }}>
          <div style={{ padding: '12px 14px', borderBottom: '1px solid var(--border, #2d2f3d)', fontSize: 12, fontWeight: 600 }}>{t('learned_patterns')} ({patterns.length})</div>
          {patterns.length === 0 ? (
            <div style={{ padding: '12px 14px', fontSize: 11, color: 'var(--muted, #6b7280)' }}>{t('patterns_empty')}</div>
          ) : (
            patterns.map((p, i) => (
              <div key={p.id} style={{ padding: '10px 14px', borderBottom: i < patterns.length - 1 ? '1px solid var(--border, #2d2f3d)' : 'none' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                  <div style={{ fontSize: 11, fontWeight: 600 }}>{p.topic}</div>
                  <Pill text={`${p.source || ''} · ${Math.round(p.confidence * 100)}%`} bg={ACCENT_SUBTLE} fg={ACCENT} />
                </div>
                <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', fontFamily: 'monospace' }}>{p.pattern}</div>
              </div>
            ))
          )}
        </Card>
      </>
    )
  } else if (tab === 'exploit-lab') {
    const validated = findings.filter((f) => f.status === 'exploited' || f.status === 'blocked')
    body = validated.length === 0 ? (
      <Card><div style={{ fontSize: 11, color: 'var(--muted, #6b7280)' }}>{t('exploit_lab_empty')}</div></Card>
    ) : (
      <Card style={{ padding: 0 }}>
        {validated.map((f, i) => (
          <div key={f.id} style={{ padding: '10px 14px', borderBottom: i < validated.length - 1 ? '1px solid var(--border, #2d2f3d)' : 'none' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <div style={{ fontSize: 11, fontWeight: 500 }}>{f.title}</div>
              <Pill text={f.status === 'exploited' ? t('status_exploited') : t('status_blocked')} bg={f.status === 'exploited' ? '#fde2e1' : 'var(--bg, #12131a)'} fg={f.status === 'exploited' ? '#b91c1c' : '#6b7280'} />
            </div>
            {f.evidence ? <pre style={{ fontSize: 10, color: 'var(--muted, #6b7280)', fontFamily: 'monospace', background: 'var(--bg, #12131a)', padding: '6px 8px', borderRadius: 4, margin: '4px 0 0', whiteSpace: 'pre-wrap', overflowX: 'auto' }}>{f.evidence}</pre> : null}
            <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', marginTop: 4 }}>{f.location} · {f.topic}</div>
          </div>
        ))}
      </Card>
    )
  } else {
    const row = (label: string, value: string) => (
      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border, #2d2f3d)', fontSize: 12 }}>
        <span style={{ color: 'var(--muted, #6b7280)' }}>{label}</span>
        <span>{value}</span>
      </div>
    )
    body = (
      <Card>
        {row(t('settings_schedule_label'), t('settings_schedule_value'))}
        {row(t('settings_topics_label'), t('settings_topics_value'))}
        {row(t('settings_sandbox_label'), t('settings_sandbox_value'))}
        {row(t('settings_lastscan_label'), s.last_scan_at || '—')}
        <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', marginTop: 10, lineHeight: 1.5 }}>
          {t('settings_footer')}
        </div>
      </Card>
    )
  }

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: 16, fontFamily: 'system-ui, -apple-system, sans-serif', color: 'var(--text, #e2e8f0)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <ShieldCheck className="lucide-inline" style={{ width: 20, height: 20, color: ACCENT }} aria-hidden />
          <h2 style={{ margin: 0, fontSize: 18 }}>{t('title')}</h2>
          <Pill text={t('cadence_pill')} bg={ACCENT_SUBTLE} fg={ACCENT} />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 11, color: 'var(--muted, #6b7280)' }}>
            {status?.running
              ? t('status_running')
              : status?.last_scan_at
                ? t('status_last', { ts: status.last_scan_at.replace('T', ' ').replace('Z', '') })
                : t('status_none')}
          </span>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 3 }}>
            <button
              disabled={scanning || !!status?.running}
              onClick={scanNow}
              style={{ fontSize: 11, padding: '5px 14px', borderRadius: 9999, fontWeight: 500, border: `1px solid ${ACCENT_SUBTLE}`, background: scanning ? 'var(--border, #2d2f3d)' : 'transparent', color: scanning ? 'var(--muted, #6b7280)' : ACCENT, cursor: scanning ? 'default' : 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6 }}
            >
              <RefreshCw className="lucide-inline" style={{ width: 13, height: 13 }} aria-hidden />
              {scanning ? t('btn_starting') : t('btn_scan_now')}
            </button>
            {scanErr ? <span style={{ fontSize: 10, color: SEV_COLOR.critical }}>{scanErr}</span> : null}
          </div>
        </div>
      </div>
      {tabBar}
      {body}
    </div>
  )
}
