// PipelineDashboardView — Issue Radar's pipeline dashboard.
//
// The pipeline used to be a separate builtin app with its own page, its own App
// Store card, and its own idea of which repository it was looking at. It is a
// dashboard here instead, for one reason that decides the whole shape: a pipeline
// is per-repository, and Issue Radar already owns which repository you are looking
// at. As an app it had to reconstruct that choice -- a connected-repo list fetch, a
// remembered localStorage preference, and a picker -- and the reconstruction could
// disagree with the repo picker two panes away. Here the answer is passed in.
//
// Two views, because they answer different questions from DIFFERENT data:
//   * PIPELINE (default) reads the scheduled jobs' own event trail: which step
//     every item is in, what each step is moving, and what each session cost.
//   * ITEM LANES reads the crew ledger through the crew-fabric route and draws one
//     lane per work item across the phase enum.
// They are not two renderings of one dataset, so a tab is honest where a merged
// view would imply the numbers are comparable.
//
// The tab row is the repo's `UnderlineTabs`, not a hand-rolled one: an earlier
// version put `role="tablist"` / `role="tab"` / `aria-selected` on plain buttons
// with no roving tabindex, no arrow-key handling and no `aria-controls`, which
// ANNOUNCES the tabs keyboard contract to assistive tech and then does not honour
// it. A screen reader said "tab 1 of 2" while the arrow keys did nothing, and that
// is worse than not claiming the roles at all.
import { useMemo, useState } from 'react'
import UnderlineTabs, { type UnderlineTab } from '../../../components/UnderlineTabs'
import { i18nT } from '../../../i18n/t'
import { useIssueRadar } from '../context'
import GlobalPipelineView from '../pipeline/views/GlobalPipelineView'
import ItemLanesView from '../pipeline/views/PipelineView'
import type { RepoRef } from '../pipeline/api'

type Tab = 'pipeline' | 'lanes'

export default function PipelineDashboardView() {
  const { active } = useIssueRadar()
  const [tab, setTab] = useState<Tab>('pipeline')

  // Narrowed to the four fields the pipeline reads, and memoized on the VALUES
  // rather than passed through as `active`. Two reasons: `active` carries fields
  // these views have no business reading, and it is a fresh object on renders where
  // the repository has not changed -- which would remount the lanes view (it keys
  // on the repo) and re-key both queries on every unrelated context change.
  const repo = useMemo<RepoRef>(() => {
    const r: RepoRef = { owner: active.owner, repo: active.repo }
    if (active.provider) r.provider = active.provider
    if (active.host) r.host = active.host
    return r
  }, [active.owner, active.repo, active.provider, active.host])

  // Both views are REMOUNTED when the repository changes, not re-rendered.
  //
  // Each one holds drill-down state scoped to one repository -- the fold view its
  // open step and open item, the lanes view its held lane and hover card -- and an
  // issue number means a different item in a different repository. Without this, an
  // operator with #102 expanded who switched repositories kept that item open, and
  // the L2 sessions query is keyed on the number ALONE (the dispatch queue is a
  // number->entry map), so the previous repository's sessions and costs rendered
  // under the new one's name.
  //
  // Applied to BOTH branches here rather than to one: the lanes view already keyed
  // itself internally, which is exactly why the fold view's identical need was easy
  // to miss. The child keeps its own key so it stays correct if rendered elsewhere;
  // this is the seam that guarantees it for the pair.
  //
  // Provider and host are part of the identity, not decoration: `acme/widgets` on
  // GitHub and the same slug on a self-hosted GitLab are different repositories.
  const scopeKey = `${repo.provider ?? 'github'}:${repo.host ?? ''}:${repo.owner}/${repo.repo}`

  const tabs: Array<UnderlineTab<Tab>> = [
    { key: 'pipeline', label: i18nT('apps.autoTriagePipeline.global.tab_pipeline') },
    { key: 'lanes', label: i18nT('apps.autoTriagePipeline.global.tab_lanes') },
  ]

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-bg">
      <div className="shrink-0 px-4 pt-3 md:px-6">
        <UnderlineTabs
          tabs={tabs}
          value={tab}
          onChange={setTab}
          ariaLabel={i18nT('apps.autoTriagePipeline.global.tablist_label')}
        />
      </div>

      <div className="min-h-0 flex-1 overflow-hidden">
        {tab === 'pipeline' ? (
          <GlobalPipelineView key={scopeKey} repo={repo} />
        ) : (
          <ItemLanesView key={scopeKey} repo={repo} />
        )}
      </div>
    </div>
  )
}
