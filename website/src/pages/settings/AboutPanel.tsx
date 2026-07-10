import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Info, RefreshCw, Scale, CheckCircle2, AlertCircle } from 'lucide-react'
import { Card, CardTitle, Btn } from '../../components/ui'
import { useBranding } from '../../hooks/useBranding'
import { useAppSelector } from '../../store'
import { codeBrowserBranchUrl, codeBrowserCommitUrl } from '../../lib/codeBrowser'

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
  const { botName } = useBranding()
  const gatewayVersion = useAppSelector(s => s.dashboard.status?.version) || ''
  const buildBranch = useAppSelector(s => s.dashboard.status?.branch) || ''
  const buildCommit = useAppSelector(s => s.dashboard.status?.commit) || ''
  const queryClient = useQueryClient()
  const api = getUpdateApi()
  const isDesktop = !!api

  // Server state: app info (version, channel, platform)
  const { data: info } = useQuery({
    queryKey: ['update-info'],
    queryFn: () => api!.getInfo(),
    enabled: isDesktop,
    staleTime: Infinity, // static per session
  })

  // Update lifecycle state, read from the shared cache that
  // useUpdateSubscription (mounted in App.tsx) populates. Reading the same
  // query key keeps About and UpdateModal consistent regardless of which
  // page the events arrived on.
  const { data: updateState } = useQuery<UpdateState | null>({
    queryKey: ['update-state'],
    queryFn: () => null,
    enabled: false,
    staleTime: Infinity,
  })

  // Manual check action
  const checkMutation = useMutation({
    mutationFn: () => api!.check(),
    onMutate: () => queryClient.setQueryData(['update-state'], null),
  })

  const version = info?.version || gatewayVersion || '\u2014'
  const channel = info?.channel
  const updatesDisabled = info?.disabled
  const checking = checkMutation.isPending || updateState?.state === 'checking'

  // Status line under the Check button.
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

  return (
    <>
      <Card>
        <CardTitle><Info size={15} className="lucide-inline" /> About</CardTitle>
        <Row label="Application">{botName || 'KiroClaw'}</Row>
        <Row label="Version">{version}</Row>
        {buildBranch && <Row label="Branch"><a href={codeBrowserBranchUrl(buildBranch)} target="_blank" rel="noopener noreferrer" className="hover:underline" title="Browse this branch on GitHub">{buildBranch}</a></Row>}
        {buildCommit && <Row label="Commit"><a href={codeBrowserCommitUrl(buildCommit)} target="_blank" rel="noopener noreferrer" className="hover:underline" title="View this commit on GitHub">{buildCommit}</a></Row>}
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
                KiroClaw checks for updates automatically. You can also check now.
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
          <p className="text-sm text-muted">
            Updates are managed by the KiroClaw desktop app. Open the desktop app to check for and install updates.
          </p>
        )}
      </Card>

      <Card>
        <CardTitle><Scale size={15} className="lucide-inline" /> License</CardTitle>
        <Row label="License">Apache 2.0</Row>
        <p className="mt-2 text-[13px] text-muted">
          KiroClaw is open source. Memory, scheduling, and agent orchestration run locally on your machine.
        </p>
      </Card>
    </>
  )
}
