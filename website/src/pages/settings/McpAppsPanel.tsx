import { useState } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink, FileText, LayoutDashboard, PenLine } from 'lucide-react'
import { Card, CardTitle } from '../../components/ui'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api } from '../../api/client'
import type { DashboardConfig } from '../chat/ChatSettings'
import { i18nT } from '../../i18n/t'

type GatewayStatus = {
  enabled: boolean
  apps_enabled: boolean
  running: boolean
  ping_ok: boolean
  supported: boolean
}

/** URL for the MCP Apps extension overview, so the panel can teach the concept
 * rather than assume it. Kept a code constant, not a catalog value: a translator
 * must not be able to retarget an outbound link. */
const MCP_APPS_DOC_URL = 'https://modelcontextprotocol.io/extensions/apps/overview'

/**
 * Presentation state of the MCP Apps render switch.
 *
 * Pure so the rule is testable without rendering.
 *
 * `checked` is `apps_enabled` and nothing else. That key is the SOLE grant for
 * rendering: the render stub is emitted for every stdio server unconditionally,
 * and `mcp_gateway.enabled` chooses only whether backends are SHARED between
 * sessions. The broker starts for either switch, and `apps_enabled` defaults
 * true, so a default install renders apps with nothing turned on.
 *
 * This does NOT show a conjunction with `mcp_gateway.enabled`.
 * Doing so would read OFF on a default install while rendering actually worked —
 * the inverse of the defect it would be trying to prevent, and the worse
 * direction, because it denies a capability the user has rather than promising
 * one they lack.
 */
export function mcpAppsSwitchState(s: {
  appsEnabled: boolean
  loading: boolean
  busy: boolean
}): { checked: boolean; disabled: boolean } {
  return {
    checked: s.appsEnabled,
    disabled: s.loading || s.busy,
  }
}

/** One illustrative example of what an MCP App replaces a wall of text with.
 *
 * Not a bordered, filled box, on purpose: that is this dashboard's visual
 * grammar for a control, and these are captions — nothing here is clickable.
 * Muted inline text with an icon reads as an example list instead. */
function Example({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <li className="flex items-center gap-1.5">
      {icon}
      <span>{label}</span>
    </li>
  )
}

/**
 * User-facing home for MCP Apps: what they are, whether they render, and where.
 *
 * Lives under Connections, beside the Shared MCP backends tab it depends on:
 * rendering server-authored UI in the conversation is a chat feature and a
 * consent decision, and Connections is the only place a user configures MCP at
 * all. The dependency is real rather than incidental — a server only receives
 * the render stub while it is shared — so the panel states it, discloses what
 * enabling it will do before the click, and then carries it out rather than
 * leaving the user to discover a second switch elsewhere.
 */
