import { useState, useCallback } from 'react'
import { Eye, EyeOff, X, ExternalLink, RotateCcw, Trash2, Lock } from 'lucide-react'
import { Input } from './ui'

/**
 * A write-only credential field. Stored secrets are never displayed: the
 * stored state renders a masked preview (e.g. `xoxb-••••wxyz`) with Replace /
 * Remove only — no API path returns the raw value. When unset — or while
 * replacing — it shows a paste input with a show/hide toggle (that toggle
 * covers only the user's own in-flight input, not a stored secret).
 *
 * The parent owns the pending `value` and `cleared` flags and folds them into
 * its save payload; this component only manages local edit UI state.
 */
export interface SecretFieldProps {
  label: string
  description?: string
  placeholder?: string
  /** True when a secret is currently persisted on the server. */
  isSet: boolean
  /** Masked preview to show when a secret is stored and not revealed. */
  preview: string
  /** Read-only view (remote session): masked display only, no actions. */
  readOnly?: boolean
  /** Pending new value being typed (empty when none). */
  value: string
  onChange: (v: string) => void
  /** True when the user has marked the stored secret for removal on save. */
  cleared: boolean
  onClearedChange: (cleared: boolean) => void
  /** Optional "where do I get this" link rendered as an external-link icon. */
  setupLink?: { href: string; label?: string }
}

export function SecretField({
  label, description, placeholder, isSet, preview, value, onChange,
  cleared, onClearedChange, setupLink, readOnly = false,
}: SecretFieldProps) {
  const [editing, setEditing] = useState(false)
  const [revealed, setRevealed] = useState(false)

  const startReplace = useCallback(() => {
    setEditing(true)
    setRevealed(false)
    onChange('')
  }, [onChange])

  const cancelReplace = useCallback(() => {
    setEditing(false)
    setRevealed(false)
    onChange('')
  }, [onChange])

  const iconBtn = 'w-8 h-8 flex items-center justify-center rounded-md border border-border bg-bg-elevated text-muted hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-40 disabled:cursor-not-allowed'

  return (
    <div className="flex flex-col gap-1.5 py-1.5">
      <div className="flex items-center gap-1.5">
        <span className="text-[13px] font-semibold text-text">{label}</span>
        {setupLink && (
          <a href={setupLink.href} target="_blank" rel="noopener noreferrer"
            className="text-muted hover:text-accent transition-colors" aria-label={setupLink.label ?? 'Where to find this'}>
            <ExternalLink size={12} />
          </a>
        )}
      </div>
      {description && <div className="text-[12px] text-muted">{description}</div>}

      {readOnly ? (
        <div className="flex flex-col gap-1.5">
          <code className="truncate rounded-md border border-border bg-bg-elevated px-3 py-2 text-[13px] text-muted font-mono">
            {isSet ? preview : '(not set)'}
          </code>
          <div className="flex items-center gap-1.5 text-[12px] text-muted">
            <Lock size={12} />
            Managed on the server. Read-only from remote sessions.
          </div>
        </div>
      ) : cleared ? (
        <div className="flex items-center justify-between gap-2 rounded-md border border-danger bg-bg-elevated px-3 py-2">
          <span className="text-[12px] text-danger">Will be removed on save.</span>
          <button type="button" className={iconBtn} onClick={() => onClearedChange(false)} aria-label="Undo remove" title="Undo">
            <RotateCcw size={14} />
          </button>
        </div>
      ) : isSet && !editing ? (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <code className="flex-1 truncate rounded-md border border-border bg-bg-elevated px-3 py-2 text-[13px] text-text font-mono">
              {preview}
            </code>
            <button type="button" className={iconBtn} onClick={startReplace} aria-label="Replace" title="Replace">
              <RotateCcw size={14} />
            </button>
            <button type="button" className={`${iconBtn} hover:text-danger hover:border-danger`} onClick={() => onClearedChange(true)}
              aria-label="Remove" title="Remove">
              <Trash2 size={14} />
            </button>
          </div>
          <div className="flex items-center gap-1.5 text-[12px] text-muted">
            <Lock size={12} />
            Stored securely and never displayed. Replace to rotate.
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <Input
            type={revealed ? 'text' : 'password'}
            value={value}
            onChange={e => onChange(e.target.value)}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            aria-label={label}
            className="font-mono"
          />
          <button type="button" className={iconBtn} onClick={() => setRevealed(r => !r)}
            aria-label={revealed ? 'Hide' : 'Show'} title={revealed ? 'Hide' : 'Show'}>
            {revealed ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
          {isSet && (
            <button type="button" className={iconBtn} onClick={cancelReplace} aria-label="Cancel" title="Cancel">
              <X size={14} />
            </button>
          )}
        </div>
      )}

    </div>
  )
}
