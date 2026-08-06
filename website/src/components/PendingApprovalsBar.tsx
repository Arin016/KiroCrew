import { useMemo, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Lock, ChevronUp, ChevronDown, CheckCircle, Ban } from 'lucide-react'
import { api } from '../api/client'
import ToolInputPreview from './ToolInputPreview'
import { SourceBadge, Btn } from './ui'
import Clickable from './Clickable'
import { i18nT } from '../i18n/t'

/** One pending tool approval as returned by GET /api/approvals. */
type PendingApproval = {
  id: string
  source?: string
  tool?: string
  tool_input?: string
  slot?: string
  ts?: number
}

const QUERY_KEY = ['global-approvals'] as const

/**
 * Persistent, always-on-screen surface for pending tool approvals that have NO
 * owning chat conversation. Background sources (taskrunner, cron, heartbeat,
 * autonudge) raise approvals with `slot=""` by design — borrowing the active
 * chat's slot was removed because it hijacked innocent conversations and
 * mis-scoped the Trust control. Those unowned approvals therefore only ever
 * reached the notification feed, which the user has to keep open or keep
 * checking. This bar surfaces exactly those, docked at the bottom of the main
 * content area so it rides above the chat composer and is visible on every
 * route.
 *
 * Owned-chat approvals are intentionally NOT shown here: they already render as
 * an inline permission card inside their conversation, so mirroring them would
 * duplicate the prompt and split its resolve path. Project task-gate approvals
 * (`task-gate-*`) are excluded too — they own a dedicated Projects nav badge.
 */
export default function PendingApprovalsBar() {
  const reduceMotion = useReducedMotion()
  const queryClient = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  // Ids with an in-flight resolve, held OUT of `pending` so the row leaves the
  // instant it is acted on. The shared query is both polled and
  // websocket-invalidated, so a concurrent refetch could otherwise re-add a
  // just-resolved row into the cache mid-flight; hiding by id makes the bar
  // immune to that regardless of what any refetch writes. Cleared only after
  // the post-resolve reconcile lands (see the mutation's onSettled), so a
  // FAILED resolve re-shows the row and stays retryable rather than vanishing.
  const [resolvingIds, setResolvingIds] = useState<ReadonlySet<string>>(new Set())

  // Mirrors the query the topbar already owns (same key + options): it is polled
  // on an interval AND invalidated in real time by the websocket layer on
  // `approval` / `approval_resolved`. React Query dedupes the two observers, so
  // this adds no extra network cost.
  const { data: approvals = [] } = useQuery<PendingApproval[]>({
    queryKey: QUERY_KEY,
    queryFn: () => api.approvals(),
    staleTime: 0,
    refetchInterval: 30_000,
  })

  const resolveMutation = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'approve' | 'reject' }) =>
      api.resolveApproval(id, action),
    onMutate: ({ id }) => {
      setResolvingIds(prev => new Set(prev).add(id))
    },
    onSettled: async (_data, _err, { id }) => {
      // Reconcile against the authoritative server list BEFORE un-hiding the
      // row: awaiting the refetch means the cache reflects the resolve outcome
      // when the id leaves `resolvingIds`, so a resolved row stays gone and a
      // failed one reappears — with no resurrection flicker in between.
      await queryClient.invalidateQueries({ queryKey: QUERY_KEY })
      setResolvingIds(prev => {
        if (!prev.has(id)) return prev
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    },
  })

  // Unowned only, never project task-gates, never a row being resolved. The
  // `a.id` guard also keeps a contract-violating id-less row out (it would
  // otherwise collide on a React `key` and share a resolving flag).
  const pending = useMemo(
    () => approvals.filter(
      a => a.id && !a.slot && !a.id.startsWith('task-gate-') && !resolvingIds.has(a.id),
    ),
    [approvals, resolvingIds],
  )

  const count = pending.length

  return (
    <AnimatePresence>
      {count > 0 && (
        <motion.div
          key="pending-approvals-bar"
          data-testid="pending-approvals-bar"
          className="shrink-0 border-t border-warn/30 bg-card/95 backdrop-blur-sm"
          initial={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 12 }}
          animate={reduceMotion ? { opacity: 1 } : { opacity: 1, y: 0 }}
          exit={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 12 }}
          transition={{ duration: 0.18, ease: 'easeOut' }}
          // A newly-arrived approval must be announced to assistive tech even
          // when the bar was already present (count changed, not mounted).
          role="status"
          aria-live="polite"
        >
          {/* Collapsed head: its visible text IS its accessible name, plus
              aria-expanded — no icon-only toggle that would need its own label. */}
          <Clickable
            onClick={() => setExpanded(v => !v)}
            aria-expanded={expanded}
            className="flex items-center gap-2 px-4 py-2 cursor-pointer select-none hover:bg-bg-hover transition-colors"
          >
            <Lock className="lucide-inline text-warn shrink-0" />
            <span className="text-[13px] font-semibold text-text-strong">{count}</span>
            <span className="text-[13px] text-muted">
              {count > 1
                ? i18nT('pages.chat.collapsibleToolGroup.approvals_pending')
                : i18nT('pages.chat.collapsibleToolGroup.approval_needed')}
            </span>
            <span className="flex-1" />
            {expanded
              ? <ChevronDown className="lucide-inline text-muted shrink-0" />
              : <ChevronUp className="lucide-inline text-muted shrink-0" />}
          </Clickable>

          <AnimatePresence initial={false}>
            {expanded && (
              <motion.ul
                className="list-none m-0 px-3 pb-2 max-h-[38vh] overflow-y-auto flex flex-col gap-1.5"
                initial={reduceMotion ? { opacity: 0 } : { opacity: 0, height: 0 }}
                animate={reduceMotion ? { opacity: 1 } : { opacity: 1, height: 'auto' }}
                exit={reduceMotion ? { opacity: 0 } : { opacity: 0, height: 0 }}
                transition={{ duration: 0.16, ease: 'easeOut' }}
              >
                {pending.map(a => {
                  const title = i18nT('hooks.useWebSocket.tool_approval', {
                    name: a.tool || i18nT('hooks.useWebSocket.unknown'),
                  })
                  return (
                    <li
                      key={a.id}
                      className="rounded-md border border-border border-l-[3px] border-l-warn bg-bg-elevated/40 px-3 py-2"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        {a.source && <SourceBadge source={a.source} />}
                        <span className="text-[13px] font-medium text-text-strong truncate min-w-0 flex-1">{title}</span>
                      </div>
                      {a.tool_input && <ToolInputPreview toolInput={a.tool_input} threshold={200} />}
                      <div className="mt-1.5 flex gap-1.5 flex-wrap">
                        <Btn
                          className="hover:text-ok hover:border-ok"
                          onClick={() => resolveMutation.mutate({ id: a.id, action: 'approve' })}
                        ><CheckCircle className="lucide-inline" /> {i18nT('components.approvalCard.approve')}</Btn>
                        <Btn
                          danger
                          onClick={() => resolveMutation.mutate({ id: a.id, action: 'reject' })}
                        ><Ban className="lucide-inline" /> {i18nT('components.approvalCard.reject')}</Btn>
                      </div>
                    </li>
                  )
                })}
              </motion.ul>
            )}
          </AnimatePresence>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