export function McpAppsPanel() {
  const qc = useQueryClient()
  const statusQ = useQuery<GatewayStatus>({
    queryKey: ['mcpGatewayStatus'],
    queryFn: () => api.mcpGatewayStatus(),
  })
  const dashQ = useQuery<DashboardConfig>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
  })

  // Default true so a still-loading status (or a backend predating the field)
  // never disables the control; only a definite `false` gates it.
  const supported = statusQ.data?.supported ?? true

  // Optimistic value held only for the duration of the request. The status query
  // is the source of truth, so the override is DROPPED once the refetch lands —
  // holding it would pin this tab to its own last write and hide a change made
  // anywhere else.
  const [appsPending, setAppsPending] = useState<boolean | null>(null)
  const [appsBusy, setAppsBusy] = useState(false)
  const [appsError, setAppsError] = useState<string | null>(null)

  const appsEnabled = appsPending ?? statusQ.data?.apps_enabled ?? true
  const appsState = mcpAppsSwitchState({
    appsEnabled,
    loading: statusQ.isLoading,
    busy: appsBusy,
  })

  const dashMut = useMutation({
    mutationFn: (next: DashboardConfig) => api.updateDashboardConfig(next),
    onMutate: async (next) => {
      await qc.cancelQueries({ queryKey: ['dashboardConfig'] })
      const prev = qc.getQueryData<DashboardConfig>(['dashboardConfig'])
      qc.setQueryData(['dashboardConfig'], next)
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(['dashboardConfig'], ctx.prev)
      setAppsError(i18nT('pages.settings.mcpAppsPanel.panel_save_failed'))
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['dashboardConfig'] }),
  })

  const sidePanel = dashQ.data?.mcp_app_panel ?? false
  const dashDisabled = !dashQ.isSuccess

  const runApps = async (next: boolean) => {
    setAppsBusy(true)
    setAppsError(null)
    setAppsPending(next)
    try {
      const r = await api.mcpGatewayAppsEnable(next)
      // Seed the cache from the RESPONSE before invalidating. Dropping the local
      // override on the way out is only safe if the cache already carries the new
      // value — otherwise a failed refetch leaves the switch showing stale state.
      qc.setQueryData(['mcpGatewayStatus'], (prev: GatewayStatus | undefined) =>
        prev ? { ...prev, apps_enabled: r.enabled } : prev)
      await qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
    } catch (e) {
      // Prefer the server's message: this endpoint's refusals are actionable
      // ("…is set in config.local.json; edit that file instead") and collapsing
      // them into one generic line throws away the only usable instruction.
      const msg = e instanceof Error ? e.message.trim() : ''
      setAppsError(msg || i18nT('pages.settings.mcpAppsPanel.render_save_failed'))
    } finally {
      setAppsPending(null)
      setAppsBusy(false)
    }
  }

  return (
    <div aria-label={i18nT('pages.settings.mcpAppsPanel.aria_label')}>
      <Card>
        <CardTitle>
          <LayoutDashboard className="lucide-inline" aria-hidden="true" />
          {i18nT('pages.settings.mcpAppsPanel.what_title')}
        </CardTitle>
        <p className="text-sm text-muted leading-relaxed mb-3">
          {i18nT('pages.settings.mcpAppsPanel.what_body')}
        </p>
        <ul className="flex flex-wrap items-center gap-x-5 gap-y-1.5 mb-3 pl-0
          list-none text-sm text-muted">
          <Example
            icon={<PenLine className="lucide-inline text-accent" aria-hidden="true" />}
            label={i18nT('pages.settings.mcpAppsPanel.example_diagram')}
          />
          <Example
            icon={<LayoutDashboard className="lucide-inline text-accent" aria-hidden="true" />}
            label={i18nT('pages.settings.mcpAppsPanel.example_dashboard')}
          />
          <Example
            icon={<FileText className="lucide-inline text-accent" aria-hidden="true" />}
            label={i18nT('pages.settings.mcpAppsPanel.example_viewer')}
          />
        </ul>
        <p className="text-sm text-muted leading-relaxed">
          {i18nT('pages.settings.mcpAppsPanel.sandbox_body')}
        </p>
        <a
          href={MCP_APPS_DOC_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline mt-3"
        >
          {i18nT('pages.settings.mcpAppsPanel.learn_more')}
          <ExternalLink className="lucide-inline" aria-hidden="true" />
        </a>
      </Card>

      <SettingsSection title={i18nT('pages.settings.mcpAppsPanel.section_rendering')}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.mcpAppsPanel.render_label')}
            description={i18nT('pages.settings.mcpAppsPanel.render_description')}
            checked={appsState.checked}
            disabled={appsState.disabled}
            configKey="mcp_gateway.apps_enabled"
            onChange={next => void runApps(next)}
          />

          <SettingsToggle
            label={i18nT('pages.settings.mcpAppsPanel.side_panel_label')}
            description={i18nT('pages.settings.mcpAppsPanel.side_panel_description')}
            checked={sidePanel}
            disabled={dashDisabled}
            configKey="dashboard.mcp_app_panel"
            onChange={v => dashMut.mutate({ ...(dashQ.data as DashboardConfig), mcp_app_panel: v })}
          />
          {!supported && (
            <div className="text-sm text-muted mt-2">
              {i18nT('pages.settings.mcpAppsPanel.unsupported_platform')}
            </div>
          )}

          {appsError && (
            <div className="flex items-start gap-1.5 text-sm text-danger mt-2" aria-live="polite">
              <AlertTriangle className="lucide-inline shrink-0" aria-hidden="true" />
              <span>{appsError}</span>
            </div>
          )}
        </SettingsCard>
      </SettingsSection>
    </div>
  )
}
