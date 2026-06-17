import { useState, useCallback } from 'react'
import { ArrowLeft } from 'lucide-react'
import { useIsMobile } from '../hooks/useIsMobile'
import { useAppSelector, useAppDispatch } from '../store'
import { ackNotification } from '../store/notificationsSlice'
import { PageHeader, StatCard, Card, CardTitle, EmptyState } from '../components/ui'
import InfoTip from '../components/InfoTip'
import NotificationFeed from '../components/notifications/NotificationFeed'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import type { Notification } from '../types'

/**
 * Full Notifications page (route /notifications). Page chrome + master/detail
 * layout only; the feed (filter/list) and detail view are the same shared
 * components rendered by the topbar bell popover, so behavior stays identical
 * in both surfaces. This page now owns just the selection state and stat cards.
 */
export default function NotificationsPage() {
  const dispatch = useAppDispatch()
  const items = useAppSelector(s => s.notifications.items)
  const [selectedTs, setSelectedTs] = useState<string | null>(null)
  const isMobile = useIsMobile()

  const unread = items.filter(n => !n.acked).length
  const byCat = useCallback((k: string) => items.filter(n => n.kind === k).length, [items])
  // Derived from items so deleting/clearing the selected notification clears the
  // detail automatically (no separate selection bookkeeping needed).
  const selected = items.find(n => n.ts === selectedTs) || null

  // Auto-ack on select
  const handleSelect = useCallback((n: Notification) => {
    setSelectedTs(n.ts)
    if (!n.acked) dispatch(ackNotification(n.ts))
  }, [dispatch])

  return (
    <>
      <PageHeader title="Notifications" subtitle="All agent activity, cron results, webhooks, and approvals" />
      <div className="px-6 pb-8 flex-1 min-h-0 flex flex-col overflow-hidden">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(120px,1fr))] mb-4 shrink-0">
          <StatCard label="Total" value={items.length} accent />
          <StatCard label="Unread" value={unread} />
          <StatCard label="Cron" value={byCat('cron')} />
          <StatCard label="Hooks" value={byCat('hook')} />
          <StatCard label="Heartbeat" value={byCat('heartbeat')} />
        </div>

        {/* Split layout: feed + detail */}
        <div className="flex-1 min-h-0 flex gap-4">
          {/* Left: feed */}
          <div className={`flex flex-col shrink-0 ${isMobile ? 'w-full' : 'min-w-[320px] max-w-[420px] w-[40%]'} ${isMobile && selected ? 'hidden' : ''}`}>
            <Card className="flex flex-col flex-1 min-h-0">
              <CardTitle>Activity Feed <InfoTip text="Click a notification to view details. Jump to the source chat or job from the detail panel." /></CardTitle>
              <NotificationFeed selectedTs={selectedTs} onSelect={handleSelect} />
            </Card>
          </div>

          {/* Right: detail panel */}
          {isMobile && selected ? (
            <div className="flex-1 min-w-0">
              <Card className="flex flex-col h-full min-h-0">
                <button className="flex items-center gap-1 px-2 py-1.5 text-[13px] text-muted hover:text-text cursor-pointer bg-transparent border-none mb-1" onClick={() => setSelectedTs(null)}>
                  <ArrowLeft size={14} /> Back
                </button>
                <NotificationDetailPanel key={selected.ts} n={selected} onClose={() => setSelectedTs(null)} />
              </Card>
            </div>
          ) : !isMobile && <div className="flex-1 min-w-0">
            {selected ? (
              <Card className="flex flex-col h-full min-h-0">
                <NotificationDetailPanel key={selected.ts} n={selected} onClose={() => setSelectedTs(null)} />
              </Card>
            ) : (
              <Card className="flex items-center justify-center h-full">
                <EmptyState icon={<ArrowLeft className="lucide-inline" />} title="Select a notification" subtitle="Click any item to view details and navigate to its source" />
              </Card>
            )}
          </div>}
        </div>
      </div>
    </>
  )
}
