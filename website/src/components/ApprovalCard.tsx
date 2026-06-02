import { useState } from 'react'
import { CheckCircle, Handshake, Ban, Package, Wrench } from 'lucide-react'
import ToolInputPreview from './ToolInputPreview'
import TrustDropdown from './TrustDropdown'

export default function ApprovalCard({ title, toolInput, showButtons, showTrust = true, onApprove }: {
  title: string; toolInput: string; showButtons: boolean; showTrust?: boolean
  onApprove: (decision: string, pattern?: string) => void
}) {
  const [decided, setDecided] = useState<string | null>(null)
  const handle = (d: string, pattern?: string) => { setDecided(d); onApprove(d, pattern) }
  const borderColor = decided === 'approved' || decided === 'trust' || decided === 'trust_command' || decided === 'trust_base' ? 'border-l-ok' : decided === 'rejected' ? 'border-l-danger' : 'border-l-warn'

  const isShell = title.startsWith('Running: ')
  const normalized = title.replace(/^(Running: |Reading )/, '')
  const baseCmd = normalized.split(/\s+/)[0] || normalized
  const btnClass = 'px-2.5 py-1 rounded-md border border-border bg-transparent text-muted text-[13px] cursor-pointer font-body hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all'

  return (
    <div className={`bg-card border border-border border-l-[3px] ${borderColor} rounded-md px-3.5 py-2.5 text-sm animate-scale-in`}>
      {toolInput
        ? <><strong>Tool approval requested:</strong></>
        : <>{showButtons ? <><Package className="lucide-inline" /> Running: </> : <><Wrench className="lucide-inline" /> </>}<strong>{title}</strong>{showButtons ? ' wants to run' : ''}</>
      }
      {toolInput && <ToolInputPreview toolInput={toolInput} threshold={200} />}
      {showButtons && !decided && (
        <div className="mt-1.5 flex gap-1.5 flex-wrap">
          <button className={btnClass} onClick={() => handle('approved')}><CheckCircle className="lucide-inline" /> Approve</button>
          {showTrust && <TrustDropdown fullCommand={normalized} baseCommand={baseCmd} isShell={isShell} className={btnClass} onAction={(action, pattern) => handle(action, pattern)} />}
          <button className={btnClass + ' hover:!text-danger hover:!border-danger'} onClick={() => handle('rejected')}><Ban className="lucide-inline" /> Reject</button>
        </div>
      )}
      {decided && (
        <div className="mt-1.5 text-[13px] text-muted">
          {decided === 'approved' && <><CheckCircle className="lucide-inline" /> Approved</>}
          {(decided === 'trust' || decided === 'trust_command' || decided === 'trust_base') && <><Handshake className="lucide-inline" /> Trusted — auto-approving future calls</>}
          {decided === 'rejected' && <><Ban className="lucide-inline" /> Rejected</>}
        </div>
      )}
    </div>
  )
}
