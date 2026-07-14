import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Scale, CheckCircle2, AlertCircle, GitBranch, GitCommitHorizontal, ExternalLink, ArrowUp, Package, X } from 'lucide-react'
import { Card, CardTitle, Btn } from '../../components/ui'
import { useBranding } from '../../hooks/useBranding'
import { useAppSelector } from '../../store'
import { codeBrowserBranchUrl, codeBrowserCommitUrl } from '../../lib/codeBrowser'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { api, ApiError } from '../../api/client'

type UpdateState = {
  state: 'checking' | 'available' | 'downloading' | 'downloaded' | 'not-available' | 'error'
  version?: string
  notes?: string
  channel?: string
  message?: string
}

type UpdateInfo = {
  version?: string
  channel?: string
  platform?: string
  packaged?: boolean
  disabled?: string
}

type UpdateAPI = {
  onState: (cb: (payload: UpdateState) => void) => (() => void)
  check: () => Promise<unknown>
  getInfo: () => Promise<UpdateInfo>
}

function getUpdateApi(): UpdateAPI | undefined {
  return (window as unknown as { updateAPI?: UpdateAPI }).updateAPI
}

// Subtle accent tint for the version pill + build chips (works with any theme's
// --accent via color-mix; avoids depending on a tinted-bg token).
const ACCENT_TINT: React.CSSProperties = {
  background: 'color-mix(in oklab, var(--accent) 12%, transparent)',
  borderColor: 'color-mix(in oklab, var(--accent) 30%, transparent)',
}

/** Row: label on the left, value on the right. */
function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-1.5 text-sm">
      <span className="text-muted">{label}</span>
      <span className="text-text font-medium">{children}</span>
    </div>
  )
}

