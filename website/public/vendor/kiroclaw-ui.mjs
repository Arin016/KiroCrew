// Vendor stub: re-exports @kiroclaw/ui from the host.
const m = window.__kiroclaw_modules?.['@kiroclaw/ui']
if (!m) throw new Error('[vendor/kiroclaw-ui] Host modules not initialized.')
export const {
  Card, CardTitle, Btn, SendBtn, Input, SearchInput,
  Badge, AimBadge, StatCard, Skeleton, ContentSkeleton,
  EmptyState, PageHeader, Toggle, InfoTip, SegmentedControl,
  MarkdownRenderer,
} = m