export function AboutPanel() {
  const { botName, avatar } = useBranding()
  const gatewayVersion = useAppSelector(s => s.dashboard.status?.version) || ''
  const buildBranch = useAppSelector(s => s.dashboard.status?.branch) || ''
  const buildCommit = useAppSelector(s => s.dashboard.status?.commit) || ''
  const updateAvailable = useAppSelector(s => s.dashboard.status?.update_available) || false
  const queryClient = useQueryClient()
  const desktopApi = getUpdateApi()
  const isDesktop = !!desktopApi

  // Desktop (Electron) app info (version, channel, platform)
  const { data: info } = useQuery({
    queryKey: ['update-info'],
    queryFn: () => desktopApi!.getInfo(),
    enabled: isDesktop,
    staleTime: Infinity, // static per session
  })

  // Desktop update lifecycle state, read from the shared cache that
  // useUpdateSubscription (mounted in App.tsx) populates.
  const { data: updateState } = useQuery<UpdateState | null>({
    queryKey: ['update-state'],
    queryFn: () => null,
    enabled: false,
    staleTime: Infinity,
  })

  // Desktop manual check action
  const checkMutation = useMutation({
    mutationFn: () => desktopApi!.check(),
    onMutate: () => queryClient.setQueryData(['update-state'], null),
  })

  const version = info?.version || gatewayVersion || '—'
  const channel = info?.channel
  const updatesDisabled = info?.disabled
  const checking = checkMutation.isPending || updateState?.state === 'checking'

  // Desktop status line under the Check button.
  let status: React.ReactNode = null
  if (checking) {
    status = <span className="text-muted flex items-center gap-1.5"><RefreshCw size={13} className="lucide-inline animate-spin" /> Checking for updates...</span>
  } else if (updateState?.state === 'not-available') {
    status = <span className="text-ok flex items-center gap-1.5"><CheckCircle2 size={13} className="lucide-inline" /> You are on the latest version.</span>
  } else if (updateState?.state === 'available' || updateState?.state === 'downloading') {
    status = <span className="text-accent flex items-center gap-1.5"><RefreshCw size={13} className="lucide-inline animate-spin" /> Update found — downloading...</span>
  } else if (updateState?.state === 'downloaded') {
    status = <span className="text-accent flex items-center gap-1.5"><CheckCircle2 size={13} className="lucide-inline" /> Update {updateState.version || ''} ready — see the install prompt.</span>
  } else if (updateState?.state === 'error') {
    status = <span className="text-danger flex items-center gap-1.5"><AlertCircle size={13} className="lucide-inline" /> Couldn't check for updates{updateState.message ? `: ${updateState.message}` : ''}.</span>
  }

  // --- Gateway (web dashboard) update flow ---
  // The gateway exposes /api/update/check + /api/update; used when not running
  // inside the Electron shell. "Check for updates" flips to "Update to vX" when
  // status.update_available is set; the update itself is gated behind a
  // changelog confirm because applying restarts the gateway.
  const [gwChanges, setGwChanges] = useState('')
  const [gwTarget, setGwTarget] = useState('')
  const [gwFound, setGwFound] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [applyError, setApplyError] = useState('')
  const [restarting, setRestarting] = useState(false)
  const [autoUpdate, setAutoUpdate] = useState(true)
  const { data: mcCfg } = useQuery({ queryKey: ['mc-config-autoupdate'], queryFn: () => api.kiroclawConfig() })
  useEffect(() => {
    const v = (mcCfg as any)?.auto_update
    if (typeof v === 'boolean') setAutoUpdate(v)
  }, [mcCfg])
  const gwCheck = useMutation({
    mutationFn: () => api.checkUpdate(),
    onSuccess: (d: any) => {
      setGwChanges(d?.changes || '')
      if (d?.version) setGwTarget(String(d.version))
      // Derive availability from the check response itself, not only the redux
      // status flag (which refreshes on a slower WS status push). Otherwise a
      // check that finds an update could still show "You're on the latest
      // version" until the flag catches up.
      setGwFound(!!d?.available)
      if (typeof d?.auto_update === 'boolean') setAutoUpdate(d.auto_update)
    },
  })
  const gwApply = useMutation({
    mutationFn: () => api.applyUpdate(),
    onSuccess: () => setRestarting(true),
    onError: (e: unknown) => {
      // A real server rejection (e.g. 409 dirty tree, 400) arrives as ApiError
      // with a status code — surface it. A bare network failure means the POST's
      // connection was reset by the gateway restart the update itself triggers;
      // that is the expected success path, not a failure.
      if (e instanceof ApiError) setApplyError(e.message || 'Update failed')
      else setRestarting(true)
    },
  })
  // Update is available if either the redux status flag or the latest check
  // response says so.
  const showUpdate = updateAvailable || gwFound

  // Escape closes the confirm dialog (unless an apply/restart is in flight),
  // matching the keyboard affordance of the settings dropdown it replaces.
  useEffect(() => {
    if (!showConfirm) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !gwApply.isPending && !restarting) setShowConfirm(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [showConfirm, gwApply.isPending, restarting])

  return (
    <>
      <Card>
        {/* Identity */}
        <div className="flex items-center gap-3.5 py-1.5">
          <img
            src={avatar}
            alt=""
            className="w-11 h-11 rounded-xl object-cover bg-bg-hover shrink-0"
            onError={e => { (e.currentTarget as HTMLImageElement).style.visibility = 'hidden' }}
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2.5 flex-wrap">
              <span className="text-[17px] font-bold text-text-strong">{botName || 'KiroClaw'}</span>
              <span className="text-[12px] font-mono font-semibold text-accent rounded-full px-2.5 py-0.5 border" style={ACCENT_TINT}>v{version}</span>
            </div>
            <div className="text-[12.5px] text-muted mt-0.5">Autonomous agent management · open source</div>
          </div>
        </div>

        {/* Build metadata */}
        {(buildBranch || buildCommit) && (
          <div className="mt-3 pt-3 border-t border-border flex flex-wrap gap-2">
            {buildBranch && (
              <a href={codeBrowserBranchUrl(buildBranch)} target="_blank" rel="noopener noreferrer"
                 title="Browse this branch on GitHub"
                 className="inline-flex items-center gap-1.5 text-[12px] font-mono text-accent border rounded-lg px-2.5 py-1 no-underline hover:underline" style={ACCENT_TINT}>
                <GitBranch size={12} className="shrink-0" /> <span className="truncate max-w-[220px]">{buildBranch}</span> <ExternalLink size={10} className="opacity-60 shrink-0" />
              </a>
            )}
            {buildCommit && (
              <a href={codeBrowserCommitUrl(buildCommit)} target="_blank" rel="noopener noreferrer"
                 title="View this commit on GitHub"
                 className="inline-flex items-center gap-1.5 text-[12px] font-mono text-accent border rounded-lg px-2.5 py-1 no-underline hover:underline" style={ACCENT_TINT}>
                <GitCommitHorizontal size={12} className="shrink-0" /> {buildCommit} <ExternalLink size={10} className="opacity-60 shrink-0" />
              </a>
            )}
          </div>
        )}

        {isDesktop && channel && <Row label="Update channel">{channel}</Row>}
        {isDesktop && info?.platform && <Row label="Platform">{info.platform}</Row>}
      </Card>

      <Card>
        <CardTitle><RefreshCw size={15} className="lucide-inline" /> Updates</CardTitle>
        {isDesktop ? (
          updatesDisabled ? (
            <p className="text-sm text-muted">
              Automatic updates are unavailable in this build
              {updatesDisabled === 'dev' ? ' (development build).' : ' on this platform.'}
            </p>
          ) : (
            <div className="flex flex-col gap-2.5">
              <p className="text-sm text-muted">
                {botName || 'KiroClaw'} checks for updates automatically. You can also check now.
              </p>
              <div>
                <Btn primary onClick={() => checkMutation.mutate()} disabled={checking}>
                  <RefreshCw size={13} className={`lucide-inline ${checking ? 'animate-spin' : ''}`} /> Check for updates
                </Btn>
              </div>
              {status && <div className="text-[13px]">{status}</div>}
            </div>
          )
        ) : (
          <div className="flex flex-col gap-2.5">
            {showUpdate ? (
              <>
                <p className="text-sm text-muted flex items-center gap-1.5">
                  <ArrowUp size={13} className="lucide-inline text-accent" /> A new version{gwTarget ? ` (v${gwTarget})` : ''} is available.
                </p>
                <div>
                  <Btn primary onClick={() => { if (!gwChanges) gwCheck.mutate(); setApplyError(''); setRestarting(false); setShowConfirm(true) }}>
                    <ArrowUp size={13} className="lucide-inline" /> Update{gwTarget ? ` to v${gwTarget}` : ' now'}
                  </Btn>
                </div>
              </>
            ) : (
              <>
                <p className="text-sm text-muted">
                  {botName || 'KiroClaw'} checks for updates automatically. You can also check now.
                </p>
                <div>
                  <Btn onClick={() => gwCheck.mutate()} disabled={gwCheck.isPending}>
                    <RefreshCw size={13} className={`lucide-inline ${gwCheck.isPending ? 'animate-spin' : ''}`} /> Check for updates
                  </Btn>
                </div>
                {gwCheck.isSuccess && !showUpdate && (
                  <span className="text-ok text-[13px] flex items-center gap-1.5"><CheckCircle2 size={13} className="lucide-inline" /> You're on the latest version.</span>
                )}
                {gwCheck.isError && (
                  <span className="text-danger text-[13px] flex items-center gap-1.5"><AlertCircle size={13} className="lucide-inline" /> Couldn't check for updates.</span>
                )}
              </>
            )}
            <div className="flex items-center justify-between pt-2.5 border-t border-border">
              <span className="text-sm text-text">Auto-update on restart</span>
              <button role="switch" aria-checked={autoUpdate} aria-label="Auto-update on restart"
                title="Automatically pull and apply updates when the gateway restarts"
                className={`w-9 h-5 rounded-full transition-colors cursor-pointer border-none shrink-0 ${autoUpdate ? 'bg-accent' : 'bg-border'}`}
                onClick={async () => { const next = !autoUpdate; setAutoUpdate(next); try { await api.setAutoUpdate(next) } catch { setAutoUpdate(!next) } }}>
                <span className={`block w-3.5 h-3.5 rounded-full bg-white shadow transition-transform ${autoUpdate ? 'translate-x-4' : 'translate-x-0.5'}`} />
              </button>
            </div>
          </div>
        )}
      </Card>

      <Card>
        <CardTitle><Scale size={15} className="lucide-inline" /> License</CardTitle>
        <Row label="License">Apache 2.0</Row>
        <p className="mt-2 text-[13px] text-muted">
          {botName || 'KiroClaw'} is open source. Memory, scheduling, and agent orchestration run locally on your machine.
        </p>
      </Card>

      {/* Web update confirm — shows the changelog, then applies (which restarts the gateway). */}
      {showConfirm && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise"
             role="dialog" aria-modal="true" aria-label="Update"
             onClick={() => { if (!gwApply.isPending && !restarting) setShowConfirm(false) }}>
          <div role="document" className="bg-card border border-border rounded-xl p-6 max-w-md w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-3">
              <div className="text-sm font-bold text-text-strong flex items-center gap-1.5"><Package size={15} className="lucide-inline" /> Update{gwTarget ? ` to v${gwTarget}` : ''}</div>
              <button aria-label="Close" className="text-muted hover:text-text cursor-pointer bg-transparent border-none disabled:opacity-40 disabled:cursor-default" disabled={gwApply.isPending || restarting} onClick={() => { if (!gwApply.isPending && !restarting) setShowConfirm(false) }}><X size={15} /></button>
            </div>
            {gwCheck.isPending ? (
              <div className="text-[13px] text-muted flex items-center gap-1.5 mb-4"><RefreshCw size={13} className="lucide-inline animate-spin" /> Loading changelog…</div>
            ) : gwChanges ? (
              <>
                <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">What's new</div>
                <div className="p-3 bg-bg rounded-lg border border-border max-h-56 overflow-y-auto mb-4 text-[13px] text-text"><MarkdownRenderer content={gwChanges} /></div>
              </>
            ) : (
              <p className="text-[13px] text-muted mb-4">A newer version is available.</p>
            )}
            <p className="text-[12px] text-muted mb-3">Updating restarts the gateway — active sessions will briefly disconnect.</p>
            {applyError && <div className="text-[13px] text-danger mb-3 flex items-center gap-1.5"><AlertCircle size={13} className="lucide-inline" /> {applyError}</div>}
            {restarting ? (
              <div className="text-[13px] text-accent flex items-center justify-center gap-1.5 py-2" role="status">
                <RefreshCw size={13} className="lucide-inline animate-spin" /> Updating — gateway restarting…
              </div>
            ) : (
              <Btn primary className="w-full justify-center" disabled={gwApply.isPending} onClick={() => gwApply.mutate()}>
                {gwApply.isPending ? <><RefreshCw size={13} className="lucide-inline animate-spin" /> Updating…</> : 'Update now'}
              </Btn>
            )}
          </div>
        </div>
      )}
    </>
  )
}
